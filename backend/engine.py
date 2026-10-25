"""闲鱼客服软件的 CDP 连接与消息收发引擎。

为什么用 CDP 而不是模拟键鼠：
软件本质是 Electron（内嵌 Chromium），聊天界面是 React 网页。
通过调试端口可以直接读取 DOM、往输入框填字并触发发送，
全程在后台进行，不抢占鼠标、不受分辨率影响，也不修改软件任何文件。

工作流程：
    连接调试端口 -> 定位聊天页 -> 轮询未读会话 -> 关键词匹配 -> 自动回复

DOM 结构（实测，2026-10-15，均用**类名前缀**匹配，不写死运行时哈希）
---------------------------------------------------------------------
* 会话列表项：``div[class*="conversation-item"]``，激活项类名含
  ``conversation-item-active``。文本子元素靠几何特征区分：
  昵称（x≈66、h≥17、w≤200）、预览（x≈66、h≤16、w≤175）、时间（x≈66、h≤16）；
  未读数在 ``[class*="ant-badge"]`` 内（注意：**每个**会话项都有头像徽章，
  无未读时文本为空，必须判非空文本才算未读）。
* 列表是 rc-virtual-list 虚拟滚动，只渲染可见窗口，必须用真实滚轮事件驱动。
* 输入区：``div[class*="sendbox"]``，内含
  ``textarea.ant-input``（真实 textarea，不是 contenteditable）；
  发送按钮是 ``div[class*="sendbox-bottom"]`` 内的 ``button.ant-btn``（文案「发 送」）。
  注意：``[class*="send"]`` 会先匹配到 sendbox 容器 div，点它等于点输入框。
* 聊天列顶栏：``div[class*="message-topbar"]``，含买家昵称，用于校验切换是否成功。

已验证的坑（改代码前先读）
--------------------------
1. 虚拟列表必须用真实滚轮（``page.mouse.wheel``），JS 赋值 scrollTop 会让
   rc-virtual-list 内部偏移失联。
2. 定位会话项要先 ``scrollIntoView({behavior:'instant'})`` → 等渲染稳定 →
   **重新取实时矩形**再点；smooth 动画会让坐标停在旧位置点错人。
3. 点击用真实鼠标事件（``page.mouse.click``），比 JS 合成 click 可靠。
4. 发送前输入框 fill 后要等 React 状态更新（按钮由禁用变可用）再点发送。
"""

from __future__ import annotations

import logging
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

logger = logging.getLogger(__name__)

# 单个元素查找超时（毫秒）。设短一点，避免界面卡住时整个循环卡死。
ACTION_TIMEOUT_MS = 3000
# 发送按钮等待启用的超时（fill 后 React 需要一点时间 re-render）
SEND_TIMEOUT_MS = 5000
# 页面加载等待上限
PAGE_TIMEOUT_MS = 15000

# ============ 已验证的 DOM 几何特征 ============ #
COL_ANCHOR = 66          # 会话项文本列左缘 x 坐标
NICK_MIN_H = 17          # 昵称行高度下限
NICK_MAX_W = 200         # 昵称行宽度上限
PREVIEW_MAX_H = 16       # 预览行高度上限
PREVIEW_MAX_W = 175      # 预览行宽度上限

LIST_WHEEL_AT = (165, 400)   # 会话列表滚轮落点（必须悬停在列表区域上）
WHEEL_STEP = 200             # 单次滚轮像素（约 3 个会话）
WHEEL_UP_BIG = -700          # 快速回顶
SCROLL_SETTLE = 0.5          # 每次滚动后等待渲染
SWITCH_WAIT = 2.5            # 点击会话后等待切换
LIST_MAX_STEPS = 70          # 单次查找最多滚多少轮
ECHO_MEMORY = 50             # 记住多少条自己发过的消息用于回声去重

DEFAULT_SELECTORS = {
    "conversation_list": "[class*='conversation-item']",
    "unread_item": "[class*='ant-badge']",
    "input_box": "[class*='sendbox'] textarea",
    "send_button": "[class*='sendbox-bottom'] button",
}


@dataclass
class Conversation:
    """一个会话的快照。

    Attributes:
        user: 买家昵称，用于日志与去重。
        text: 列表预览文本（即该会话最后一条消息），规则匹配用它。
        unread: 是否有未读徽章。
        preview: 同 text，保留用于打开会话时区分同名会话。
    """

    user: str
    text: str
    unread: bool
    preview: str = ""
    # 会话列表里显示的最后消息时间（"刚刚"、"10小时前"、"09-21"…）。
    # 空串 = 没采到（结构变化），此时按"不确定"处理，不自动回复。
    time_text: str = ""


# ============ JS：会话列表扫描（可见窗口） ============ #

SCAN_JS = r"""
() => {
  const out = [];
  for (const it of document.querySelectorAll('[class*="conversation-item"]')) {
    const r = it.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    const cls = (typeof it.className === 'string') ? it.className : '';
    const active = cls.indexOf('conversation-item-active') >= 0;
    // 未读数：ant-badge 内有非空文本（数字）。
    // 注意每个会话项都有头像 ant-badge，无未读时文本为空，不能只判存在。
    let unread = '';
    for (const b of it.querySelectorAll('[class*="ant-badge"]')) {
      const t = (b.innerText || '').trim();
      if (t) { unread = t; break; }
    }
    // 兜底：徽章数字可能以独立叶子形式落在 x<60 区域
    if (!unread) {
      for (const e of it.querySelectorAll('*')) {
        if (e.children.length) continue;
        const t = (e.innerText || '').trim();
        const rr = e.getBoundingClientRect();
        if (rr.width === 0 || rr.height === 0) continue;
        if (rr.x < 60 && /^\d{1,2}$/.test(t)) { unread = t; break; }
      }
    }
    // 昵称 / 预览：靠几何特征从文本叶子里挑
    let nick = '', preview = '';
    for (const e of it.querySelectorAll('*')) {
      if (e.children.length) continue;
      const t = (e.innerText || '').trim();
      const rr = e.getBoundingClientRect();
      if (!t || rr.width === 0 || rr.height === 0) continue;
      const near = Math.abs(rr.x - 66) <= 6;
      if (near && rr.height >= 17 && rr.width <= 200) {
        if (!nick) nick = t;
      } else if (near && rr.height <= 16 && rr.width <= 175) {
        if (!preview) preview = t;
      }
    }
    if (!nick) continue;
    // 最后消息时间（会话项右侧）："刚刚 / N分钟前 / N小时前 / 昨天 / N天前 / MM-DD / HH:MM"。
    // 为什么必须采集：引擎重启后内存里去重集合清空，会把**历史未读**会话
    // 当成新消息全部回复一遍（2026-10-20 实测：给一条 9 月的"你好"回了消息）。
    // 有了时间就能判断"这条消息到底是不是刚来的"。
    let timeText = '', timeX = -1;
    for (const e of it.querySelectorAll('*')) {
      if (e.children.length) continue;
      const t = (e.innerText || '').trim();
      if (!/^(\d+分钟前|\d+小时前|\d+天前|昨天|刚刚|\d{2}-\d{2}|\d{1,2}:\d{2})$/.test(t)) continue;
      const rr = e.getBoundingClientRect();
      if (rr.width === 0 || rr.height === 0) continue;
      if (rr.x > timeX) { timeX = rr.x; timeText = t; }
    }
    out.push({nick: nick, preview: preview, unread: unread, timeText: timeText,
              active: active, y: Math.round(r.y)});
  }
  return JSON.stringify(out);
}
"""

# 列表滚动状态（供 Python 判断是否滚到头/滚到底）
LIST_STATE_JS = r"""
() => {
  const item = document.querySelector('[class*="conversation-item"]');
  if (!item) return JSON.stringify({found: false});
  let p = item.parentElement, box = null;
  while (p) {
    if (p.scrollHeight > p.clientHeight + 20) { box = p; break; }
    p = p.parentElement;
  }
  if (!box) return JSON.stringify({found: false});
  return JSON.stringify({
    found: true,
    top: Math.round(box.scrollTop),
    clientHeight: box.clientHeight,
    scrollHeight: box.scrollHeight,
    atTop: box.scrollTop <= 1,
    atBottom: box.scrollTop + box.clientHeight >= box.scrollHeight - 4
  });
}
"""

# 按「昵称+预览」定位会话项，命中后 instant 滚动到可见区中部
LOCATE_TMPL = r"""
() => {
  const nick = __NICK__;
  const preview = __PREVIEW__;
  const items = Array.from(document.querySelectorAll('[class*="conversation-item"]'))
    .filter(e => e.getBoundingClientRect().width > 0);
  for (const it of items) {
    let name = '', prev = '';
    for (const e of it.querySelectorAll('*')) {
      if (e.children.length) continue;
      const t = (e.innerText || '').trim();
      const rr = e.getBoundingClientRect();
      if (!t || rr.width === 0) continue;
      if (Math.abs(rr.x - 66) <= 6 && rr.height >= 17 && rr.width <= 200) {
        if (!name) name = t;
      } else if (Math.abs(rr.x - 66) <= 6 && rr.height <= 16 && rr.width <= 175) {
        if (!prev) prev = t;
      }
    }
    if (name !== nick) continue;
    if (preview && prev && prev !== preview) continue;
    it.scrollIntoView({block: 'center', behavior: 'instant'});
    return 'located';
  }
  return 'not-visible';
}
"""

# 重新取命中会话项的实时矩形中心（滚动稳定后调用）
RECT_TMPL = r"""
() => {
  const nick = __NICK__;
  const preview = __PREVIEW__;
  const items = Array.from(document.querySelectorAll('[class*="conversation-item"]'))
    .filter(e => e.getBoundingClientRect().width > 0);
  for (const it of items) {
    let name = '', prev = '';
    for (const e of it.querySelectorAll('*')) {
      if (e.children.length) continue;
      const t = (e.innerText || '').trim();
      const rr = e.getBoundingClientRect();
      if (!t || rr.width === 0) continue;
      if (Math.abs(rr.x - 66) <= 6 && rr.height >= 17 && rr.width <= 200) {
        if (!name) name = t;
      } else if (Math.abs(rr.x - 66) <= 6 && rr.height <= 16 && rr.width <= 175) {
        if (!prev) prev = t;
      }
    }
    if (name !== nick) continue;
    if (preview && prev && prev !== preview) continue;
    const r = it.getBoundingClientRect();
    return JSON.stringify({x: Math.round(r.x + r.width / 2),
                           y: Math.round(r.y + r.height / 2)});
  }
  return 'not-found';
}
"""

TOPBAR_JS = r"""
() => {
  const el = document.querySelector('[class*="message-topbar"]');
  return el ? (el.innerText || '').replace(/\s+/g, ' ').trim() : '';
}
"""

# 聊天**消息区**文本（用来核对当前会话里是否真的有目标订单号）
#
# 铁律（2026-10-18 20:44 实测踩坑）：**绝不能用整页 body 判定订单号**——
# 右侧「买家信息」面板展示的是**全店订单**，同一订单号会在该买家的每一个话题
# 里都出现，用 body 判定会 100% 命中错误的那一条（用户截图两次指出发错话题）。
# 这里必须只取消息气泡容器，排除右侧面板。
CHAT_TEXT_JS = r"""
() => {
  const sels = ['[class*="message-panel"]', '[class*="chat-content"]',
                '[class*="message-list"]', '[class*="message-content"]',
                '[class*="chat-message"]', '[class*="message-body"]'];
  for (const s of sels) {
    const el = document.querySelector(s);
    if (el) { return (el.innerText || '').replace(/\s+/g, ' ').trim(); }
  }
  // 兜底：取最大的消息气泡容器（右侧面板宽度窄且类名不同）
  let best = null, bestArea = 0;
  for (const e of document.querySelectorAll('div')) {
    const r = e.getBoundingClientRect();
    if (r.width < 200 || r.height < 100) continue;
    const a = r.width * r.height;
    if (a > bestArea) { bestArea = a; best = e; }
  }
  return best ? (best.innerText || '').replace(/\s+/g, ' ').trim() : '';
}
"""

# 聊天消息区文本（发货主流程统一走这个常量名）
CHAT_AREA_JS = CHAT_TEXT_JS

# 发送后校验：卡密是否真的出现在**当前会话**的聊天区里。
# 为什么必须校验（2026-10-18 22:31 用户截图血证）：send_reply 原来只校验
# "输入框被清空"，而输入框清空只证明文本从输入框消失了，**不证明它发到了
# 当前会话**——SPA 路由残留/会话被切换时，消息会发到别的买家那里，而返回值
# 仍是 True → deliveries 记成 sent → 永不重试 → 「日志成功、界面没卡密」。
CHAT_HAS_TEXT_JS = r"""
(needle) => {
  const sels = ['[class*="message-panel"]', '[class*="chat-content"]',
                '[class*="message-list"]', '[class*="message-content"]',
                '[class*="chat-message"]', '[class*="message-body"]'];
  for (const s of sels) {
    const el = document.querySelector(s);
    if (el) {
      const t = (el.innerText || '').replace(/\s+/g, '');
      if (t.indexOf(needle) >= 0) return '1';
    }
  }
  let best = null, bestArea = 0;
  for (const e of document.querySelectorAll('div')) {
    const r = e.getBoundingClientRect();
    if (r.width < 200 || r.height < 100) continue;
    const a = r.width * r.height;
    if (a > bestArea) { bestArea = a; best = e; }
  }
  if (best) {
    const t = (best.innerText || '').replace(/\s+/g, '');
    return t.indexOf(needle) >= 0 ? '1' : '0';
  }
  return '0';
}
"""

# 取**所有**同名会话项的矩形列表（同名买家可能有多条会话，
# 2026-10-18 实测：列表里同时存在 3 个「巨星孙不易」，按昵称盲选必发错人）
RECT_ALL_TMPL = r"""
() => {
  const nick = __NICK__;
  const items = Array.from(document.querySelectorAll('[class*="conversation-item"]'))
    .filter(e => e.getBoundingClientRect().width > 0);
  const out = [];
  for (const it of items) {
    let name = '', prev = '';
    for (const e of it.querySelectorAll('*')) {
      if (e.children.length) continue;
      const t = (e.innerText || '').trim();
      const rr = e.getBoundingClientRect();
      if (!t || rr.width === 0) continue;
      if (Math.abs(rr.x - 66) <= 6 && rr.height >= 17 && rr.width <= 200) {
        if (!name) name = t;
      } else if (Math.abs(rr.x - 66) <= 6 && rr.height <= 16 && rr.width <= 175) {
        if (!prev) prev = t;
      }
    }
    if (name !== nick) continue;
    const r = it.getBoundingClientRect();
    out.push({x: Math.round(r.x + r.width / 2),
              y: Math.round(r.y + r.height / 2),
              preview: prev});
  }
  return JSON.stringify(out);
}
"""


class XianyuIMEngine:
    """闲鱼客服软件的自动化引擎。

    通过 Playwright 连接 Electron 开放的 CDP 端口，实现消息检测与回复。
    所有 DOM 交互均基于实测结构（见模块文档字符串），改版后优先调
    config.yaml 的 selectors，其次改本文件的几何常量。
    """

    def __init__(self, cdp_port: int, selectors: dict[str, str] | None = None,
                 dry_run: bool = False) -> None:
        """初始化引擎。

        Args:
            cdp_port: 软件启动时开放的调试端口。
            selectors: 页面元素选择器映射（缺省用实测默认值）。
            dry_run: 试运行模式，只打印将发送的内容而不真实发送。
        """
        self.cdp_port = cdp_port
        self.selectors = {**DEFAULT_SELECTORS, **(selectors or {})}
        self.dry_run = dry_run
        self._pw = None
        self._browser = None
        self._page = None
        # 自己发过的消息文本，用于识别会话预览里的回声，防止自问自答
        self._sent_texts: deque[str] = deque(maxlen=ECHO_MEMORY)
        # 上次轮询时「当前激活会话」的 (昵称, 预览)，用于发现
        # 「新消息落进正打开的会话」——这种消息不产生未读徽章。
        # 只由 process() 在消息真正进入处理后更新；被去重挡下时不得更新，
        # 否则相同文本的后续消息永远不再触发变化检测（历史 bug）。
        self._last_active: tuple[str, str] | None = None
        # 最近一次扫描看到的激活会话（供 process 决策用，不直接当基线）
        self._pending_active: tuple[str, str] | None = None

    # ============ 连接管理 ============ #

    def connect(self, timeout: float = 30.0) -> bool:
        """连接已运行的软件调试端口。

        Args:
            timeout: 等待软件就绪的最长秒数。

        Returns:
            连接成功返回 True，超时或端口未开放返回 False。
        """
        endpoint = f"http://127.0.0.1:{self.cdp_port}"
        deadline = time.time() + timeout

        self._pw = sync_playwright().start()
        while time.time() < deadline:
            try:
                self._browser = self._pw.chromium.connect_over_cdp(
                    endpoint, timeout=ACTION_TIMEOUT_MS
                )
                logger.info("已连接调试端口 %s", endpoint)
                return True
            except PlaywrightError as exc:
                logger.debug("端口未就绪，等待中: %s", exc)
                time.sleep(1.0)

        logger.error("连接超时，请确认软件已用 --remote-debugging-port=%d 启动", self.cdp_port)
        self.close()
        return False

    def find_chat_page(self) -> bool:
        """在所有页面中定位聊天主界面。

        判定优先级：DOM 特征（存在会话列表项）> URL 特征 > 页面体积。
        聊天页 URL 是 seller.goofish.com，不含 im/chat/message 字样，
        所以 DOM 特征才是主判据。

        Returns:
            找到并激活返回 True，否则 False。
        """
        if not self._browser:
            return False

        candidates: list[Any] = []
        for ctx in self._browser.contexts:
            candidates.extend(ctx.pages)

        # 1) DOM 特征：页面里渲染出了会话列表项（等页面加载，最多 30 秒）
        deadline = time.time() + 30
        while time.time() < deadline:
            for page in candidates:
                try:
                    hit = page.evaluate(
                        "() => !!document.querySelector('[class*=\"conversation-item\"]')"
                    )
                except Exception:
                    continue
                if hit:
                    logger.info("按 DOM 特征定位到聊天页: %s", page.url)
                    self._page = page
                    break
            if self._page:
                break
            time.sleep(1.0)

        # 2) URL 特征回退
        if not self._page:
            for page in candidates:
                try:
                    url = page.url.lower()
                except Exception:
                    continue
                if "im" in url or "chat" in url or "message" in url:
                    logger.info("按 URL 定位到聊天页: %s", url)
                    self._page = page
                    break

        # 3) 体积回退：选正文文本最多的页面
        if not self._page and candidates:
            largest, size = None, 0
            for page in candidates:
                try:
                    v = page.evaluate("() => document.body ? document.body.innerText.length : 0")
                    if v > size:
                        largest, size = page, v
                except Exception:
                    continue
            if largest:
                logger.info("按页面体积回退定位: %s", largest.url)
                self._page = largest

        if not self._page:
            logger.error("未找到聊天页面")
            return False

        self._page.set_default_timeout(ACTION_TIMEOUT_MS)
        return True

    def close(self) -> None:
        """释放 Playwright 资源。

        注意：只断开 CDP 连接，不会关闭用户正在使用的软件窗口。
        """
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        # 页面对象随连接一起作废。不清 _page 会让主循环的重连条件
        # （engine._page is None）永远不成立，CDP 断开后无法自愈。
        self._page = None
        logger.info("已断开连接")

    # ============ 消息读取 ============ #

    def scan_unread(self, full: bool = False) -> list[Conversation]:
        """扫描会话列表，找出需要处理的会话。

        两类会话会被返回：
        1. 有未读徽章的会话（买家新消息的常规信号）；
        2. 「当前激活会话」且预览内容较上次轮询发生了变化的会话——
           买家给正打开的会话发消息时不产生未读徽章，只能这样发现。

        Args:
            full: 为 True 时用滚轮滚动整个列表再采集（用于兜底捞折叠在
                虚拟窗口外的旧未读），耗时约半分钟；日常轮询用 False 只
                采可见窗口（新消息会把会话顶到最上面，必然可见）。

        Returns:
            待处理的 Conversation 列表。
        """
        if not self._page:
            return []

        found: list[Conversation] = []
        try:
            if full:
                self._scroll_list_to_top()
                # 等列表进入可滚动状态：软件刚启动时虚拟列表可能还没
                # 加载完（所有项塞在可视区内、没有可滚动容器），
                # 不等待会误判为「滚到底」只采到首屏几项。
                warmed = False
                for _ in range(20):
                    if self._list_state().get("found"):
                        warmed = True
                        break
                    time.sleep(0.5)
                if warmed:
                    for _ in range(LIST_MAX_STEPS):
                        found.extend(self._scan_visible())
                        if self._list_state().get("atBottom"):
                            break
                        self._wheel_list(WHEEL_STEP)
                        time.sleep(SCROLL_SETTLE)
                else:
                    found = self._scan_visible()  # 列表很短，全部可见
                self._scroll_list_to_top()
                # 把鼠标移出列表：悬停也会给列表项加 active 类，
                # 停着不动会压制「真正打开的会话」的变化检测
                self._page.mouse.move(700, 400)
            else:
                found = self._scan_visible()
        except Exception as exc:  # noqa: BLE001
            # 连接类异常（PlaywrightError/Timeout 等）：页面对象已失效，
            # 必须作废 _page，主循环下一轮才会走 connect() 重连。
            # （历史 bug：这里只 warning+return []，主循环以为无未读，
            #  而 _page 非 None 又跳过重连，引擎从此空转到底。）
            logger.warning("扫描未读失败: %s", exc)
            self._page = None
            return []
        return found

    def list_conversations(self, limit: int = 0) -> list[dict]:
        """按列表顺序（新→旧）收集会话，最多 limit 个；limit=0 表示全部。

        与 _scan_visible 的区别：**不做未读/激活过滤**，返回全部可见会话
        （nick/preview/active），供定时扫描按「前 N 个」顺序遍历静默买家
        （付款后不再发言、没有未读徽章的买家，实时轮询永远看不到他们）。
        超出可见窗口时才滚动；虚拟列表重复渲染的项按 (nick, preview) 去重；
        收集完滚回顶部，尽量不改变用户的视图位置。

        Args:
            limit: 最多收集多少个；0=全部（滚到列表底部）。

        Returns:
            [{"nick": str, "preview": str, "active": bool}, ...]；
            连接类异常时作废 _page 并返回 []（与 scan_unread 一致，
            主循环下一轮走 connect() 重连）。
        """
        if not self._page:
            return []
        out: list[dict] = []
        seen: set[tuple[str, str]] = set()
        try:
            # 列表可能还没加载完（没有可滚动容器），先等它进入可滚动状态
            for _ in range(12):
                if self._list_state().get("found"):
                    break
                time.sleep(0.5)
            for _ in range(LIST_MAX_STEPS):
                rows = _parse_json(self._page.evaluate(SCAN_JS)) or []
                for it in rows:
                    nick = (it.get("nick") or "").strip()
                    preview = (it.get("preview") or "").strip()
                    if not nick:
                        continue  # 昵称都提不到的会话无法定位，跳过
                    key = (nick, preview)
                    if key in seen:
                        continue  # 虚拟列表缓冲期重复渲染，只记第一次
                    seen.add(key)
                    out.append({"nick": nick, "preview": preview,
                                "active": bool(it.get("active"))})
                if limit and len(out) >= limit:
                    out = out[:limit]
                    break
                st = self._list_state()
                if not st.get("found") or st.get("atBottom"):
                    break
                self._wheel_list(WHEEL_STEP)
                time.sleep(SCROLL_SETTLE)
        except Exception as exc:  # noqa: BLE001
            logger.warning("收集会话列表失败: %s", exc)
            self._page = None
            return []
        # 滚回顶部：收集过程可能滚得很深，还原用户习惯的视图位置
        try:
            self._scroll_list_to_top()
        except Exception:  # noqa: BLE001
            pass
        return out

    def current_active(self) -> dict | None:
        """当前激活（打开中）的会话 {"nick", "preview"}；取不到返回 None。

        定时扫描开始前调用，用于扫完后还原用户原本在看的会话（降入侵）。
        """
        if not self._page:
            return None
        try:
            rows = _parse_json(self._page.evaluate(SCAN_JS)) or []
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取激活会话失败: %s", exc)
            return None
        for it in rows:
            nick = (it.get("nick") or "").strip()
            if it.get("active") and nick:
                return {"nick": nick, "preview": (it.get("preview") or "").strip()}
        return None

    def _looks_self_sent(self, preview: str) -> bool:
        """判断会话列表预览的最后一条消息是不是**我方（卖家）自己**发的。

        为什么需要（2026-10-18 22:05 用户截图指出「他什么都没说，为什么一直在
        发，还把我的卡密也发出去了」）：会话列表的 preview 是「最后一条消息」，
        **不区分方向**。我方自动发出的卡密全文（夸克链接 + 提取码 + 温馨提示）
        也会成为 preview。旧逻辑直接把它当买家新消息跑关键词 → 命中
        「链接/提取码/卡密」→ 给从没付款的人反复自动回复。

        判据：卡卷/发货模板的固定特征（只看**开头**即可，避免正文里的词误伤）。
        这里刻意保守——宁可漏判（多回一句无害文案）也不误判（把卡密当买家
        提问复述出去）。

        Args:
            preview: 会话列表的预览文本。

        Returns:
            像是我方发货消息返回 True。
        """
        if not preview:
            return False
        head = preview[:80]
        markers = (
            "我用夸克网盘给你分享了",      # 网盘分享模板首句
            "发货内容含链接+提取码",        # 规则#3 文案
            "感谢咨询，本店虚拟商品全部",   # 发货话术模板
            "在的呢~ 感谢咨询",
            "快给ta一个评价吧",            # 平台系统提示（非买家消息）
            "对方已读你的消息",
        )
        return any(m in head for m in markers)

    def _scan_visible(self) -> list[Conversation]:
        """采集当前可见窗口的会话，应用激活项变化检测。

        激活会话（当前打开的会话）在三种情况下纳入结果：
        1. 首次见到（脚本刚启动）——应用启动时会恢复上次的激活会话，
           买家趁应用关闭时发来的消息可能就落在里面，不基线吞掉它；
        2. 同一个买家的预览内容发生变化——即正打开的会话来了新消息
           （这种消息不产生未读徽章，只能这样发现）；
        3. 带了未读徽章。
        若卖家手动切换到别的会话，只静默更新基线，不触发回复
        （避免机器人追着卖家翻看的老旧消息回）。

        **铁律（2026-10-18 22:05 用户截图指出）**：会话列表的预览文本是
        「最后一条消息」，**不分方向**——我方自己发出的卡密全文（夸克链接、
        提取码）同样会出现在预览里。旧逻辑把它当成"买家新消息"去跑关键词
        → 命中「链接/提取码/卡密」→ 给**从没付款的人**反复自动回复，甚至把
        卡密原文当消息内容读回来（实测21:46给 super 触发规则#3）。
        现在必须先判方向：预览是我方发的就**不当买家消息**处理。
        """
        raw = self._page.evaluate(SCAN_JS)
        rows = _parse_json(raw)
        out: list[Conversation] = []
        active_seen: tuple[str, str] | None = None
        for it in rows:
            nick = (it.get("nick") or "").strip()
            preview = (it.get("preview") or "").strip()
            if not nick:
                continue  # 昵称都提不到的会话无法定位，跳过
            # 我方自己发的消息（含卡密）→ 只更新基线，绝不当买家消息
            self_sent = bool(it.get("self_sent")) or self._looks_self_sent(preview)
            time_text = (it.get("timeText") or "").strip()
            if it.get("active"):
                active_seen = (nick, preview)
                first_sight = self._last_active is None
                same_nick_change = (
                    self._last_active is not None
                    and self._last_active[0] == nick
                    and self._last_active[1] != preview
                )
                if self_sent:
                    continue  # 我方发的（多半是刚发完的卡密）→ 不回自己
                if it.get("unread") or ((first_sight or same_nick_change) and preview):
                    out.append(Conversation(user=nick, text=preview,
                                            unread=bool(it.get("unread")),
                                            preview=preview, time_text=time_text))
                continue
            if not it.get("unread"):
                continue
            if self_sent:
                continue  # 未读但内容是我方发的（自己发的会带已读回执），跳过
            out.append(Conversation(user=nick, text=preview,
                                    unread=True, preview=preview, time_text=time_text))
        if active_seen is not None:
            # 只记录"最近见到的激活会话"，不直接更新 _last_active 基线：
            # 基线改由 process() 在消息真正进入处理后更新。若在此处无条件
            # 刷新基线，被去重窗口挡下的买家追问会污染基线，导致相同文本的
            # 后续消息永远不再触发变化检测（扫描层再也看不见这个会话）。
            self._pending_active = active_seen
        return out

    def is_duplicate(self, user: str, text: str) -> bool:
        """判断是否是自己刚发出消息的回声。

        闲鱼界面会把已发消息也显示在列表预览里，若不做判断，
        「激活会话内容变化」的检测会把自己发的内容当成新消息，造成自问自答。

        Args:
            user: 买家昵称（保留参数，回声判定只看文本）。
            text: 疑似新消息文本。

        Returns:
            判定为回声返回 True。
        """
        key = (text or "").strip()
        if not key:
            return True
        return key in self._sent_texts

    def _remember_sent(self, text: str) -> None:
        """记录一条自己发送的消息，供回声去重。"""
        self._sent_texts.append((text or "").strip())

    # ============ 列表滚动（真实滚轮驱动虚拟列表） ============ #

    def _wheel_list(self, dy: int) -> None:
        """在会话列表区域派发一次滚轮事件。"""
        self._page.mouse.move(LIST_WHEEL_AT[0], LIST_WHEEL_AT[1])
        self._page.mouse.wheel(0, dy)

    def _list_state(self) -> dict:
        raw = self._page.evaluate(LIST_STATE_JS)
        return _parse_json(raw)

    def _scroll_list_to_top(self) -> bool:
        """用滚轮把会话列表滚回顶部。"""
        for _ in range(LIST_MAX_STEPS):
            st = self._list_state()
            if not st.get("found"):
                return False
            if st.get("atTop"):
                return True
            self._wheel_list(WHEEL_UP_BIG)
            time.sleep(0.25)
        return False

    # ============ 消息发送 ============ #

    def open_conversation(self, user: str, preview: str = "",
                          retries: int = 3) -> bool:
        """在会话列表中点击进入指定买家的会话。

        顺序：只向下滚找（多数情况一步到位）→ 找不到回顶部再找 →
        instant 滚动定位 → 等渲染稳定 → 重取实时矩形 → 真实鼠标点击 →
        顶栏昵称校验 → 失败重试。

        Args:
            user: 买家昵称。
            preview: 列表预览文本，用于区分同名买家的多条会话。
            retries: 校验失败时的重试次数。

        Returns:
            点击并校验成功返回 True。
        """
        if not self._page:
            return False

        for attempt in range(retries):
            try:
                if not self._ensure_visible(user, preview):
                    self._scroll_list_to_top()
                    if not self._ensure_visible(user, preview):
                        logger.warning("未在列表中找到会话: %s", user)
                        return False

                # 等滚动稳定后再取实时坐标点击（避免拿到旧坐标点错人）
                time.sleep(SCROLL_SETTLE)
                rect_raw = self._page.evaluate(
                    RECT_TMPL.replace("__NICK__", _js_str(user))
                    .replace("__PREVIEW__", _js_str(preview or ""))
                )
                rect = _parse_json(rect_raw)
                if not rect or "x" not in rect:
                    continue
                self._page.mouse.click(rect["x"], rect["y"])
                time.sleep(SWITCH_WAIT)
                # 鼠标移出列表，避免悬停项与真正激活的会话抢 active 类
                self._page.mouse.move(700, 400)

                topbar = self._page.evaluate(TOPBAR_JS) or ""
                if (not topbar) or (user in topbar):
                    return True
                logger.warning("第 %d 次点击后顶栏仍显示「%s」，重试",
                               attempt + 1, topbar[:24])
            except (PlaywrightTimeout, PlaywrightError) as exc:
                logger.warning("进入会话失败 %s: %s", user, exc)
        return False

    def open_conversation_for_order(self, user: str, order_id: str,
                                    retries: int = 3,
                                    buyer_hint: str = "",
                                    title_hint: str = "") -> bool:
        """打开属于该订单买家的同名会话。

        为什么要有这个方法（2026-10-18 20:44 用户截图两次指出发错话题）：
        闲鱼**同一个买家会有多条并存会话**（按商品/订单拆话题，实测「巨星孙不易」
        同时 3 条），昵称和顶栏闲鱼号完全相同（都是 `w***ζ`），光看昵称或闲鱼号
        **无法区分**，只能靠"哪条话题里真的出现这笔订单的付款消息"来判断。

        判据（按可靠性排序）：
        1. **聊天消息区**出现目标订单号 → 命中（若该会话消息区带订单号）；
        2. **聊天消息区的订单卡文案命中目标商品标题**（`title_hint`）→ 命中。
           这是**目前唯一稳定可用**的主判据：2026-10-18 22:25 实测闲鱼的
           订单卡 DOM（`msg-dx-*`）**根本不含订单号**（oids=[]），只写
           「商品名 ¥价格 / 我已付款，等待你发货 / 去发货」。同一买家按商品拆
           话题，所以"这个话题里在聊哪个商品"正是区分多条话题的可靠依据；
        3. 聊天消息区出现买家收货姓名/电话尾号（`buyer_hint`）→ 命中（次选）；
        3. 都不中 → 返回 False，**下轮重试**。
           铁律（2026-10-18 22:35 用户截图再次血证）：定位失败时**绝不能瞎发**。
           旧实现"退回最早找到的那条候选"是错的——跨屏累积的坐标在列表滚动
           后已失效，点击落到**别的买家**会话上，卡密发错人还被记成 sent，
           结果就是「日志显示发卡密成功、买家界面里根本没有卡密、且永不再
           重试」（实测 22:25:17 订单 …025185）。宁可这轮不发、下一轮重新
           定位，也不能发错人 + 假成功。

        注意：**绝不能用整页 body 文本判订单号**（2026-10-20 实测踩坑）——
        右侧「买家信息」面板展示的是**全店订单**，同一订单号会在该买家的所有
        话题里都出现，用body 判定会 100% 命中错误的那一条。

        Args:
            user: 买家昵称。
            order_id: 目标订单号。
            retries: 翻找轮数。
            buyer_hint: 订单详情页「收货信息」里的姓名/电话，用于次选判据。
            title_hint: 保留参数（当前定位**不依赖**商品标题——探针实测闲鱼
                聊天订单卡里既无订单号也无商品名；主判据是平台发货回执
                「你已发货」）。传了也不影响判定。

        Returns:
            找到并打开会话返回 True；**定位不到一律返回 False，绝不猜着发**
            （猜着发 = 发错人 + 被记成sent + 永不重试）。
        """
        if not self._page:
            return False
        oid = str(order_id or "")
        if not oid:
            return self.open_conversation(user, "")
        hints = [h for h in (buyer_hint or "").replace("，", ",").split(",") if h.strip()]
        rect_js = RECT_ALL_TMPL.replace("__NICK__", _js_str(user))
        #每轮重新取候选：列表滚动后旧坐标已失效，累积下来的坐标会点到
        # 别的买家会话上（22:31 用户截图：日志说发成功、界面没卡密）
        seen_rects: list = []
        for attempt in range(retries):
            try:
                rects = _parse_json(self._page.evaluate(rect_js)) or []
                seen_rects = []  # 滚动后坐标全变，每轮重置去重表
                if not rects:
                    self._scroll_list_to_top()
                    time.sleep(SCROLL_SETTLE)
                    rects = _parse_json(self._page.evaluate(rect_js)) or []
                if not rects:
                    logger.warning("未在列表中找到同名会话: %s", user)
                    return False
                logger.info("同名会话候选 %d 个，逐个核对订单 %s",
                            len(rects), oid)
                first_ok = False
                for r in rects:
                    try:
                        cx, cy = int(r["x"]), int(r["y"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    # 同一坐标只试一次，避免重复点同一条
                    if any(abs(cx - p[0]) < 4 and abs(cy - p[1]) < 4
                           for p in seen_rects):
                        continue
                    seen_rects.append((cx, cy))
                    try:
                        self._page.mouse.click(cx, cy)
                    except PlaywrightError:
                        continue
                    time.sleep(SWITCH_WAIT)
                    self._page.mouse.move(700, 400)
                    topbar = self._page.evaluate(TOPBAR_JS) or ""
                    if user and topbar and (user not in topbar):
                        continue  # 点错人了，继续下一个候选
                    if not first_ok:
                        first_ok = True  # 同名里第一条正确命中的会话
                    # 只看聊天消息区，排除右侧全店订单面板
                    chat = self._page.evaluate(CHAT_AREA_JS) or ""
                    if oid and oid in chat:
                        logger.info("已定位订单 %s 所在会话「%s」（聊天区订单号命中）",
                                    oid, user)
                        return True
                    if hints and any(h in chat for h in hints):
                        logger.info("已定位订单 %s 所在会话「%s」（收货信息命中 %s）",
                                    oid, user, hints[0])
                        return True
                    # **主判据：平台发货回执「你已发货」**。
                    # 2026-10-18 22:45 探针实测：闲鱼的聊天订单卡**既不含订单号、
                    # 也不含商品名**（卡面只有「我已付款，等待你发货 / 去发货 /
                    # 你已发货」），所以按订单号或商品标题匹配都必然失败。
                    # 真正能把「这笔订单所在的话题」与同名的其他历史话题区分开
                    # 的，是这个话题里存在平台发货回执：实测「巨星孙不易」3 条
                    # 并存话题里，只有订单那条有 4 个「你已发货」，另外两条
                    # （纯闲聊/ 历史发货）一个都没有。
                    if "你已发货" in chat:
                        logger.info("已定位订单 %s 所在会话「%s」（平台发货回执命中，"
                                    "该话题含%d 笔已发货订单）",
                                    oid, user, chat.count("你已发货"))
                        return True
                if attempt + 1 < retries:
                    # 同屏没找到，可能目标话题在虚拟列表更下方，向下翻找
                    self._wheel_list(WHEEL_STEP)
                    time.sleep(SCROLL_SETTLE)
            except (PlaywrightTimeout, PlaywrightError) as exc:
                logger.warning("核对订单会话失败 %s: %s", user, exc)
        # 定位失败：**绝不发**。跨屏累积的 seen_rects 坐标在列表滚动后已失效，
        # 点击会落到别的买家会话上；旧实现在这里兜底点击并返回 True，导致
        # 卡密发错人却记成 sent（用户 22:31 截图：日志说发成功，界面无卡密）。
        # 这里必须返回 False，让本轮放弃、下轮用新鲜的坐标重新定位。
        logger.warning("订单 %s 未能在「%s」的任一话题中定位到，"
                       "本轮不发卡密（下轮重新定位，绝不发错人）", oid, user)
        return False

    def _ensure_visible(self, nick: str, preview: str) -> bool:
        """只向下滚，找到目标即停。"""
        locate_js = (LOCATE_TMPL.replace("__NICK__", _js_str(nick))
                     .replace("__PREVIEW__", _js_str(preview or "")))
        # 列表可能还没加载完（没有可滚动容器），先等它进入可滚动状态
        for _ in range(12):
            if self._list_state().get("found"):
                break
            time.sleep(0.5)
        for _ in range(LIST_MAX_STEPS):
            if self._page.evaluate(locate_js) == "located":
                return True
            st = self._list_state()
            if not st.get("found") or st.get("atBottom"):
                return False
            self._wheel_list(WHEEL_STEP)
            time.sleep(SCROLL_SETTLE)
        return False

    def send_reply(self, reply: str) -> bool:
        """在当前会话发送一条消息。

        流程：fill 输入框（Playwright fill 会走原生 setter + input 事件，
        React 受控组件能收到）→ 等发送按钮由禁用变可用 → 点击 →
        校验输入框被清空（未清空说明没发出去，补一次回车）。

        Args:
            reply: 要发送的回复内容。

        Returns:
            发送成功返回 True。
        """
        if not self._page:
            return False

        if self.dry_run:
            logger.info("[试运行] 模拟发送: %s", reply)
            self._remember_sent(reply)
            return True

        input_sel = self.selectors.get("input_box") or DEFAULT_SELECTORS["input_box"]
        send_sel = self.selectors.get("send_button") or DEFAULT_SELECTORS["send_button"]
        try:
            box = self._page.locator(input_sel).first
            if box.count() == 0:
                # 路由漂移自愈：平台确认发货/读订单详情会把 SPA 路由带离
                # #/im-desktop，聊天输入框不在 DOM 里。这里重新定位一次聊天页
                # 再试（2026-10-19 00:35 实测：该场景导致「未找到输入框」，
                # 钱已过户但卡密没发出去）。
                logger.warning("未找到输入框，重新定位聊天页后重试: %s", input_sel)
                try:
                    self.find_chat_page()
                    time.sleep(1.5)
                except Exception as exc:  # noqa: BLE001
                    logger.error("重新定位聊天页失败: %s", exc)
                box = self._page.locator(input_sel).first
                if box.count() == 0:
                    logger.error("未找到输入框: %s", input_sel)
                    return False

            box.click(timeout=ACTION_TIMEOUT_MS)
            box.fill(reply)
            time.sleep(0.3)

            btn = self._page.locator(send_sel).first
            if btn.count() == 0:
                # 选择器失效时按按钮文案兜底
                btn = self._page.locator('button:has-text("发 送")').first
            sent = False
            if btn.count() > 0:
                try:
                    # 按钮在输入框为空时是禁用的，Playwright 会等它启用
                    btn.click(timeout=SEND_TIMEOUT_MS)
                    sent = True
                except PlaywrightTimeout:
                    logger.warning("发送按钮未就绪，退化为回车发送")
            if not sent:
                box.press("Enter")
            time.sleep(0.6)

            # 校验：发送成功后输入框应被清空
            left = (box.input_value() or "").strip()
            if left:
                logger.warning("发送后输入框仍有内容，尝试回车补发")
                box.press("Enter")
                time.sleep(0.6)
                left = (box.input_value() or "").strip()
                if left:
                    logger.error("发送失败，输入框内容未清空: %s", left[:30])
                    return False

            # 校验：消息是否真的出现在**当前会话**的聊天区里。
            # 只校验"输入框清空"是不够的——它只证明文本离开输入框，不证明
            # 发到了这个会话（实测 22:25卡密发到了别的买家会话，函数仍返回
            # True，deliveries 被记成 sent，买家永远拿不到卡密）。
            # 取卡密里最有辨识度的一段（去掉可能因换行/省略号变形的部分）。
            needle = _verify_needle(reply)
            if needle:
                ok_in_chat = False
                for _ in range(6):
                    if self._page.evaluate(CHAT_HAS_TEXT_JS, needle) == "1":
                        ok_in_chat = True
                        break
                    time.sleep(0.5)
                if not ok_in_chat:
                    logger.error("发送后未在当前会话看到消息（可能发到了别的会话），"
                                  "判为失败: %s", needle[:20])
                    return False

            self._remember_sent(reply)
            logger.info("已发送: %s", reply)
            return True
        except (PlaywrightTimeout, PlaywrightError) as exc:
            logger.error("发送失败: %s", exc)
            return False

    # ============ 辅助 ============ #

    def screenshot(self, path: str) -> None:
        """保存当前界面截图，便于排查选择器失效。

        Args:
            path: 截图保存路径。
        """
        if not self._page:
            return
        try:
            self._page.screenshot(path=path)
            logger.info("截图已保存: %s", path)
        except Exception as exc:
            logger.warning("截图失败: %s", exc)


def _js_str(value: str) -> str:
    """把 Python 字符串烘焙成 JS 字符串字面量。"""
    import json
    return json.dumps(value or "")


def _verify_needle(reply: str) -> str:
    """从待发文本里挑一段用于"发送后校验"的特征串。

    要求：足够唯一（能证明是这条消息）、足够短（不受渲染截断影响）。
    做法：优先取网盘提取码 / 链接尾段这类高辨识度片段；否则取正文开头
    连续的一段可打印字符（跳过标点与空白，避免渲染时被拆行破坏）。

    Args:
        reply: 实际发送出去的文本。

    Returns:
        校验用特征串；无合适片段返回 ""（跳过校验）。
    """
    import re as _re
    s = (reply or "").strip()
    if not s:
        return ""
    # 1) 提取码后面那串（最唯一）
    m = _re.search(r"提取码[：:\s]*([A-Za-z0-9]{4,})", s)
    if m:
        return m.group(1)
    # 2) 网盘链接的末段 id
    m = _re.search(r"pan\.[\w.]+/s/([A-Za-z0-9]{6,})", s)
    if m:
        return m.group(1)
    # 3) 正文开头最长的一段连续中文/字母数字
    m = _re.search(r"[\u4e00-\u9fa5A-Za-z0-9]{6,}", s)
    return m.group(0) if m else ""


def message_age_seconds(time_text: str, now: datetime | None = None) -> int | None:
    """把会话列表里的时间文本换算成"距今多少秒"。

    闲鱼会话列表的时间格式（实测）：`刚刚` / `N分钟前` / `N小时前` / `昨天` /
    `N天前` / `MM-DD` / `HH:MM`（今天的消息只显示时分）。

    为什么要这个：引擎重启后内存去重集合清空，会把这**历史未读**会话当成
    新消息全部回一遍（2026-10-20 给一条 9 月的"你好"自动回复了）。有了
    消息年龄，就能拒绝回复几十小时前的旧消息。

    Args:
        time_text: 会话列表显示的时间文本。
        now: 当前时间（测试用），默认取系统时间。

    Returns:
        距今秒数；无法识别时返回 None（调用方应视为"不确定"，不自动回复）。
    """
    t = (time_text or "").strip()
    if not t:
        return None
    now = now or datetime.now()

    if "刚刚" in t:
        return 0
    m = re.search(r"(\d+)\s*分钟前", t)
    if m:
        return int(m.group(1)) * 60
    m = re.search(r"(\d+)\s*小时前", t)
    if m:
        return int(m.group(1)) * 3600
    if "昨天" in t:
        return 86400
    m = re.search(r"(\d+)\s*天前", t)
    if m:
        return int(m.group(1)) * 86400
    # 仅时分：今天内的消息。若该时刻晚于现在，说明是昨天的。
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", t)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if hh > 23 or mm > 59:
            return None
        cand = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if cand > now:
            cand -= timedelta(days=1)
        return max(0, int((now - cand).total_seconds()))
    # MM-DD：跨天日期，至少一天多
    if re.fullmatch(r"\d{2}-\d{2}", t):
        return 86400 * 2
    return None


def _parse_json(raw: Any) -> list | dict:
    """evaluate 返回值宽松解析：字符串按 JSON 解，列表/字典直接用。"""
    if isinstance(raw, str):
        import json
        try:
            return json.loads(raw)
        except Exception:
            return []
    if isinstance(raw, (list, dict)):
        return raw
    return []
