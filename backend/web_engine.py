"""网页端自动化引擎（替代/并行于 main.py）。

与 main.py 的区别：
- 配置、规则、卡密库存、运行日志全部读写 MySQL（xianyu_autoreply 库），
  因此网页端改完即时生效，无需改 yaml。
- 在「关键词自动回复」之外，新增「订单卡片检测 → 自动发货」：
  买家付款后读右侧买家面板「待发货」订单卡（状态 + 商品 + 订单号），
  按新卡卷系统（shop_items -> item_bundles -> card_bundles）发货。
  顺序铁律（用户 2026-10-18 明确）：先在卖家工作台完成平台确认发货
  （去发货 → 凭证弹窗什么都不填 → 确认发货），**成功之后才发卡密**。
- 持续写心跳到 engine_status，网页端据此显示「运行中/已停止」。
- 消费 commands 表：网页点「停止」即写入 stop 命令，引擎优雅退出。

复用 engine.py 的 XianyuIMEngine 完成 CDP 连接、扫描、打开会话、发送。
"""

from __future__ import annotations

import logging
import os
import re
import signal
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional

import win10_compat  # noqa: F401  (中文 Windows 控制台编码兜底)

win10_compat.setup()

import web_config as db
from engine import DEFAULT_SELECTORS, XianyuIMEngine, message_age_seconds

logger = logging.getLogger("web_engine")
_stop = False

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(level: str, msg: str) -> None:
    """同时写日志文件与 MySQL activity_log。"""
    _emit(level, msg)
    try:
        db.log_activity(level, msg)
    except Exception:  # noqa: BLE001
        pass


_LEVEL_MAP = {"WARN": "warning", "WARNING": "warning", "FATAL": "critical",
              "CRITICAL": "critical", "ERROR": "error", "INFO": "info",
              "DEBUG": "debug", "NOTSET": "info"}


def _emit(level: str, msg: str) -> None:
    """写日志，容忍 GBK 流。

    买家消息经常带 emoji / 泰文等非 GBK 字符；若 stdout 是 GBK 流
    （手动启动未固定编码时），直接写会抛 UnicodeEncodeError，把整条
    日志行丢掉（2026-10-17 实测）。失败时按流编码替换字符重试一次。
    """
    fn = getattr(logger, _LEVEL_MAP.get(level.upper(), "info"), logger.info)
    try:
        fn(msg)
    except Exception:  # noqa: BLE001
        try:
            enc = getattr(sys.stdout, "encoding", None) or "utf-8"
            fn(msg.encode(enc, "replace").decode(enc, "ignore"))
        except Exception:  # noqa: BLE001
            pass


# ============ 单实例保护 ============ #
def _other_engine_pids() -> list:
    """返回其他正在运行的 web_engine.py 进程 PID（查询失败返回空列表）。

    实现要点（2026-10-17 踩坑）：
    1. **绝不**用 subprocess 调 PowerShell 查 CIM——本机沙箱会在每次 spawn 时
       注入一个继承父命令行的 python 垫片，查询结果里永远有一个"看起来在跑
       web_engine.py 的自己人"，引擎被误判、干等 30 秒后放弃启动（连踩 3 次）。
    2. psutil 的进程枚举是进程内读取，不产生任何子进程，没有垫片污染。
    3. 但沙箱还会在引擎启动时注入幻影垫片，实测两种形态：
       a. 引擎自己的子进程（ppid = 引擎 pid，cmdline 相同）——按 ppid 排除；
       b. 与引擎同批诞生的"同胞"（ppid 指向某个包装进程，create_time 只比
          引擎早 1-2 秒，cmdline 相同）——按诞生时间差排除。
       真实的"其他引擎"必然早于本次启动很久（start_engine.ps1 先杀后起），
       故诞生时间差 < 10 秒的一律视为沙箱幻影，不是真引擎。
    4. 命令行里带 shell 元字符（&&、;、|）的一律是包装进程而非真引擎。
    """
    try:
        import psutil
    except ImportError:  # noqa: BLE001
        return []
    try:
        me = os.getpid()
        my_birth = psutil.Process(me).create_time()
        others = []
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                info = proc.info
                if info["pid"] == me:
                    continue
                if (info.get("name") or "").lower() != "python.exe":
                    continue
                cmd = " ".join(info.get("cmdline") or [])
                if "web_engine.py" not in cmd:
                    continue
                if any(ch in cmd for ch in ("&&", ";", "|")):
                    continue  # shell wrapper / tool harness, not a real engine
                if proc.ppid() == me:
                    continue  # sandbox phantom shim injected as my own child
                try:
                    if abs(proc.create_time() - my_birth) < 10.0:
                        continue  # sandbox launch sibling (same birth window)
                except Exception:  # noqa: BLE001
                    pass
                others.append(info["pid"])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return others
    except Exception:  # noqa: BLE001
        return []


def _describe_pids(pids: list) -> str:
    """诊断用：把候选 PID 的身份（ppid / 创建时间 / 命令行）格式化成字符串，
    用于守卫告警时判断对方是真引擎还是沙箱幻影垫片。"""
    try:
        import psutil
    except ImportError:  # noqa: BLE001
        return ""
    parts = []
    for pid in pids:
        try:
            p = psutil.Process(pid)
            created = datetime.fromtimestamp(p.create_time()).strftime("%H:%M:%S")
            cmd = " ".join(p.cmdline())
            parts.append(f"pid={pid} ppid={p.ppid()} created={created} cmd={cmd!r}")
        except Exception:  # noqa: BLE001
            parts.append(f"pid={pid} <gone>")
    return " | ".join(parts)


def _wait_single_instance(max_wait: float = 30.0) -> bool:
    """确保全局只有一个引擎：发现其他引擎先等待其退出（容忍重启竞态），
    超时仍存在则返回 False，由调用方放弃本次启动，避免重复回复。"""
    log("INFO", f"单实例检查开始 me={os.getpid()}")
    deadline = time.time() + max_wait
    announced = False
    while True:
        others = _other_engine_pids()
        if not others:
            return True
        if not announced:
            log("WARN", f"检测到其他引擎进程 {others}，等待其退出 ... "
                        f"{_describe_pids(others)}")
            announced = True
        if time.time() >= deadline:
            return False
        time.sleep(2)


# ============ 看门狗 ============ #
_progress = time.time()


def _touch() -> None:
    """标记主循环有进展（看门狗据此判断引擎是否已僵尸化）。"""
    global _progress
    _progress = time.time()


def _watchdog(stuck_sec: float = 300.0) -> None:
    """看门狗：主循环超过 stuck_sec 没有任何进展时，记 error 状态并强制退出。

    背景：CDP WebSocket 半死时，engine.py 里的调用（contexts / evaluate 等）
    没有超时保护，会永久阻塞——进程活着、0% CPU、永不回复（2026-10-17
    凌晨抓到两个这样的僵尸）。强制退出后由 start_engine.ps1 / 网页端重启。
    """
    while True:
        time.sleep(30)
        idle = time.time() - _progress
        if idle > stuck_sec:
            try:
                log("ERROR", f"看门狗：主循环 {int(idle)} 秒无进展，强制退出")
                db.set_engine_status("error")
            except Exception:  # noqa: BLE001
                pass
            os._exit(4)


# ============ 频率限制（与 main.py 同款，已修过 NameError） ============ #
class RateLimiter:
    def __init__(self, window: float = 3600.0, limit: int = 60,
                 per_user_limit: int = 3) -> None:
        self.window = window
        self.limit = limit
        self.per_user_limit = per_user_limit  # 每买家每小时上限（可实时调）
        self.enabled = True  # 频率限制总开关（settings.rate_limit_enabled，调试可关）
        self.sent: dict[str, deque[float]] = {}

    def allow(self, user: str) -> bool:
        # 调试模式：开关关闭时全部放行（防骚扰交给上线前再打开）
        if not self.enabled:
            return True
        now = time.time()
        # setdefault：新买家首次调用时自动建队列。
        # （历史 bug：直接用 self.sent[user]，普通 dict 查不存在的 key
        #  抛 KeyError，导致每个新买家的第一轮回复必崩。）
        dq = self.sent.setdefault(user, deque())
        while dq and now - dq[0] > self.window:
            dq.popleft()
        if len(dq) >= self.per_user_limit:
            return False
        total = sum(
            1 for q in self.sent.values() for ts in q if now - ts <= self.window
        )
        if total >= self.limit:
            return False
        dq.append(now)
        return True


# ============ 订单卡片检测（在已打开的会话里查文案） ============ #
# 聊天流里的系统订单卡片是 msg-dx-* 结构（实测 2026-10-17）：
#   msg-dx-title「我已付款，等待你发货」+ msg-dx-desc「该笔订单无需邮寄…」
#   + 「去发货」按钮。**卡片本身不含商品名**——商品名只在右侧买家面板的
#   「当前宝贝」里（见 CURRENT_ITEM_JS）。
# 优先精确提取订单卡片全文（存消息记录用）；msg-dx 结构万一改版，
# 退化为整个聊天区文案（只要含待发货标记就仍算检测到）。
ORDER_JS = r"""
() => {
  const markers = ['我已付款', '等待你发货', '无需邮寄', '去发货', '交易凭证'];
  const hit = (t) => markers.some(m => (t || '').indexOf(m) >= 0);
  const sels = ['[class*="msg-dx-content"]', '[class*="msg-dx"]'];
  for (const sel of sels) {
    let best = '';
    for (const el of document.querySelectorAll(sel)) {
      const t = (el.innerText || '').trim();
      if (t && hit(t) && t.length > best.length) best = t;
    }
    if (best) return best;
  }
  const candidates = [
    '[class*="message-list"]', '[class*="chat-content"]',
    '[class*="conversation-content"]', '[class*="msg-list"]',
    '[class*="im-content"]', 'main', 'body'
  ];
  let root = null;
  for (const sel of candidates) {
    const el = document.querySelector(sel);
    if (el) { root = el; break; }
  }
  if (!root) root = document.body;
  const txt = (root.innerText || '');
  for (const m of markers) { if (txt.indexOf(m) >= 0) return txt; }
  return '';
}
"""

# 右侧买家面板「当前宝贝」商品名（实测 2026-10-17 DOM）：
#   商品名元素 class 含 itemName；「当前宝贝」面板的特征是商品名下方
#   带 曝光/浏览/想要 数据行（itemData）。取可见元素，优先带曝光特征的，
#   兜底取第一个可见的（隐藏的 tab 面板 getBoundingClientRect 为 0 会被滤掉）。
# 注意：当前宝贝只是买家**正在看**的商品，不等于已付款的商品
# （实测 2026-10-18：买家付款后跑去看别的商品），仅作面板无数据时的兜底。
CURRENT_ITEM_JS = r"""
() => {
  const names = Array.from(document.querySelectorAll('[class*="itemName"]'))
    .filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; });
  if (!names.length) return '';
  for (const n of names) {
    let p = n.parentElement;
    for (let i = 0; i < 4 && p; i++, p = p.parentElement) {
      if ((p.innerText || '').indexOf('曝光') >= 0) return (n.innerText || '').trim();
    }
  }
  return (names[0].innerText || '').trim();
}
"""

# 右侧买家面板的订单卡列表（实测 2026-10-18 DOM）：
#   容器含 [class*="orderStatus"]（待发货/交易成功/交易关闭）+ itemName
#   + 订单编号（15+ 位数字）+ 付款时间。**这是判断订单实时状态的权威来源**：
#   聊天里的订单卡是历史消息（付款后也可能被退款成交），面板状态才是 truth。
#   同时返回 hasPanel：面板里有没有 itemName 元素（判断面板是否打开/加载）。
#   注意（实测 2026-10-18）：
#   - 同一张卡会同时命中包装层 orderStatusWrapper--* 与标签层 orderStatus--*
#     （class 都含 "orderStatus" 子串），按订单号去重，否则同一单处理两遍；
#   - **特殊订单**（虚拟商品，订单号 19~23 位）活跃时状态位显示的是
#     「特殊订单」徽标而非「待发货」（关闭后才显示「交易关闭」）——用
#     special 标出，由 _is_pending_shipment 结合付款证据判定；
#   - 特殊订单的付款时间上面板可能滞后，terminal 标出已完结/已退款的卡。
PENDING_ORDERS_JS = r"""
() => {
  const out = [];
  const seen = {};
  for (const st of document.querySelectorAll('[class*="orderStatus"]')) {
    const status = ((st.innerText || '').split('\n')[0] || '').trim();
    if (!status) continue;
    let p = st;
    for (let i = 0; i < 6 && p; i++, p = p.parentElement) {
      if (!p) break;
      const nameEl = p.querySelector('[class*="itemName"]');
      if (!nameEl) continue;
      const txt = (p.innerText || '').replace(/\s+/g, ' ');
      const m = txt.match(/(\d{15,})/);
      const oid = m ? m[1] : '';
      if (oid) {
        if (seen[oid]) break;   // 同一订单只记第一层（包装层）
        seen[oid] = 1;
      }
      const tm = txt.match(/付款\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})/);
      out.push({
        status: status,
        title: (nameEl.innerText || '').trim(),
        order_id: oid,
        paid_at: tm ? tm[1] : '',
        special: txt.indexOf('特殊订单') >= 0,
        terminal: /完结时间|退款成功/.test(txt)
      });
      break;
    }
  }
  return JSON.stringify({
    orders: out,
    hasPanel: document.querySelectorAll('[class*="itemName"]').length > 0
  });
}
"""

# ---------------------------------------------------------------------------
# 卖家工作台订单详情页（实测 2026-10-18 DOM）：
#   URL: ...#/seller-trade/order-manage/order-detail?orderId=<订单号>
#   「去发货」= button.ant-btn 文本去发货；点后右侧滑出 ant-drawer 抽屉
#   「交易凭证」：相关描述(选填 0/200) + 相关凭证(选填 0/3) + 确认发货按钮。
#   按用户 2026-10-18 明确要求：**一律不填任何内容，直接点确认发货**
#   （虚拟商品无寄件凭证，实测空内容可直接确认通过）。
#   顺序铁律：必须先平台确认发货成功，才允许发卡密——不点去发货钱不会
#   过户，平台超时还可能自动退款。
# ---------------------------------------------------------------------------
ORDER_DETAIL_URL = ("https://seller.goofish.com/?site=COMMONPRO"
                    "#/seller-trade/order-manage/order-detail?orderId={order_id}")

ORDER_STATE_JS = r"""
(oid) => {
  const t = document.body.innerText || '';
  let toast = '';
  for (const el of document.querySelectorAll('.ant-message, .ant-notification, [class*="toast"]')) {
    const s = (el.innerText || '').trim();
    if (s && s.length < 60) { toast = s; break; }
  }
  // 「去发货」主按钮坐标（页面主操作区，非弹窗内）——状态判断的**主信号**：
  // 订单详情页进度条会罗列全部步骤文本（待发货订单也含"待收货"字样），
  // waitReceive 正则对未发货订单也命中（2026-10-18 01:30 实测踩坑导致
  // 误判幂等、跳过真实确认直接发卡密）。按钮在 = 必须平台确认；按钮不在
  // = 已发货（用户手动点过或上轮已确认）。
  let shipBtn = null;
  for (const b of document.querySelectorAll('button.ant-btn')) {
    if ((b.innerText || '').trim() !== '去发货') continue;
    const r = b.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    shipBtn = {x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2)};
    break;
  }
  // 「直接免拼」按钮坐标（2人小刀订单专用）。
  // 只有卖家在闲鱼 App 里打开「促卖助手 → 促拼设置 → 支持免拼发货」后，
  // 未成团订单页才会出现这个按钮；点了它订单立刻跳过拼团进入待发货。
  // 实测文案是「直接免拼」，这里放宽到几种同义写法以防改文案。
  let freePinBtn = null;
  {
    const want = ['直接免拼', '免拼发货', '直接成团'];
    const cands = document.querySelectorAll('button, .ant-btn, [role="button"]');
    for (const b of cands) {
      const s = (b.innerText || '').trim();
      if (!s || s.length > 8) continue;
      if (!want.some(w => s.indexOf(w) >= 0)) continue;
      const r = b.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) continue;
      freePinBtn = {x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2),
                    text: s};
      break;
    }
  }
  return JSON.stringify({
    hasOrderId: t.indexOf(oid) >= 0,
    waitShip: /买家已付款|等待卖家发货/.test(t),
    waitReceive: /待收货|等待买家收货/.test(t),
    shipped: /已发货/.test(t),
    // 「2人小刀」拼团中（2026-10-19 订单 …9014861）：订单已付款但还差人成团，
    // 页面写「买家已付款,待刀成」「23时54分内未刀成将失败,等待其他人加入中」，
    // 此时**没有**「去发货」按钮，且属于正常的等待态，不是失败。
    waitGroup: /待刀成|我已小刀|未刀成|等待其他人加入/.test(t),
    shipBtn: shipBtn,
    freePinBtn: freePinBtn,
    drawerOpen: !!document.querySelector('.ant-drawer-open'),
    // 当前是否真的停在订单详情视图（URL 含 order-detail）。订单管理列表页有
    // 「待收货/已发货」筛选标签，会污染 waitReceive/shipped 判定，必须靠这个
    // 区分我们读到的是列表页还是真实订单详情（2026-10-18 @Fu先生 假成功根因）。
    onDetail: (location.hash || '').indexOf('order-detail') >= 0,
    toast: toast
  });
}
"""

# 「去发货」主按钮坐标（页面主操作区，非弹窗内）
ORDER_SHIP_BTN_JS = r"""
() => {
  for (const b of document.querySelectorAll('button.ant-btn')) {
    if ((b.innerText || '').trim() !== '去发货') continue;
    const r = b.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    return JSON.stringify({x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2)});
  }
  return JSON.stringify(null);
}
"""

# 抽屉弹窗内「确认发货」按钮坐标 + 禁用态（ant-drawer-open 容器内，
# 避免误点页面主操作区可能存在的同名按钮）
ORDER_CONFIRM_BTN_JS = r"""
() => {
  for (const b of document.querySelectorAll('.ant-drawer-open button')) {
    if ((b.innerText || '').trim() !== '确认发货') continue;
    const r = b.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    return JSON.stringify({x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2),
                           disabled: !!b.disabled});
  }
  return JSON.stringify(null);
}
"""


# ---------------------------------------------------------------------------
# 卖家「订单管理 / 待发货」页（权威订单来源，2026-10-18 修复后采用）
#   旧方案从 IM 聊天面板读订单卡，依赖闲鱼内部 CSS 类名
#   （[class*="itemName"] / [class*="orderStatus"]），而当前客户端已改为
#   CSS-module 哈希类名，选择器命中数为 0 —— 引擎永远读不到订单，表现就是
#   "无待发货订单"。且 IM 列表只虚拟渲染前 ~10 个会话，第 11 名之后的买家
#   根本扫不到。
#   新方案直接读「订单管理 / 待发货」页：完整列出全部待发货单（订单号 / 商品 /
#   买家 / 去发货按钮），纯文本可读，不依赖任何类名。平台确认发货复用已有的
#   confirm_platform_shipment（基于 get_by_role 角色定位，哈希类名无影响）。
# ---------------------------------------------------------------------------
ORDER_MANAGE_URL = ("https://seller.goofish.com/?site=COMMONPRO"
                    "#/seller-trade/order-manage")

ORDER_MANAGE_JS = r"""
() => {
  const t = (document.body.innerText || '').replace(/\s+/g, ' ');
  if (!/待发货/.test(t)) return JSON.stringify([]);
  // 不依赖任何 DOM 结构：直接在整页文本里按订单号(>=15位)切分订单块。
  // 每个订单块 = 本单订单号 到 下一单订单号 之间的文本，含商品/状态/买家/按钮。
  const idxs = [...t.matchAll(/(\d{15,})/g)].map(m => m.index);
  const blocks = [];
  const seen = {};
  for (let i = 0; i < idxs.length; i++) {
    const start = Math.max(0, (idxs[i] || 0) - 60);
    const end = (i + 1 < idxs.length) ? idxs[i + 1] : t.length;
    const block = t.slice(start, end);
    const m = block.match(/\d{15,}/);
    const oid = m ? m[0] : '';
    if (!oid || seen[oid]) continue;
    if (!/待发货/.test(block)) continue;   // 仅待发货
    seen[oid] = 1;
    blocks.push(block);
  }
  return JSON.stringify(blocks);
}
"""

_TITLE_RE = re.compile(
    r"下单时间[^\n]*?\s+(.*?)\s+待发货")
_BUYER_RE = re.compile(r"包邮\s+([^\s]+?)\s+联系ta")


def _parse_order_block(text: str) -> tuple[str, str]:
    """从订单块纯文本里抠出商品标题与买家昵称（不依赖任何 CSS 类名）。

    订单块形如：「… 添加备注 下单时间 2026-10-18 18:12:55 我的世界UI面板 待发货 …」
    _TITLE_RE 会连「下单时间」后的时间戳一起捕进标题，这里把前缀时间戳剥掉，
    只留纯商品名，否则发给 find_bundle_for_title 会带上日期导致匹配变脆弱。
    """
    title = ""
    m = _TITLE_RE.search(text)
    if m:
        title = (m.group(1) or "").strip()
        # 剥掉可能混入的下单时间前缀，如 "2026-10-18 18:12:55 "
        title = re.sub(
            r"^\d{4}-\d{1,2}-\d{1,2}[ T]\d{1,2}:\d{1,2}(?::\d{1,2})?\s+", "", title
        ).strip()
    b = _BUYER_RE.search(text)
    buyer = b.group(1).strip() if b else ""
    return title, buyer


_IM_DESKTOP_URL = ("https://seller.goofish.com/?site=COMMONPRO#/im-desktop")


def open_chat_by_order(engine: XianyuIMEngine, oid: str,
                       timeout: float = 12.0) -> bool:
    """用订单号在订单管理页点「联系ta」，直接打开该订单的聊天会话。

    为什么这是最优路径（2026-11-03 实测）：
    点「联系ta」后平台打开的是
        #/im?itemId=...&orderId=<订单号>&peerUserId=<买家ID>
    **订单号就在 URL 里**，这个"订单↔会话"对应关系是平台自己生成的：
      · 不依赖买家昵称解析（`_BUYER_RE` 抓错也不影响）
      · 不依赖商品标题（`_TITLE_RE` 要求"待发货"，已发货订单解析为空）
      · 平台保证对应关系 → 从根上不可能发错人

    旧实现（`open_conversation_for_order`）必须按昵称先筛出候选会话，
    昵称错一个字符就直接 return False，于是"能点发货、却永远发不出卡密"。
    "去发货"之所以一直成功，是因为它只用 orderId 拼 URL、不需要知道买家是谁。

    Args:
        engine: 引擎实例（需要可用浏览器）。
        oid: 目标订单号。
        timeout: 等待聊天页出现的最长秒数。

    Returns:
        True 表示已成功切到该订单的聊天页（`engine._page` 已指向它）。
    """
    oid = str(oid or "")
    if not oid:
        return False

    # 找任意一个已登录的卖家工作台页面（**不要用 _seller_page**：它会优先返回
    # 订单详情页/聊天页；而这里只需要一个能导航的 SPA 壳）
    browser = getattr(engine, "_browser", None)
    sp = None
    if browser is not None:
        for c in browser.contexts:
            for p in c.pages:
                u = p.url or ""
                if "seller.goofish.com" in u and "#/login" not in u:
                    sp = p
                    break
            if sp is not None:
                break
    if sp is None:
        log("WARN", f"  [联系ta] 找不到卖家工作台标签页，订单 {oid}")
        return False

    # 1) 切到订单管理页。
    #    【坑】这些路由只是同一个 SPA 的 hash（#/im → #/seller-trade/order-manage），
    #    page.goto() 对"仅 hash 不同"的 URL **不会触发整页导航**，SPA 也可能不响应，
    #    结果页面还停在聊天页、一个「联系ta」按钮都找不到（2026-11-03 实测）。
    #    所以 goto 之后必须校验 URL，没变就改用 JS 直接改 hash 再等渲染。
    try:
        if "order-manage" not in (sp.url or ""):
            sp.goto(ORDER_MANAGE_URL, wait_until="domcontentloaded", timeout=20000)
            time.sleep(2.0)
            if "order-manage" not in (sp.url or ""):
                sp.evaluate("location.hash = '#/seller-trade/order-manage'")
                time.sleep(2.5)
        if "order-manage" not in (sp.url or ""):
            log("WARN", f"  [联系ta] 没能切到订单管理页（当前 {sp.url}）")
            return False
    except Exception as exc:  # noqa: BLE001
        log("WARN", f"  [联系ta] 打开订单管理页失败: {exc}")
        return False

    # 订单列表是异步加载的：等到真的出现「联系ta」按钮再点，避免空列表误判
    for _ in range(10):
        try:
            n = sp.eval_on_selector_all(
                "button, .ant-btn, [role=button]",
                "els => els.filter(e => (e.innerText||'').trim() === '联系ta').length")
        except Exception:  # noqa: BLE001
            n = 0
        if n:
            break
        time.sleep(1.0)

    # 2) 从订单行里**直接读「联系ta」的 href**，不点它。
    #    实测它的真实结构是：
    #      <a href="https://seller.goofish.com/?site=COMMONPRO#/im
    #                ?itemId=...&orderId=<订单号>&peerUserId=<买家ID>">
    #        纸夜夜夜 联系ta
    #      </a>
    #    也就是"联系ta"本身就是个链接，href 由平台生成、里面带 orderId。
    #    比点击可靠得多：
    #      · 不需要 React 响应事件（合成 click 实测无效，真实鼠标点击也不稳）
    #      · 不需要坐标（页面滚动/遮挡都不影响）
    #      · href 里的 orderId 可以**校验**，从根上保证不发错人
    #
    #    注意：订单管理页是 ant-table，**每个订单占两行 <tr>**：
    #      第 1 行 = 已发货/订单编号/下单时间；第 2 行 = 商品/买家/按钮。
    #      所以要从"含订单号那一行"的**下一个兄弟行**里找链接。
    href = ""
    for attempt in range(3):
        try:
            href = sp.evaluate(r"""
            (oid) => {
              let infoRow = null;
              for (const tr of document.querySelectorAll('tr')) {
                if ((tr.textContent || '').indexOf(oid) >= 0) { infoRow = tr; break; }
              }
              if (!infoRow) return '';
              const scan = (root) => {
                for (const a of root.querySelectorAll('a[href]')) {
                  const h = a.getAttribute('href') || '';
                  if (h.indexOf('orderId=' + oid) >= 0) return h;
                }
                return '';
              };
              return scan(infoRow.nextElementSibling || infoRow) || scan(infoRow);
            }
            """, oid) or ""
        except Exception as exc:  # noqa: BLE001
            log("WARN", f"  [联系ta] 读取聊天链接异常（第 {attempt+1} 次）: {exc}")
            href = ""
        if href:
            break
        time.sleep(1.5)          # 列表可能还没渲染出这笔订单

    if not href:
        log("WARN", f"  [联系ta] 订单 {oid} 所在行里没有带 orderId 的聊天链接")
        return False

    # 3) 校验 + 跳转。href 里的 orderId 必须是目标订单，否则宁可不跳
    #    （列表里行与行挨得近，取错链接就等于发错人）
    if f"orderId={oid}" not in href:
        log("WARN", f"  [联系ta] 链接里的订单号与目标不符，拒绝跳转：{href[:90]}")
        return False

    if href.startswith("#"):
        href = "https://seller.goofish.com/?site=COMMONPRO" + href
    try:
        sp.goto(href, wait_until="domcontentloaded", timeout=20000)
    except Exception as exc:  # noqa: BLE001
        log("WARN", f"  [联系ta] 跳转聊天页失败: {exc}")
        return False

    # 4) 等聊天页就位：URL 带目标订单号 + 出现输入框，才交还给发卡密
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.4)
        if f"orderId={oid}" in (sp.url or ""):
            try:
                ready = sp.evaluate(
                    "() => { const b = document.querySelector("
                    "'[class*=\"sendbox\"] textarea'); "
                    "return !!(b && b.getBoundingClientRect().width > 0); }")
            except Exception:  # noqa: BLE001
                ready = False
            if ready:
                engine._page = sp
                log("INFO", f"  [联系ta] 已打开订单 {oid} 的聊天会话")
                return True

    log("WARN", f"  [联系ta] 跳转后没等到 orderId={oid} 的聊天输入框")
    return False


def _seller_page(engine: XianyuIMEngine):
    """找登录态的卖家工作台标签页（seller.goofish.com 且非 #/login）。

    实测（2026-10-18）：整个客户端只有**一个**卖家 SPA 标签页，IM 聊天只是
    它的 #/im-desktop 路由。旧逻辑「排除 im-desktop 再找工作台标签页」会
    扑空；而瞎抓第一个 goofish 标签会抓到 www.goofish.com 商品页（无卖家
    登录态），导航到订单详情直接被登录墙拦下（ship_fail 截图实锤）。
    优先返回已在订单详情页的标签，其次第一个合规标签。
    """
    browser = getattr(engine, "_browser", None)
    if browser is None:
        return None
    fallback = None
    try:
        for c in browser.contexts:
            for p in c.pages:
                u = p.url or ""
                if "seller.goofish.com" not in u or "#/login" in u:
                    continue
                if "order-detail" in u:
                    return p
                if fallback is None:
                    fallback = p
    except Exception:  # noqa: BLE001
        return None
    return fallback


def read_pending_orders(engine: XianyuIMEngine) -> list[dict]:
    """读「订单管理 / 待发货」页，返回 [{order_id, title, buyer}, ...]。

    用独立的卖家工作台标签页（_seller_page），**不动 engine._page 的路由**；
    读完把标签页还原到原地址——IM 会话列表只在 #/im-desktop 路由渲染，
    路由被订单页带走后 find_chat_page/open_conversation 都会失灵。
    """
    page = _seller_page(engine)
    if page is None:
        log("WARN", "读订单管理页失败: 未找到登录态卖家工作台标签页")
        return []
    orig_url = page.url or ""
    try:
        page.goto(ORDER_MANAGE_URL, wait_until="domcontentloaded",
                  timeout=20000)
    except Exception as exc:  # noqa: BLE001
        log("WARN", f"跳转到订单管理页失败: {exc}")
        return []
    try:
        # 等订单行渲染（最多 10s）。注意：「待发货」字样在筛选栏/页签就有，
        # 不能作为订单行已加载的依据（2026-10-18 实测：等到了字样但订单行
        # 还没出，解析得 0 单）。直接轮询解析结果非空才算就绪。
        import json as _json
        data: list = []
        for _ in range(20):
            time.sleep(0.5)
            try:
                blocks = page.evaluate(ORDER_MANAGE_JS) or "[]"
                data = _json.loads(blocks) if isinstance(blocks, str) \
                    else (blocks or [])
            except Exception:  # noqa: BLE001
                data = []
            if data:
                break
        if not data:
            return []  # 没等到订单行：多半是真没有待发货单
        out = []
        for t in data:
            oid_m = re.search(r"(\d{15,})", t)
            if not oid_m:
                continue
            title, buyer = _parse_order_block(t)
            out.append({"order_id": oid_m.group(1), "title": title,
                        "buyer": buyer})
        return out
    finally:
        # 还原路由：IM 会话列表只在 #/im-desktop 渲染，不能把工作台留在订单页
        if orig_url and orig_url != (page.url or ""):
            try:
                page.goto(orig_url, wait_until="domcontentloaded", timeout=10000)
            except Exception:  # noqa: BLE001
                pass


def resend_undelivered(engine: XianyuIMEngine) -> int:
    """补发「平台已确认但卡密从未送达」的订单卡密。

    为什么必须有这个兜底（2026-10-18 20:21 实测踩坑，用户 21:05 追问）：
    平台点「确认发货」成功后，该订单会**立刻移出「待发货」列表**，于是
    `ship_pending_from_manage` 再也扫不到它。而卡密发送如果这轮失败
    （会话没定位到/发送报错），就再也无人重试——结果就是**钱已过户、
    买家永远拿不到卡密**（实测订单 …025185 停在 pending 状态）。

    铁律（用户明确）：只要买家付了钱，卡密就**必须**送到。平台已确认
    说明钱已到账，此时任何失败都必须无限重试，不能「跳过」。

    Returns:
        本轮实际补发成功的订单数。
    """
    rows = db.list_undelivered_confirmed()
    if not rows:
        return 0
    log("INFO", f"[卡密补发] 有 {len(rows)} 笔已确认平台发货但卡密未送达，"
                f"开始补发："
                + "，".join(f"{r.get('buyer_name')}#{str(r.get('order_id'))[-6:]}"
                            for r in rows))
    sent_count = 0
    for r in rows:
        oid = r.get("order_id") or ""
        buyer = r.get("buyer_name") or ""
        title = r.get("product_name") or ""
        if not oid or not buyer:
            continue
        bundle = db.find_bundle_for_title(title)
        if not bundle:
            log("WARN", f"  [卡密补发] 订单 {oid} 商品「{title}」未绑定卡卷，无法补发")
            continue
        # 优先用订单号点「联系ta」（不依赖昵称），失败再回退按昵称定位
        opened = False
        try:
            opened = open_chat_by_order(engine, oid)
        except Exception as exc:  # noqa: BLE001
            log("WARN", f"  [卡密补发] 订单 {oid} 走「联系ta」异常: {exc}")
        if not opened and buyer:
            try:
                sp = _seller_page(engine)
                if sp is not None:
                    if "im-desktop" not in (sp.url or ""):
                        sp.goto(_IM_DESKTOP_URL, wait_until="domcontentloaded",
                                timeout=20000)
                        time.sleep(2)
                    engine._page = sp
                engine.find_chat_page()
                opened = engine.open_conversation_for_order(
                    buyer, oid, title_hint=title)
                if opened:
                    log("INFO", f"  [卡密补发] 订单 {oid} 回退按昵称定位成功")
            except Exception as exc:  # noqa: BLE001
                log("WARN", f"  [卡密补发] 回退按昵称定位失败: {exc}")
        if not opened:
            log("WARN", f"  [卡密补发] 订单 {oid} 两种方式都没打开会话，"
                        f"本轮不发，下轮重试（绝不发错人）")
            continue
        log("INFO", f"  [卡密补发] 订单 {oid} 正在补发卡密给「{buyer}」…")
        ok = _deliver(engine, buyer, f"[卡密补发 {oid}]",
                      payload=bundle["content"],
                      product_name=bundle["item_title"],
                      product_id=bundle["item_id"],
                      order_id=oid, via="卡卷-补发",
                      delivery_id=r.get("id"),
                      platform_confirmed=True)
        if ok:
            sent_count += 1
    return sent_count


def ship_pending_from_manage(engine: XianyuIMEngine) -> int:
    """扫「订单管理 / 待发货」页，对每笔待发货单执行「平台确认发货 → 发卡密」。

    复用 confirm_platform_shipment（去发货→不填凭证→确认发货）与 _deliver
    （发卡卷全文）。两级去重（订单号级 + 买家级 10 分钟）兜底防重复发货。
    本函数只负责"发现并派单"，具体状态机沿用 handle_order 的成熟路径：
    先确认平台发货、成功后才发卡密。

    Returns:
        本轮实际派单（进入平台确认+发卡密流程）的订单数。
    """
    orders = read_pending_orders(engine)
    if not orders:
        return 0
    log("INFO", f"[订单管理扫描] 发现 {len(orders)} 笔待发货订单："
                + "，".join(f"{o['buyer'] or '?'}#{o['order_id'][-6:]}" for o in orders))
    shipped = 0
    for o in orders:
        oid = o["order_id"]
        title = o["title"]
        buyer = o["buyer"]
        # 两级去重：订单号已发过则跳过
        rec = db.latest_delivery_for_order(oid)
        if rec and rec.get("platform_status") == "confirmed" \
                and rec.get("status") == "sent":
            log("INFO", f"  [订单管理] 订单 {oid} 已完成发货，跳过")
            continue
        # 1) 平台确认发货（基于角色定位，哈希类名无影响）
        if not (rec and rec.get("platform_status") == "confirmed"):
            log("INFO", f"  [订单管理] 订单 {oid} 平台确认发货中…")
            ok = confirm_platform_shipment(engine, oid)
            if not ok:
                # 2人小刀待刀成等"平台侧还没到时候"的情况：不算失败
                pending_reason = _SHIP_PENDING_REASON.pop(oid, "")
                if pending_reason:
                    log("INFO", f"  [订单管理] 订单 {oid} 暂不可发货：{pending_reason}；"
                                f"保持等待，不记为失败")
                    if not engine.dry_run and rec is None:
                        db.record_platform_result(oid, buyer, title, ok=False,
                                                  note=pending_reason)
                    continue
                log("WARN", f"  [订单管理] 订单 {oid} 平台确认失败，不发卡密，下轮重试")
                if not engine.dry_run:
                    if rec:
                        db.update_delivery(rec["id"], platform_status="failed",
                                           status="failed", note="平台发货确认失败")
                    else:
                        db.record_platform_result(oid, buyer, title, ok=False,
                                                  note="平台发货确认失败")
                continue
            if not engine.dry_run:
                if rec:
                    db.mark_platform_confirmed(rec["id"])
                else:
                    rec = {"id": db.record_platform_result(oid, buyer, title, ok=True)}
        # 2) 找卡卷（未绑定绝不发）
        bundle = db.find_bundle_for_title(title)
        if not bundle:
            log("WARN", f"  [订单管理] 订单 {oid} 商品「{title}」未绑定卡卷，"
                        f"请到网页端「商品列表」绑定")
            if not engine.dry_run:
                if rec:
                    db.update_delivery(rec["id"], status="failed", note="未绑定卡卷")
                else:
                    db.record_delivery(buyer, "", product_name=title,
                                       order_id=oid, status="failed",
                                       note="未绑定卡卷")
            continue
        # 3) 打开该订单的聊天会话，把卡密发给买家。
        #    **优先用订单号点「联系ta」**：平台会打开
        #    #/im?orderId=<订单号>&peerUserId=... 的聊天页，订单号就在 URL 里，
        #    完全不依赖昵称解析 —— 这就修掉了"每次都要等补发才能发出卡密"的病根
        #    （旧逻辑按昵称找会话，昵称 `通知消息` 之类抓错就永远发不出去）。
        #    失败才回退到老的按昵称定位（容错，应对「联系ta」按钮改版）。
        opened = False
        try:
            opened = open_chat_by_order(engine, oid)
        except Exception as exc:  # noqa: BLE001
            log("WARN", f"  [订单管理] 订单 {oid} 走「联系ta」异常: {exc}")

        if not opened and buyer:
            try:
                sp = _seller_page(engine)
                if sp is not None:
                    if "im-desktop" not in (sp.url or ""):
                        sp.goto(_IM_DESKTOP_URL, wait_until="domcontentloaded",
                                timeout=20000)
                        time.sleep(2)
                    engine._page = sp
                engine.find_chat_page()
                opened = engine.open_conversation_for_order(
                    buyer, oid, title_hint=title)
                if opened:
                    log("INFO", f"  [订单管理] 订单 {oid} 回退按昵称定位成功")
            except Exception as exc:  # noqa: BLE001
                log("WARN", f"  [订单管理] 回退按昵称定位失败: {exc}")

        if not opened:
            log("WARN", f"  [订单管理] 订单 {oid} 两种方式都没打开会话，"
                        f"本轮不发卡密，下轮重试（绝不发错人）")
            continue
        sent = _deliver(engine, buyer, f"[订单管理待发货单 {oid}]",
                        payload=bundle["content"],
                        product_name=bundle["item_title"],
                        product_id=bundle["item_id"],
                        order_id=oid, via="卡卷",
                        delivery_id=(rec or {}).get("id"),
                        platform_confirmed=True)
        if sent:
            shipped += 1
    return shipped


def order_state_verdict(st: dict) -> str:
    """根据 ORDER_STATE_JS 的结果判定订单状态。

    返回:
      "wait_ship" — 待发货，必须平台确认（有「去发货」按钮）
      "wait_group" — 2人小刀拼团中（待刀成），**正常等待态**，不发卡密也不算失败
      "shipped"   — 已发货/待收货，幂等跳过（无按钮 + 真实详情页佐证）
      "unknown"   — 状态不明，任何自动操作都不安全

    铁律：
    - 「去发货」按钮存在性 = 待发货的**唯一权威信号**（哈希类名无影响，且
      列表页/进度条都不会渲染这个按钮）。按钮在 → 必须平台确认。
    - **绝不**用 waitReceive（"待收货"）当已发货信号：订单详情页进度条会罗列
      全部步骤文本，待发货单页面也含"待收货"字样；订单管理列表页还有
      「待收货/已发货」筛选标签。两者都会让 waitReceive/shipped 误命中，
      把待发货单误判成已发货、跳过真实确认直接发卡密（钱不过户）。
      2026-10-18 @Fu先生 订单假成功就是这个坑：导航还没切到详情视图时，
      ORDER_STATE_JS 读到了列表页的"已发货"筛选项 → 假跳过。
    - 「待刀成」是**独立状态**，不能和 unknown 混为一谈：2026-10-19 订单
      …9014861 是 2人小刀单，买家已付款但还差 1 人成团，页面没有「去发货」，
      旧逻辑判成 unknown → 每轮都记「平台确认失败」，网页上显示成失败，
      制造大量假故障；实际它只是还没成团，成团后自然进待发货。
    - 只有「确实停在订单详情视图（onDetail）且页面出现"已发货"措辞且按钮不在」
      才视为已发货 → 幂等跳过。其余一律 unknown（不跳过、不盲点）。
    """
    if not isinstance(st, dict):
        return "unknown"
    if st.get("shipBtn") is not None:
        return "wait_ship"
    # 拼团中：页面明确写着"待刀成"，且确实在详情页（避免列表页文案污染）
    if st.get("onDetail") and st.get("waitGroup"):
        return "wait_group"
    # 按钮不在：只有"真实详情页 + 已发货措辞"才认作已发货，避免列表页筛选项污染
    if st.get("onDetail") and st.get("shipped"):
        return "shipped"
    return "unknown"


def _is_pending_shipment(o: dict, order_text: str = "") -> bool:
    """判断面板订单卡是否为「待发货」——含特殊订单识别。

    普通订单：状态位文本即「待发货」。
    特殊订单（虚拟商品）：实测 2026-10-18，活跃订单的状态位被
    「特殊订单」徽标占据（关闭后才显示「交易关闭」），且付款时间在
    面板上可能滞后数分钟——徽标 + 已付款证据才判待发货：
      - 已付款证据 = 面板卡有付款时间，或聊天订单卡文案含「我已付款」
        （聊天卡付款后秒推，比面板快，实测 14:32:54 付款 14:32:58 即到）；
      - 已完结/已退款（terminal）或状态已是终态的一律不算——未付款的
        特殊订单（只有下单时间）也不会误判。
    """
    status = (o.get("status") or "").strip()
    if status == "待发货":
        return True
    if not o.get("special") or o.get("terminal"):
        return False
    if status in ("交易关闭", "交易成功", "已退款", "已发货", "等待买家收货"):
        return False
    return bool(o.get("paid_at")) or "我已付款" in (order_text or "")


def detect_order(engine: XianyuIMEngine) -> str:
    """若当前打开的会话含待发货订单卡片，返回其文案；否则返回空串。"""
    try:
        if engine._page is None:
            return ""
        return engine._page.evaluate(ORDER_JS) or ""
    except Exception as exc:  # noqa: BLE001
        log("ERROR", f"订单检测异常: {exc}")
        return ""


def read_current_item(engine: XianyuIMEngine) -> str:
    """读右侧买家面板「当前宝贝」的商品名；读不到返回空串。

    面板数据随会话切换异步加载，首次读不到时短暂重试一次。
    """
    try:
        if engine._page is None:
            return ""
        title = (engine._page.evaluate(CURRENT_ITEM_JS) or "").strip()
        if not title:
            time.sleep(1.5)
            title = (engine._page.evaluate(CURRENT_ITEM_JS) or "").strip()
        return title
    except Exception as exc:  # noqa: BLE001
        log("ERROR", f"当前商品读取异常: {exc}")
        return ""


def read_panel_orders(engine: XianyuIMEngine) -> dict:
    """读右侧买家面板的订单卡列表。

    Returns:
        {"orders": [{status, title, order_id, paid_at}, ...],
         "hasPanel": 面板是否加载出内容}
    面板未打开/未加载时 orders 为空且 hasPanel=False，此时重试一次。
    """
    import json as _json

    def _once() -> dict:
        raw = engine._page.evaluate(PENDING_ORDERS_JS)
        data = _json.loads(raw) if isinstance(raw, str) else (raw or {})
        return {"orders": data.get("orders") or [],
                "hasPanel": bool(data.get("hasPanel"))}

    try:
        if engine._page is None:
            return {"orders": [], "hasPanel": False}
        panel = _once()
        if not panel["orders"] and not panel["hasPanel"]:
            time.sleep(1.5)
            panel = _once()
        return panel
    except Exception as exc:  # noqa: BLE001
        log("ERROR", f"订单面板读取异常: {exc}")
        return {"orders": [], "hasPanel": False}


def _ship_fail_dump(page, order_id: str, reason: str) -> None:
    """平台确认发货失败时留现场：截图 + 页面可见文本落到 LOG_DIR。

    2026-10-18 x***7 订单连败两次但日志只有一句「状态未变化」，没有任何
    现场证据可复盘。失败必须留证（截图 + innerText），否则下次报障仍是
    两眼黑。
    """
    import re as _re

    stamp = _re.sub(r"[^0-9A-Za-z]", "", str(order_id))[-12:] or "unknown"
    try:
        shot = LOG_DIR / f"ship_fail_{stamp}_{datetime.now():%H%M%S}.png"
        page.screenshot(path=str(shot), full_page=False)
        log("WARN", f"  失败现场截图: {shot}")
    except Exception as exc:  # noqa: BLE001
        log("WARN", f"  失败现场截图失败: {exc}")
    try:
        text = page.evaluate("() => (document.body && document.body.innerText) || ''")
        txt = LOG_DIR / f"ship_fail_{stamp}_{datetime.now():%H%M%S}.txt"
        txt.write_text(text or "", encoding="utf-8")
        log("WARN", f"  失败现场文本: {txt}")
    except Exception as exc:  # noqa: BLE001
        log("WARN", f"  失败现场文本抓取失败: {exc}")
    log("WARN", f"  平台发货确认失败 order={order_id} reason={reason}")


def _close_overlays(page) -> None:
    """清掉残留遮罩/抽屉/弹窗 DOM，避免挡住下一轮点击。

    上一轮失败后抽屉/遮罩可能还挂在页面上（动画没放完、点了没关干净），
    直接重来会被遮罩挡住导致再次打空。优先点关闭按钮，没有就摘节点。
    """
    try:
        page.evaluate(
            "() => {"
            " const roots = document.querySelectorAll("
            "   '.ant-drawer-open, .ant-drawer-mask, .ant-modal-wrap');"
            " roots.forEach(e => {"
            "   const close = e.querySelector("
            "     '.ant-drawer-close, .ant-modal-close');"
            "   if (close) { close.click(); }"
            "   else if (e.parentNode) { e.parentNode.removeChild(e); }"
            " });"
            "}")
    except Exception:  # noqa: BLE001
        pass


def _click_second_confirm(page) -> bool:
    """点掉可能出现的二次确认弹窗（.ant-modal-confirm）。

    点「确认发货」后平台可能再弹一个「确认要发货吗？」的 ant-modal-confirm
    （与抽屉 .ant-drawer 是两套 DOM，不点它发货不会真正提交）。
    返回 True=本回合点掉了某个确认按钮；False=没有可见弹窗。
    抽屉（.ant-drawer）不算弹窗——它的「确认发货」由主流程点。
    """
    try:
        visible = page.locator(".ant-modal-confirm:visible")
        for i in range(visible.count()):
            btn = visible.nth(i).locator("button:visible").last
            if btn.count() > 0:
                btn.click(timeout=3000)
                log("INFO", "  平台发货确认: 点掉二次确认弹窗")
                return True
        return False
    except Exception:  # noqa: BLE001
        return False


# 「平台侧还没到可发货状态」的原因（key=订单号）。
#
# 与"真失败"必须区分开：2人小刀订单在等成团时没有「去发货」按钮，如果按
# 失败处理，网页「订单管理」会把一笔**正常等待中**的订单标成红色"失败"，
# 而且每 3 分钟重试一次刷日志（2026-10-19 订单 …9014861 实测）。
# 调用方读这个 dict 决定：有值 → 只记 INFO、不落 failed；无值 → 按失败处理。
_SHIP_PENDING_REASON: dict[str, str] = {}

# 已经详细说明过的「待刀成」订单（避免每轮扫描重复刷同一段长提示）
_GROUP_HINTED: set[str] = set()


def _brief_err(exc: BaseException, limit: int = 60) -> str:
    """把 Playwright/pymysql 那种多行长报错压成一句能读的话。

    【为什么需要】Playwright 的异常是一大坨多行文本（含 `Call log:` 与调用栈），
    旧代码直接 `str(exc)[:80]` 硬截断，正好切在词中间，日志里就冒出
    `waiting for get_by_role`` 这种**半个符号**（2026-11-03 用户截图指出
    「多了个符号」）。这里改成：只取首行 → 去掉 Call log 之后的噪音 →
    截断时回退到最近的空白/标点 → 清掉尾部残留的引号括号。
    """
    text = str(exc or "").strip()
    if not text:
        return type(exc).__name__
    text = text.splitlines()[0].strip()
    for mark in ("Call log:", "Call log", "call log:"):
        if mark in text:
            text = text.split(mark)[0].strip()
    # 去掉 "Locator.click: " / "Page.goto: " 这类方法名前缀（对用户没意义）
    text = re.sub(r"^[A-Za-z_]+\.[A-Za-z_]+:\s*", "", text)
    # 把 Playwright 的超时原文翻成人话
    m = re.match(r"Timeout (\d+)ms exceeded", text)
    if m:
        text = "等待 %g 秒仍没出现" % (int(m.group(1)) / 1000)
    junk = " ;；,，:：-—`\"'）)】]》"
    text = text.rstrip(junk)
    if len(text) > limit:
        cut = text[:limit]
        for sep in (" ", "：", ":", "，", ",", "（", "("):
            pos = cut.rfind(sep)
            if pos > limit * 0.5:
                cut = cut[:pos]
                break
        text = cut.rstrip(junk) + "…"
    return text


def confirm_platform_shipment(engine: XianyuIMEngine, order_id: str) -> bool:
    """在闲鱼卖家工作台完成「去发货 → 凭证弹窗什么都不填 → 确认发货」。

    顺序铁律（用户 2026-10-18 明确）：必须先平台确认发货成功，才允许发
    卡密——不在平台点去发货，钱不会过户（买家已付款但卖家不确认，平台
    超时可能自动退款）。

    - 幂等：页面无「去发货」按钮且文本显示已发货/待收货（用户手动点过或
      上轮已确认）直接 True；按钮存在一律真实点击确认（进度条文本会误导，
      详见 ORDER_STATE_JS 注释）；
    - 本环境 Target.createTarget 被浏览器禁用（实测 Protocol error:
      Not supported），不能新开标签页——改为**复用用户已开的工作台标签页**
      （绝不用引擎轮询用的 im-desktop 页），用完还原原 URL；
    - 弹窗内一律不填任何内容（交易描述/相关凭证都是选填，虚拟商品无凭证），
      直接真实鼠标点击「确认发货」；
    - **两轮点击**：第一轮失败（含状态不明/弹窗未就绪/点击异常）→ 清残留
      遮罩 + 重载订单页 → 第二轮；两轮都败才 False。裸坐标点击在抽屉动画
      期间会打到空处（2026-10-18 x***7 订单连败教训），故一律用 locator
      点击（自带可见/稳定/可用检测）；
    - 轮询期间自动点掉二次确认弹窗（.ant-modal-confirm，与抽屉是两套 DOM）；
    - 失败即留现场：截图 + 页面 innerText 落 LOG_DIR（ship_fail_*.png/txt），
       reason 字段标明卡在哪一步；
    - 任何异常/超时/两轮后状态未变 → False，调用方不得发卡密。
    """
    import json as _json

    if not order_id:
        return False
    if engine.dry_run:
        log("INFO", f"  [试运行] 模拟平台确认发货 order={order_id}（不真正点击）")
        return True
    browser = getattr(engine, "_browser", None)
    if browser is None:
        log("WARN", "  平台发货确认: 无浏览器连接，跳过")
        return False

    # 找登录态的卖家工作台标签页。绝不能瞎抓第一个 goofish 标签——
    # www.goofish.com 商品页没有卖家登录态，导航到订单详情会被登录墙拦下
    # （2026-10-18 ship_fail 截图实锤：短信登录/扫码登录页）。
    page = _seller_page(engine)
    if page is None:
        log("WARN", "  平台发货确认: 未找到登录态卖家工作台标签页"
                   "（请保持闲鱼卖家工作台在浏览器中打开且已登录）")
        return False

    orig_url = page.url or ""
    try:
        # 两轮点击：第一轮失败 → 清残留遮罩 + 重载订单页 → 第二轮。
        # 2026-10-18 x***7 订单教训：单轮裸坐标连败两次、日志只有一句
        # 「状态未变化」无任何现场证据。裸坐标在抽屉弹出动画/遮罩出现的
        # 瞬间会打到空处，重来一次大多能成；两轮都败才认输并留证。
        round1_why = "未知原因"
        for attempt in (1, 2):
            if attempt == 2:
                log("INFO", f"  平台发货确认: 第 1 轮没成功（{round1_why}），"
                            f"重载订单页再试 order={order_id}")
                _close_overlays(page)
                page.goto(ORDER_DETAIL_URL.format(order_id=order_id),
                          wait_until="domcontentloaded", timeout=20000)
            # 等 SPA 真正切到**订单详情视图**（URL 含 order-detail）。只等
            # hasOrderId 不够——从订单管理列表 goto 详情是 SPA hash 切换，列表页
            # 文本（含"待收货/已发货"筛选项）会残留、订单号也匹配，会把列表页
            # 误读成"已发货"而假跳过（2026-10-18 @Fu先生 假成功根因）。
            st: dict = {}
            for _ in range(24):  # 12s
                time.sleep(0.5)
                st = _json.loads(page.evaluate(ORDER_STATE_JS, order_id))
                if st.get("hasOrderId") and st.get("onDetail"):
                    break
            else:
                if attempt == 1:
                    round1_why = "订单详情页 12 秒内没加载出来"
                    continue  # 没切到详情视图就重载再来一轮
                log("WARN", f"  平台发货确认: 订单页未切到详情视图 order={order_id}"
                            f"（导航异常？）")
                _ship_fail_dump(page, order_id, "not_on_detail")
                return False
            # 给「去发货」按钮一点渲染时间（最多 8s）。待发货单一定有此按钮；
            # 按钮在=必须确认；按钮不在再结合措辞判断，避免把渲染延迟误判成已发货。
            for _ in range(16):
                if st.get("shipBtn") is not None:
                    break
                time.sleep(0.5)
                st = _json.loads(page.evaluate(ORDER_STATE_JS, order_id))
            # 幂等/状态判定：以「去发货」按钮存在性为主信号（见 order_state_verdict）
            verdict = order_state_verdict(st)
            if verdict == "shipped":
                log("INFO", f"  平台发货确认: 订单 {order_id} 已是待收货/已发货，跳过")
                return True
            if verdict == "wait_group":
                # 2人小刀（拼团）订单：买家已付款但还差人成团，此时页面**没有**
                # 「去发货」，属于正常等待态而不是失败（2026-10-19 订单 …9014861）。
                # 若卖家已在闲鱼App开启「支持免拼发货」，页面会出现「直接免拼」，
                # 点一下即可跳过拼团直接成交 → 立刻进待发货 → 后续正常发卡密。
                fp = st.get("freePinBtn")
                if not fp:
                    _SHIP_PENDING_REASON[order_id] = "待刀成（2人小刀拼团中，等其他人加入）"
                    log("INFO", f"  平台发货确认: 订单 {order_id} 处于「待刀成」——"
                                f"2人小刀拼团中，还差人成团，本轮不发卡密（正常等待）")
                    if order_id not in _GROUP_HINTED:
                        _GROUP_HINTED.add(order_id)
                        # 2026-10-19 实测：闲鱼**PC 工作台没有「直接免拼」按钮**——
                        # 订单详情页只有「取消订单」，订单管理列表和 IM 订单卡也
                        # 都没有；整页扫描无任何"免拼/成团"元素。该按钮只在手机
                        # App 的订单页/消息/我卖出的页面才有。所以这里必须让用户
                        # 去手机点，不能提示"去开启免拼发货"（用户早就开了，会误导）。
                        log("INFO", f"  平台发货确认: 闲鱼电脑端不提供「直接免拼」，请到"
                                    f"手机App打开这笔订单点「直接免拼」（或等其他人参团）；"
                                    f"成团后订单会自动进入待发货，本软件随即自动发卡密")
                    return False
                btn_text = fp.get("text") or "直接免拼"
                log("INFO", f"  平台发货确认: 订单 {order_id} 待刀成，发现"
                            f"「{btn_text}」→ 点击直接免拼…")
                clicked = False
                try:
                    page.get_by_role("button", name=btn_text, exact=False).first.click(
                        timeout=8000)
                    clicked = True
                except Exception as exc:  # noqa: BLE001
                    # 该按钮有时是 div/span 而非 <button>，locator 命中不了，
                    # 退回坐标点击（位置在订单状态区，稳定，不会误点到别的操作）
                    try:
                        page.mouse.click(fp["x"], fp["y"])
                        clicked = True
                    except Exception as exc2:  # noqa: BLE001
                        log("WARN", f"  平台发货确认: 点「{btn_text}」失败"
                                    f" order={order_id}: {exc} / {exc2}")
                if not clicked:
                    if attempt == 1:
                        round1_why = "点「免拼」按钮失败"
                        continue
                    _ship_fail_dump(page, order_id, "click_free_pin")
                    return False
                # 免拼成功后订单应进入待发货 → 等「去发货」按钮出现
                got = False
                for _ in range(20):   # 10s
                    time.sleep(0.5)
                    st = _json.loads(page.evaluate(ORDER_STATE_JS, order_id))
                    if st.get("shipBtn") is not None:
                        got = True
                        break
                if not got:
                    log("WARN", f"  平台发货确认: 已点「{btn_text}」，但订单"
                                f" {order_id} 未出现「去发货」，下轮重试")
                    if attempt == 1:
                        round1_why = "点了免拼但没出现「去发货」"
                        continue
                    return False
                log("INFO", f"  平台发货确认: 订单 {order_id} 免拼成功，已进入待发货")
                verdict = "wait_ship"   # 继续走下面的正常确认流程
            if verdict != "wait_ship":
                # 状态不明：第一轮可能是陈旧渲染，重载再判一次；第二轮仍不明
                # 才收手（绝不敢在状态不明时点按钮）。
                if attempt == 1:
                    round1_why = "页面状态没判明（找不到「去发货」按钮）"
                    continue
                log("WARN", f"  平台发货确认: 订单 {order_id} 状态不明（无去发货按钮且"
                            f"文本未显示已发货），不确认")
                _ship_fail_dump(page, order_id, "unknown_verdict")
                return False
            # 点「去发货」——locator 点击自带可见/稳定/可用检测，不用裸坐标
            # （裸坐标在抽屉弹出动画期间会打到空处，2026-10-18 实测教训）
            #
            # 2026-10-20 加固：页面卡顿时 10s 不够（当天实测同一笔单 locator
            # 点击超时、连 screenshot 都超 3 秒），且按钮可能被吸底栏/动画层
            # 遮挡。所以先滚动到可见再点，超时放宽到 15s；仍失败就用 JS 直接
            # 触发 button.click() 兜底（React 合成事件能接住），避免整轮作废。
            try:
                ship_btn = page.get_by_role("button", name="去发货", exact=True).first
                try:
                    ship_btn.scroll_into_view_if_needed(timeout=3000)
                except Exception:  # noqa: BLE001
                    pass          # 滚动失败不影响后面的点击尝试
                ship_btn.click(timeout=15000)
            except Exception as exc:  # noqa: BLE001
                log("WARN", f"  平台发货确认: 点「去发货」按钮没成功"
                            f"（{_brief_err(exc)}），改用 JS 兜底 order={order_id}")
                js_ok = False
                try:
                    js_ok = bool(page.evaluate("""() => {
                      // 只点真正的 <button>，避免打到 div/span 外壳
                      for (const b of document.querySelectorAll('button')) {
                        if ((b.innerText || '').trim() !== '去发货') continue;
                        const r = b.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0) { b.click(); return true; }
                      }
                      return false;
                    }"""))
                except Exception as exc2:  # noqa: BLE001
                    log("WARN", f"  平台发货确认: JS 兜底也异常 order={order_id}: "
                                f"{str(exc2)[:60]}")
                if not js_ok:
                    if attempt == 1:
                        round1_why = "「去发货」按钮点不动（点击与 JS 兜底都失败）"
                        continue
                    _ship_fail_dump(page, order_id, "click_ship_btn")
                    return False
                log("WARN", f"  平台发货确认: 已用 JS 兜底点击「去发货」 order={order_id}")
            # 等抽屉弹窗内「确认发货」按钮就绪（最多 10s）
            ok_btn = None
            for _ in range(20):
                time.sleep(0.5)
                ok_btn = _json.loads(page.evaluate(ORDER_CONFIRM_BTN_JS))
                if ok_btn and not ok_btn.get("disabled"):
                    break
                ok_btn = None
            if not ok_btn:
                log("WARN", f"  平台发货确认: 凭证弹窗未就绪 order={order_id}")
                if attempt == 1:
                    round1_why = "确认弹窗没弹出来"
                    continue
                _ship_fail_dump(page, order_id, "drawer_not_ready")
                return False
            # 什么都不填，直接点抽屉内「确认发货」
            try:
                page.locator(".ant-drawer-open").get_by_role(
                    "button", name="确认发货", exact=True).first.click(timeout=10000)
            except Exception as exc:  # noqa: BLE001
                log("WARN", f"  平台发货确认: 点确认发货失败 order={order_id}: {exc}")
                if attempt == 1:
                    round1_why = "点「确认发货」没成功"
                    continue
                _ship_fail_dump(page, order_id, "click_confirm")
                return False
            # 轮询等结果（最多 15s）：「去发货」按钮消失 = 已离开待发货态
            # （确认成功 / 已被处理 / 已关闭），这是最可靠的主信号——不依赖任何
            # 文本正则（列表页筛选项、进度条"待收货"字样都会误命中）。
            # 期间顺手点掉可能出现的二次确认弹窗（.ant-modal-confirm）。
            success = False
            fail_toast = False
            for _ in range(30):
                time.sleep(0.5)
                st = _json.loads(page.evaluate(ORDER_STATE_JS, order_id))
                if st.get("shipBtn") is None:
                    success = True
                    break
                if "发货成功" in (st.get("toast") or ""):
                    success = True
                    break
                if "失败" in (st.get("toast") or "") and "成功" not in (st.get("toast") or ""):
                    fail_toast = True
                    break
                if _click_second_confirm(page):
                    continue
            if success:
                log("INFO", f"  平台发货确认: 订单 {order_id} 确认发货成功")
                return True
            log("WARN", f"  平台发货确认: 订单 {order_id} 点击后状态未变化"
                        f"（第 {attempt} 轮）")
            if fail_toast:
                # 平台明确返回失败，重试无意义，直接留证收手
                _ship_fail_dump(page, order_id, "fail_toast")
                return False
            # 第一轮失败 → 进入第二轮（循环继续）
        log("WARN", f"  平台发货确认: 订单 {order_id} 两轮均失败")
        _ship_fail_dump(page, order_id, "both_attempts_failed")
        return False
    except Exception as exc:  # noqa: BLE001
        log("WARN", f"  平台发货确认: 异常 order={order_id}: {exc}")
        _ship_fail_dump(page, order_id, f"exception:{exc}")
        return False
    finally:
        # 还原用户标签页原来的地址（我们只是借用）
        if page is not None and orig_url and orig_url != (page.url or ""):
            try:
                page.goto(orig_url, wait_until="domcontentloaded", timeout=10000)
            except Exception:  # noqa: BLE001
                pass


# ============ 处理逻辑 ============ #
def _safe_log(text: str, limit: int = 60) -> str:
    """日志脱敏：卡密链接 + 提取码绝不能原样写进日志文件。

    2026-10-18 事故：会话列表把我方卡密全文当买家消息读回，
    `log(f"[{user}] {text}")` 把完整网盘链接与提取码落盘。
    这里统一替换为占位符，只保留可读前缀便于排查。
    """
    s = (text or "").replace("\n", " ")
    s = re.sub(r"(https?://pan\.[\w.]+/s/)\S+", r"\1***", s)
    s = re.sub(r"(提取码[：: ]*)\S+", r"\1***", s)
    return s[:limit]


def _own_echo(text: str) -> bool:
    """判断一段"买家消息"其实是我方（卖家）自己发出去的内容。

    铁律（2026-10-18 22:05 用户截图指出「他什么都没说为什么一直在发，
    还把我的卡密也发出去了」）：会话列表预览是"最后一条消息"且不分方向，
    我方发出的卡密全文（网盘链接 + 提取码）会变成买家侧文本流进
    handle_reply → 命中「链接/提取码/卡密」关键词 → 给没付款的人反复自动回复。

    这是**最后一道兜底**，与 engine._looks_self_sent 互为 redundancy：
    那层靠文案启发式可能漏判，这里靠"内容指纹"（我方所有规则回复文本 +
    卡密模板特征）判定，漏判概率极低。任何命中都**只跳过回复**，绝不删卡。

    Args:
        text: 待判定的消息文本。

    Returns:
        像是我方自己发的返回 True。
    """
    if not text:
        return False
    body = text.strip()
    # 1) 卡密/网盘指纹：提取码、网盘链接
    if "提取码" in body or re.search(r"https?://pan\.[\w.]+/s/", body):
        return True
    # 2) 我方模板首句
    for marker in ("我用夸克网盘给你分享了", "感谢咨询，本店虚拟商品全部",
                   "发货内容含链接+提取码", "查收后麻烦确认收货"):
        if marker in body:
            return True
    # 3) 与任一启用规则的回复文本重合（前40 字足以覆盖模板差异）
    try:
        for r in db.list_rules():
            rep = (r.get("reply") or "").strip()
            if len(rep) >= 8 and rep[:40] in body:
                return True
    except Exception:
        pass
    return False


def handle_reply(engine: XianyuIMEngine, user: str, text: str,
                 limiter: RateLimiter, msg_age: int | None = None,
                 max_age: int = 1800) -> None:
    """按规则回复买家消息。

    Args:
        engine: 引擎实例。
        user: 买家昵称（用于日志与限流）。
        text: 触发匹配的消息文本（会话预览）。
        limiter: 频率限制器。
        msg_age: 该消息距今的秒数（会话列表时间换算）；None 表示无法判断。
        max_age: 允许自动回复的最大消息年龄，默认 1800 秒（30 分钟）。

    **铁律（2026-10-20 用户明确）**：不允许出现"买家没说话却自动回复"。
    引擎重启时内存去重集合会清空，于是把**历史未读**会话整批当成新消息
    回复（实测给一条 9 月的"你好"回了消息）。这里用消息年龄兜住：
    超过 max_age 的旧消息只记日志、不回复。无法判断年龄（时间没采到）
    时同样不回复——宁可漏回，也不能对几小时前的消息乱发。
    """
    if msg_age is None:
        # 用 WARN 而不是 INFO：时间采集依赖会话列表的 DOM，万一闲鱼改版导致
        # 采不到时间，自动回复会**整批失效**。INFO 太安静，容易被当成"没人发消息"。
        log("WARN", f"  [{user}] 未能识别会话时间，为稳妥不自动回复"
                    f"（若持续出现请检查会话列表结构）")
        return
    if msg_age > max_age:
        minutes = msg_age // 60
        log("INFO", f"  [{user}] 最后一条消息距今 {minutes} 分钟"
                    f"（超过 {max_age // 60} 分钟），不自动回复")
        return
    if _own_echo(text):
        log("INFO", f"  [{user}] 内容判定为我方回声（卡密/自身回复），跳过回复")
        return
    rule = db.match_rule(text)
    if not rule:
        log("INFO", f"  [{user}] 未命中规则，保持不回复")
        return
    if not limiter.allow(user):
        log("WARN", f"  [{user}] 触发频率限制，跳过")
        return
    if engine.send_reply(rule["reply"]):
        db.record_message(user, "in", text)
        db.record_message(user, "out", rule["reply"])
        db.inc_rule_hit(rule["id"])
        db.set_engine_status("running", pid=os.getpid(), last_activity=_now_iso())
        log("INFO", f"  [{user}] 已回复（规则#{rule['id']}）: {_safe_log(rule['reply'])}")
    else:
        log("WARN", f"  [{user}] 发送失败，下轮重试")


def handle_order(engine: XianyuIMEngine, user: str, order_text: str,
                 panel: dict = None) -> bool:
    """检测到待发货订单后的自动发货主流程。

    顺序铁律（用户 2026-10-18 明确）：**先在平台完成「去发货 → 凭证弹窗
    什么都不填 → 确认发货」，成功之后才发卡密**。不在平台点去发货钱不会
    过户（平台超时还可能自动退款）；先发卡密再补确认则买家可能拿码即退款。

    商品与订单状态以**右侧买家面板的订单卡**为权威来源（聊天里的订单卡
    只是历史消息，付款后也可能被退款；且会被虚拟列表滚出 DOM，实测
    2026-10-18 完全渲染不出来）。面板按新→旧列出全部订单，状态为
    「待发货」的每一笔按状态机处理：

    1. 平台已确认 + 卡密已发（status=sent）→ 跳过；
    2. 本地先确认有货可发：商品标题 -> shop_items -> item_bundles ->
       已启用 card_bundles。**未绑定卡卷绝不平台确认**——确认了钱就过账，
       发不出货更糟；记 failed + 警告，让用户去网页端补绑定；
    3. 平台未确认/确认失败 → confirm_platform_shipment()。失败则记
       failed + 警告，**不发卡密**，买家下条消息自然重试；
    4. 平台已确认 → 发卡卷全文，deliveries 落库（带 order_id）。
       发送失败留 pending，下轮只补发卡密、不重复平台确认（幂等）。

    面板无订单数据（未打开/未加载）时没有订单号、无法平台确认发货，
    按顺序要求**不发货**（历史 bug：曾按「当前宝贝」发货，买家没付款也发）。

    Returns:
        买家是否收到了（或模拟收到）发货内容。
    """
    # 防重复发货：**只按订单号判重，绝不按买家判重**。
    # 铁律（用户 2026-10-18 21:05 明确）：只要买家付款下单，无论此前是否发过
    # 同一人的其他订单，**这一笔都必须发卡密**——卡密是付费商品，不是自动回复。
    # 旧逻辑用 recent_delivery_for(user, 600) 做买家级 10 分钟去重，会把同一人
    # 的**新订单**一起挡掉（截图证据：日志「近期已发货，跳过重复发货」而买家
    # 刚下的那笔至今pending、卡密从未送达）。
    # 订单级判重在下面每个订单的处理里做（latest_delivery_for_order(oid)）。

    # 1) 权威来源：右侧面板订单卡（状态 + 商品 + 订单号）。
    #    普通订单状态=「待发货」；特殊订单状态位是徽标，见 _is_pending_shipment
    if panel is None:
        panel = read_panel_orders(engine)
    pendings = [o for o in panel["orders"] if _is_pending_shipment(o, order_text)]
    # 付款后面板可能还没异步刷出来：有聊天订单卡但面板暂无待发货时多重试两轮
    if not pendings and panel.get("hasPanel") and order_text:
        for _ in range(2):
            time.sleep(2.0)
            panel = read_panel_orders(engine)
            pendings = [o for o in panel["orders"] if _is_pending_shipment(o, order_text)]
            if pendings:
                break
    # 付款时间新的优先（面板本就新→旧，排序只是保险）
    pendings.sort(key=lambda o: o.get("paid_at") or "", reverse=True)
    shipped = False
    if pendings:
        for p in pendings[:5]:
            oid = p.get("order_id") or ""
            title = p.get("title") or ""
            rec = db.latest_delivery_for_order(oid) if oid else None
            already_sent = bool(rec and rec.get("status") == "sent")
            # 1) 已完成（平台确认 + 卡密已发）→ 跳过
            if already_sent and rec.get("platform_status") == "confirmed":
                log("INFO", f"  [{user}] 订单 {oid} 已完成发货，跳过")
                continue
            # 2) 本地先确认有货可发（没绑定卡卷绝不平台确认）
            bundle = db.find_bundle_for_title(title)
            if not bundle:
                log("WARN", f"  [{user}] 待发货订单 {oid or '（无订单号）'} 商品「{title}」"
                           f"未绑定卡卷（卡密库也无库存），请到网页端「商品列表」给该商品绑定卡卷")
                if not engine.dry_run:
                    if rec:
                        db.update_delivery(rec["id"], status="failed", note="未绑定卡卷")
                    else:
                        db.record_delivery(user, "", product_name=title,
                                           order_id=oid or None, status="failed",
                                           note="未绑定卡卷")
                continue
            # 3) 平台确认发货（未确认/确认过但失败才执行）
            if not (rec and rec.get("platform_status") == "confirmed"):
                log("INFO", f"  [{user}] 订单 {oid} 平台确认发货中"
                            f"（去发货 → 不填凭证 → 确认发货）...")
                ok = confirm_platform_shipment(engine, oid)
                if not ok:
                    # 平台侧"还没到可发货状态"（如 2人小刀待刀成）不算失败：
                    # 只记 INFO，不把订单染色成 failed，也不每轮重复告警。
                    pending_reason = _SHIP_PENDING_REASON.pop(oid, "")
                    if pending_reason:
                        log("INFO", f"  [{user}] 订单 {oid} 暂不可发货：{pending_reason}"
                                    f"；保持等待，不记为失败")
                        if not engine.dry_run and rec is None:
                            db.record_platform_result(oid, user, title, ok=False,
                                                      note=pending_reason)
                        continue
                    log("WARN", f"  [{user}] 订单 {oid} 平台发货确认失败，钱不会到账；"
                                f"按顺序要求不发卡密，下轮自动重试")
                    if not engine.dry_run:
                        if rec:
                            db.update_delivery(rec["id"], platform_status="failed",
                                               status="failed", note="平台发货确认失败")
                        else:
                            db.record_platform_result(oid, user, title, ok=False,
                                                      note="平台发货确认失败")
                    continue
                if not engine.dry_run:
                    if rec:
                        db.mark_platform_confirmed(rec["id"])
                    else:
                        rec = {"id": db.record_platform_result(oid, user, title, ok=True)}
                # 点完「确认发货」SPA 路由被带到订单详情页，聊天输入框随之消失。
                # 必须回到聊天页并重新打开该买家会话，否则 _deliver 直接报
                # 「未找到输入框: [class*='sendbox'] textarea」→ 钱已过户、卡密
                # 发不出去（2026-10-19 00:35 实测：一口小猫头 订单…068555）。
                # scan_chat_paid_orders 里早有这一步，这条路径漏了。
                try:
                    # 与其他三条路径保持一致：订单号直达优先，昵称只做兜底
                    opened = open_chat_by_order(engine, oid)
                    if not opened:
                        engine.find_chat_page()
                        opened = engine.open_conversation_for_order(user, oid)
                    if not opened:
                        log("WARN", f"  [{user}] 订单 {oid} 平台已确认，但未能回到"
                                     f"该订单所在会话，本轮不发卡密（补发队列兜底）")
                        continue
                except Exception as exc:  # noqa: BLE001
                    log("WARN", f"  [{user}] 订单 {oid} 回聊天页失败: {exc}"
                                 f"（补发队列兜底）")
                    continue
            if already_sent:
                # 历史记录：卡密早已发过（平台确认是旧的/缺的），只补确认不重发
                log("INFO", f"  [{user}] 订单 {oid} 卡密早已发过，仅补平台确认，不重发")
                continue
            # 4) 平台已确认 → 发卡密（发送失败留 pending，下轮只补发）
            sent = _deliver(engine, user, order_text,
                            payload=bundle["content"],
                            product_name=bundle["item_title"],
                            product_id=bundle["item_id"],
                            order_id=oid, via="卡卷",
                            delivery_id=(rec or {}).get("id"))
            shipped = shipped or sent
        return shipped

    # 面板有内容但没有待发货订单：已发货/已关闭/已退款，不能发
    if panel["hasPanel"]:
        log("INFO", f"  [{user}] 无待发货订单（已发货/已关闭/已退款），不自动发货")
        return False

    # 面板无数据（未打开/未加载）：没有订单号就无法平台确认发货，
    # 按顺序要求不发货
    log("WARN", f"  [{user}] 右侧面板无订单数据（请保持闲鱼客服软件右侧"
                f"「买家信息」面板打开）；无订单号无法平台确认发货，"
                f"按顺序要求不自动发卡密")
    return False


def _deliver(engine: XianyuIMEngine, user: str, order_text: str,
             payload: str, product_name: str, product_id, order_id: str = None,
             via: str = "卡卷", delivery_id=None,
             platform_confirmed: bool = False) -> bool:
    """把发货内容发给买家并落库。payload 已由调用方取得。

    delivery_id 为平台确认时落的记录 id（status='pending'）——发送成功后
    把该行更新为 sent；没有则新插一行（兜底路径，无订单号）。

    Returns:
        发送成功（或试运行模拟）返回 True，发送失败返回 False。
    """
    if engine.dry_run:
        log("INFO", f"  [试运行] 命中{via}「{product_name}」，将发送: {_safe_log(payload)}"
                   f"（不真正发送）")
        return True
    if engine.send_reply(payload):
        db.record_message(user, "in", order_text or "[面板待发货订单]", is_order=1)
        db.record_message(user, "out", payload)
        if delivery_id:
            db.update_delivery(delivery_id, status="sent", code=payload,
                               product_id=product_id, product_name=product_name,
                               note=f"via {via}")
        else:
            db.record_delivery(user, payload, product_id=product_id,
                               product_name=product_name, status="sent",
                               order_id=order_id, note=f"via {via}",
                               platform_status="confirmed" if platform_confirmed else None)
        db.set_engine_status("running", pid=os.getpid(), last_activity=_now_iso())
        log("INFO", f"  [{user}] 已自动发货（{via}: {product_name}）")
        return True
    log("WARN", f"  [{user}] 发货内容发送失败，下轮补发（平台已确认，不会重复确认）")
    return False


def process(engine: XianyuIMEngine, unread: list, handled: dict,
            limiter: RateLimiter, window_sec: int = 120) -> None:
    global _stop
    for conv in unread:
        if _stop:
            break
        _touch()
        if engine.is_duplicate(conv.user, conv.text):
            # 回声（自己发过的内容成了预览）：更新基线防下轮重复纳入
            engine._last_active = (conv.user, conv.preview)
            continue
        marker = (conv.user, conv.text)
        now = time.time()
        # 频率限制开启：同文本 window_sec 内不重处理（防扫描抖动），追问放行；
        # 关闭（调试模式）：缩到 5 秒，允许同一消息反复触发以方便测试，
        # 但仍保留最小窗口防止同一轮扫描内一瞬双开。
        ttl = max(0, int(window_sec)) if limiter.enabled else 5
        if marker in handled and now - handled[marker] < ttl:
            # 去重窗口内：**不更新激活会话基线**——否则买家追问的相同文本
            # 会"污染"基线，之后扫描层再也看不见这个会话（历史 bug：
            # 只回复一次，后续相同内容永远不回）。窗口过期后自然补处理。
            continue

        log("INFO", f"[{conv.user}] {_safe_log(conv.text)}")
        db.upsert_conversation(conv.user, conv.preview, 1, conv.text, _now_iso())

        if not engine.open_conversation(conv.user, conv.text):
            # 打开失败：不记 handled、不更新基线，下轮重试
            continue
        # 成功进入处理流程：此刻起更新激活会话基线
        engine._last_active = (conv.user, conv.preview)
        # 标记已翻看：定时扫描在一个间隔内不重复翻这个人（降入侵）
        _scan_seen[conv.user] = time.time()

        # 触发器双层设计（2026-10-18 实测）：
        # 1. 聊天流订单卡（msg-dx）——付款瞬间的最新消息，必然渲染，实时触发；
        # 2. 右侧面板「待发货」订单——权威状态，但聊天卡会被虚拟列表滚出
        #    DOM（实测完全渲染不出来），所以每条买家消息都顺带查一次面板，
        #    保证待发货订单不依赖聊天卡是否在可视窗口。
        #    面板判定用 _is_pending_shipment：普通单看「待发货」，特殊单
        #    （虚拟商品）状态位是「特殊订单」徽标，要结合付款证据判断。
        # 消息年龄：会话列表显示的时间（"10小时前"/"09-21"…）换算成秒。
        # 只用于**关键词回复**把关；订单发货不受此限制（买家几小时前付款，
        # 现在发现待发货订单照样要发，不能因为消息旧就不发货）。
        msg_age = message_age_seconds(getattr(conv, "time_text", "") or "")

        order_text = detect_order(engine)
        panel = read_panel_orders(engine)
        has_pending = any(_is_pending_shipment(o, order_text)
                          for o in panel["orders"])
        if order_text or has_pending:
            if not handle_order(engine, conv.user, order_text, panel):
                # 没发成货（未绑定/无库存/已退款）也别让买家干等：走关键词回复
                handle_reply(engine, conv.user, conv.text, limiter, msg_age)
        else:
            handle_reply(engine, conv.user, conv.text, limiter, msg_age)

        handled[marker] = now

    if len(handled) > 500:
        cutoff = time.time() - 120
        handled = {k: v for k, v in handled.items() if v > cutoff}


# ============ 定时扫描（静默买家捞单） ============ #
# 买家昵称 -> 最近一次翻看时间戳。正常处理路径（process）与定时扫描
# （scan_pending_orders）共用：一个扫描间隔内不重复翻同一个人，降低
# 会话列表翻动频率（用户 2026-10-18 要求"不要显得特别入侵"）。
_scan_seen: dict[str, float] = {}


def _heartbeat_thread() -> None:
    """后台心跳线程：独立 DB 连接，每 5s 无条件续一次 engine_status 心跳。

    主循环自己的心跳每 3s 一次，但主线程可能被任何 CDP 调用阻塞
    （open_conversation、page.evaluate 无超时保护，2026-10-18 实测
    单个会话可阻塞十几秒；扫描轮次开始前的 current_active /
    list_conversations 阶段也实测过 24s 无心跳）。期间没有任何心跳，
    网页仪表盘 15s 阈值、桌面控制台 45s 阈值都会误判引擎已停止。

    本线程常开，覆盖主线程一切停顿（扫描轮次、CDP 停滞、connect
    等待）。语义上显示层只回答"引擎进程活着吗"——引擎真死由看门狗
    的"无进展强杀"负责，两者阈值不同、职责分离。
    """

    hb = None
    while True:
        time.sleep(5)
        try:
            if hb is None or not hb.open:
                # 独占连接（带远程→本地回退），绝不碰主线程的全局连接
                hb = db.new_conn()
            cur = hb.cursor()
            cur.execute(
                "INSERT INTO engine_status (id, state, pid, last_heartbeat) "
                "VALUES (1,'running',%s,NOW()) "
                "ON DUPLICATE KEY UPDATE state='running', pid=%s, "
                "last_heartbeat=NOW()",
                (os.getpid(), os.getpid()),
            )
            cur.close()
        except Exception:  # noqa: BLE001
            hb = None  # 连接坏了下次重连；心跳失败不致命


def _scan_settings(settings: dict) -> tuple[bool, int, int]:
    """解析定时扫描设置（带边界保护）。

    Returns:
        (是否开启, 范围[0=全部], 间隔秒)。间隔钳制在 [30, 3600]：
        下限 30s——每翻一个会话约 3s（点击+等渲染+读面板），间隔太小
        只会把界面刷爆没有意义；上限 1h——防误填天量数值导致永远不扫。
        范围负数按 0（全部）处理，非数字回落默认 10。
    """
    on = bool(int(settings.get("scan_enabled") or 0))
    # 注意区分「显式 0=全部」与「key 缺失」：缺失回落默认 10，
    # 缺失时若按 0（全部）处理，配置没写完的库一开扫就是几十个会话。
    raw_scope = settings.get("scan_scope")
    try:
        scope = 10 if raw_scope is None else max(0, int(raw_scope))
    except (TypeError, ValueError):
        scope = 10
    raw_interval = settings.get("scan_interval")
    try:
        interval = 180 if raw_interval is None else int(raw_interval)
    except (TypeError, ValueError):
        interval = 180
    return on, scope, max(30, min(3600, interval))


# 聊天消息区里的订单卡（2026-10-18 21:21 用户思路的实现）
#
# 用户原话：「直接检测是从哪个聊天框出现『我已拍下，待付款』/『我已付款，
# 等待你发货』这些信息然后进行判断，第一个是下单未付款，第二个是已付款，
# 检测到已付款然后点击发货然后再发卡密，发卡密的时候看一下订单是在哪个
# 聊天框在发出去」。
#
# 为什么这套比"订单管理页 + 按昵称找会话"可靠：
# 1. **订单卡自带订单号** → 订单与会话是**同一条消息**里的一一对应关系，
#    完全不需要靠昵称/闲鱼号去猜哪个话题才是这次订单的；
# 2. 聊天卡天然区分「待付款」和「已付款」两种状态（订单管理页筛选栏两者
#    都叫"待发货"，容易混）；
# 3. 消息列表是**虚拟渲染**的：只有视口附近的消息在 DOM 里，必须把消息
#    列表往上滚一屏才能把上次的订单卡读出来。
#
# 结构定位（不依赖哈希类名，闲鱼改版也会失效）：
# 消息列表 = 会话列表右侧（x≥320）、可纵向滚动、div 数量最多的那个容器。
CHAT_ORDER_CARDS_JS = r"""
() => {
  // 1) 定位**消息列表**容器：会话列表右侧、可纵向滚动，且**内部还会再滚动**
  //    （sh/ch 比值最大者= 真消息列表；chat-main-view 是整个右侧面板，
  //    它也"可滚动"，div 数量最多但不是消息列表 —— 实测踩坑）
  let ml = null, bestRatio = 0;
  for (const e of document.querySelectorAll('div')) {
    const r = e.getBoundingClientRect();
    if (r.x < 320 || r.width < 380 || r.height < 150) continue;
    const inner = e.querySelectorAll('div').length;
    if (inner < 5) continue;              // 太"空"的不是消息列表
    const ratio = e.scrollHeight / (e.clientHeight || 1);
    if (ratio > bestRatio) { bestRatio = ratio; ml = e; }
  }
  if (!ml) return JSON.stringify({ok: false, cards: []});

  // 2) 订单卡：含「我已付款 / 我已拍下 / 等待卖家发货」的气泡。
  //    取**文本最短的那个命中元素**当卡片代表（祖先的innerText 包含子元素、
  //    会重复嵌套，用「子元素命中且文本更短」来剔除祖先）。
  const KEY = /我已付款|我已拍下|等待卖家发货/;
  let mlText = ml.innerText || '';
  const hits = [];
  for (const e of ml.querySelectorAll('*')) {
    const t = e.innerText || '';
    if (!KEY.test(t)) continue;
    const r = e.getBoundingClientRect();
    if (r.width < 80 || r.height < 30) continue;
    let keep = true;
    for (const c of e.children) {
      const ct = c.innerText || '';
      // 子元素也命中关键词、且文本更短（说明它才是真正的卡片本体）
      if (KEY.test(ct) && ct.length < t.length) { keep = false; break; }
    }
    if (!keep) continue;
    hits.push(e);
  }
  const mlRect = ml.getBoundingClientRect();
  // 同一张卡会被其多个子元素重复命中 → 按「文本+几何位置」去重
  const seen = new Set();
  const cards = [];
  for (const e of hits) {
    const flat = (e.innerText || '').replace(/\s+/g, ' ').trim();
    const r = e.getBoundingClientRect();
    // 同一张卡：文本相同且垂直位置几乎相同（±4px）
    let dup = false;
    for (const s of seen) {
      if (s.txt === flat && Math.abs(s.y - r.y) <= 4) { dup = true; break; }
    }
    if (dup) continue;
    seen.add({txt: flat, y: r.y});
    cards.push({
      paid: /我已付款|等待卖家发货/.test(flat),
      unpaid: /我已拍下/.test(flat),
      has_ship_btn: /去发货/.test(flat),
      from_buyer: (r.x + r.width / 2) < (mlRect.x + mlRect.width / 2),
      text: flat.slice(0, 220)
    });
  }
  return JSON.stringify({ok: true, cards: cards, hits: hits.length,
                         mlTextLen: mlText.length});
}
"""

# 把消息列表往上滚（订单卡是虚拟渲染，视口外不在 DOM 里）
CHAT_SCROLL_UP_JS = r"""
(dy) => {
  let ml = null, bestRatio = 0;
  for (const e of document.querySelectorAll('div')) {
    const r = e.getBoundingClientRect();
    if (r.x < 320 || r.width < 380 || r.height < 150) continue;
    if (e.querySelectorAll('div').length < 5) continue;
    const ratio = e.scrollHeight / (e.clientHeight || 1);
    if (ratio > bestRatio) { bestRatio = ratio; ml = e; }
  }
  if (!ml) return JSON.stringify({ok: false});
  const before = ml.scrollTop;
  ml.scrollTop = Math.max(0, ml.scrollTop - dy);
  return JSON.stringify({ok: true, before: before, after: ml.scrollTop,
                         max: ml.scrollHeight - ml.clientHeight});
}
"""


def read_chat_order_cards(engine: XianyuIMEngine) -> list[dict]:
    """读**当前会话**聊天消息区里的订单卡（上翻一屏以穿破虚拟渲染）。

    注意（2026-10-18 21:35 实测）：闲鱼的订单卡 DOM（`msg-dx-*`）**本身不含
    订单号**（实测 oids=[]），只有「我已付款/我已拍下/去发货」文案。所以这张卡
    的作用是回答两个问题：
      - 这个会话里**有没有**买家刚下的单；
      - 那单**付了没**（已付款 vs 待付款）、**平台确认了没**（卡上还有没有
        「去发货」按钮）。
    订单号从**右侧买家面板**取（`read_panel_orders`），而右侧面板读的正是
    **当前会话**这个买家的订单——于是「订单 ↔ 会话」靠"同一次打开"绑定，
    不需要跨会话按昵称猜。

    Returns:
        [{"paid": bool, "unpaid": bool, "has_ship_btn": bool,
          "from_buyer": bool, "text": str}, ...]；无卡片返回 []。
    """
    import json as _json
    page = engine._page
    if page is None:
        return []
    # 往上翻一屏：订单卡通常在上一条消息附近，虚拟列表只渲染视口附近的项
    for _ in range(2):
        try:
            page.evaluate(CHAT_SCROLL_UP_JS, 600)
            time.sleep(0.6)
        except Exception as exc:  # noqa: BLE001
            log("WARN", f"上翻消息列表失败: {exc}")
            break
    try:
        raw = page.evaluate(CHAT_ORDER_CARDS_JS) or "{}"
        data = _json.loads(raw) if isinstance(raw, str) else raw
    except Exception as exc:  # noqa: BLE001
        log("WARN", f"解析聊天订单卡失败: {exc}")
        return []
    out: list[dict] = []
    for c in (data.get("cards") or []):
        out.append({
            "paid": bool(c.get("paid")),
            "unpaid": bool(c.get("unpaid")),
            "has_ship_btn": bool(c.get("has_ship_btn")),
            "from_buyer": bool(c.get("from_buyer")),
            "text": c.get("text") or "",
        })
    return out


def _guess_title(card_text: str) -> str:
    """从聊天订单卡文案里猜商品标题（兜底用）。

    卡面典型文案：「…我的世界UI面板，模型，支持Oraxen ¥1.00 [共1件] 成交价…」，
    商品名在「¥」前面。取不到就返回空串——宁可让用户去绑定，也不猜错商品
    （猜错会把A 商品的卡密发给买了 B 商品的买家）。

    Args:
        card_text: 订单卡纯文本。

    Returns:
        猜测的商品标题；猜不到返回 ""。
    """
    if not card_text:
        return ""
    m = re.search(r"(.+?)\s*[¥￥]\s*[\d.]+", card_text)
    if m:
        t = (m.group(1) or "").strip()
        # 去掉时间戳前缀（"2026-10-18 20:19:03 我的世界UI面板…"）
        t = re.sub(r"^\d{4}-\d{2}-\d{2}\s*\d{2}:\d{2}:\d{2}\s*", "", t).strip()
        return t
    return ""


def scan_chat_paid_orders(engine: XianyuIMEngine,
                         scope: int = 10) -> int:
    """按用户设计的主流程：逐个会话读聊天订单卡 → 已付款的走发货全流程。

    完整链路（2026-10-18 21:21 用户明确要求）：
        1. 逐个打开会话，**只读聊天消息区**里的订单卡；
        2. 卡上「我已付款，等待你发货」= 已付款，「我已拍下，待付款」= 未付款
           （未付款**绝不发货**）；
        3. 已付款且仍带「去发货」按钮的 → 走平台确认发货；
        4. 平台确认成功后，**卡密就在这个会话里直接发**（订单与会话同源，
           天然不会发错聊天框）；
        5. 订单号级去重（`latest_delivery_for_order`）保证一笔单只发一次。

    这是「订单 → 会话」唯一可靠的一一对应来源：订单卡就在那个会话的消息里，
    不需要按昵称/闲鱼号去猜（实测同一买家有3 条并存话题且闲鱼号完全相同）。

    Args:
        engine: 已连接且定位到聊天页的引擎。
        scope: 最多翻看多少个会话（列表按最近活跃排序，新的在前）。

    Returns:
        本轮实际完成「平台确认 + 发卡密」的订单数。
    """
    if engine._page is None:
        return 0
    convs = engine.list_conversations(limit=scope)
    if not convs:
        return 0
    done = 0
    for c in convs[:max(1, scope)]:
        user = (c.get("nick") or c.get("user") or "").strip()
        if not user:
            continue
        try:
            if not engine.open_conversation(user, c.get("preview") or ""):
                continue
        except Exception as exc:  # noqa: BLE001
            log("WARN", f"  [聊天订单卡扫描] 打开「{user}」失败: {exc}")
            continue
        # 1) 聊天卡：判断这个会话有没有买家下的单、付了没
        cards = read_chat_order_cards(engine)
        if not cards:
            continue
        has_paid = any(x["paid"] for x in cards)
        has_unpaid = any(x["unpaid"] for x in cards)
        log("INFO", f"  [聊天订单卡扫描] 「{user}」会话订单卡 {len(cards)} 张"
                    f"（已付款={has_paid} 待付款={has_unpaid}）")
        if not has_paid:
            # 只有「我已拍下，待付款」→ 买家还没付钱，绝不发卡密
            if has_unpaid:
                log("INFO", f"  [聊天订单卡扫描] 「{user}」仅待付款订单，未发货")
            continue
        # 2) 订单号取自**当前会话**的右侧买家面板（与卡同属这次打开，
        #    天然一一对应，绝不跨会话猜）
        panel = read_panel_orders(engine)
        pendings = [o for o in panel["orders"] if _is_pending_shipment(o, "")]
        for p in pendings[:3]:
            oid = p.get("order_id") or ""
            title = p.get("title") or ""
            if not oid:
                continue
            rec = db.latest_delivery_for_order(oid)
            if rec and rec.get("status") == "sent" \
                    and rec.get("platform_status") == "confirmed":
                continue  # 这笔已完成，不重复发
            log("INFO", f"  [聊天订单卡扫描] 「{user}」已付款待发货订单 {oid}"
                        f"（商品「{title}」）")
            bundle = db.find_bundle_for_title(title)
            if bundle is None:
                log("WARN", f"  [聊天订单卡扫描] 订单 {oid} 商品「{title}」"
                            f"未绑定卡卷，请到网页端「商品列表」绑定")
                continue
            # 3) 平台确认发货（带去发货按钮 → 必须确认）
            if not (rec and rec.get("platform_status") == "confirmed"):
                log("INFO", f"  [聊天订单卡扫描] 订单 {oid} 平台确认发货中…")
                if not confirm_platform_shipment(engine, oid):
                    pending_reason = _SHIP_PENDING_REASON.pop(oid, "")
                    if pending_reason:
                        log("INFO", f"  [聊天订单卡扫描] 订单 {oid} 暂不可发货："
                                    f"{pending_reason}；保持等待，不记为失败")
                    else:
                        log("WARN", f"  [聊天订单卡扫描] 订单 {oid} 平台确认失败，"
                                    f"不发卡密，下轮重试")
                    continue
                if not engine.dry_run:
                    if rec:
                        db.mark_platform_confirmed(rec["id"])
                    else:
                        rec = {"id": db.record_platform_result(
                            oid, user, title, ok=True)}
                # 确认发货会把 SPA 路由带走，必须**回到同一个会话**再发卡密
                # ——这就是「订单在哪个聊天框就在哪个聊天框发」的保证。
                #
                # 定位方式演进：open_conversation(昵称+预览) → open_conversation_for_order
                # (昵称+订单号) → 现在**订单号直达**。前两版都离不开昵称，
                # 昵称解析错或同名多话题时就会一直失败；而此刻平台已确认发货、
                # 订单已移出待发货列表，于是变成「钱已过户、卡密发不出去」
                # （2026-11-03 用户日志实测：订单 5127387843588031824 卡在这里）。
                opened = open_chat_by_order(engine, oid)
                if not opened:
                    # 兜底：订单管理页没找到（列表未加载/筛选不对）时，退回昵称定位
                    engine.find_chat_page()
                    opened = engine.open_conversation_for_order(
                        user, oid, title_hint=title)
                if not opened:
                    log("WARN", f"  [聊天订单卡扫描] 订单 {oid} 发卡密前无法定位会话"
                                f"（订单号直达与昵称均失败），下轮重试")
                    continue
            # 4) 就在当前（= 该订单所在）会话发卡密
            if engine.dry_run:
                done += 1
                continue
            if _deliver(engine, user, f"[聊天卡待发货单 {oid}]",
                        payload=bundle["content"],
                        product_name=bundle["item_title"],
                        product_id=bundle["item_id"],
                        order_id=oid, via="卡卷-聊天卡",
                        delivery_id=(rec or {}).get("id"),
                        platform_confirmed=True):
                done += 1
    return done


def scan_pending_orders(engine: XianyuIMEngine, scope: int = 0,
                        skip_within: float = 0.0,
                        max_round_sec: float = 120.0) -> int:
    """定时扫描会话列表前 scope 个（0=全部），处理静默买家的待发货订单。

    背景：买家付款后不再发言就不会产生未读徽章，实时轮询永远看不到他们
    （2026-10-18 用户报障：特殊订单付款后无消息，引擎只判"无待发货"，
    根本不尝试自动发货，每次都要用户手动发）。本函数按间隔主动翻开这些
    会话读右侧订单面板，发现待发货订单就走 handle_order 全流程
    （平台确认发货 -> 发卡密）。

    降入侵设计（用户明确要求"不要显得特别入侵"）：
      1. 扫描前记录当前激活会话，扫完**还原**——用户视角会话列表归位；
      2. 只读面板 + 走发货状态机，**绝不发关键词回复**（不打扰买家）；
      3. skip_within 秒内翻看/处理过的买家跳过（与正常处理路径共享
         _scan_seen）——刚聊过的买家不重复翻；
      4. 默认间隔 180s、默认只看前 10 个会话（网页设置可调）；
      5. handle_order 内部两级去重（买家级 10 分钟 + 订单号级）兜底，
         已发货的买家只是被翻开看一眼面板，不会有任何动作；
      6. 轮次时长上限 max_round_sec：单个 open_conversation 可能被 CDP
         阻塞十几秒（evaluate 无超时保护），超时即收手还原，剩下的
         买家下一轮再扫（未标记 _scan_seen，不会被跳过）。

    Args:
        engine: 已连接且定位到聊天页的引擎。
        scope: 0=全部会话；N=列表前 N 个（新的在前，付款的买家通常靠前）。
        skip_within: 多少秒内翻看过的买家跳过（传扫描间隔即可）。
        max_round_sec: 单轮时长上限，超时中止本轮（已翻的不重复翻）。

    Returns:
        本轮实际派单（有 pending 且进入 handle_order）的买家数。
    """
    if engine._page is None:
        return 0
    now = time.time()
    deadline = now + max_round_sec
    # 清理过期记录，防字典无限增长
    if len(_scan_seen) > 200:
        cutoff = now - max(600.0, skip_within * 4)
        for k in [k for k, ts in _scan_seen.items() if ts < cutoff]:
            del _scan_seen[k]

    # 1) 记录当前激活会话（还原用）——必须在滚动收集之前抓
    active = engine.current_active()
    convs = engine.list_conversations(limit=scope or 0)
    if not convs:
        return 0
    active_idx = None
    if active:
        for i, c in enumerate(convs):
            if c["nick"] == active["nick"] and c["preview"] == active["preview"]:
                active_idx = i
                break
    log("INFO", f"定时扫描: {len(convs)} 个会话（范围={'全部' if not scope else f'前{scope}个'}）")
    shipped = 0
    switched = False  # 是否真正切走过激活会话（没切过就不用还原）
    try:
        for i, c in enumerate(convs):
            if _stop:
                break
            if time.time() > deadline:
                log("WARN", f"定时扫描: 单轮超过 {max_round_sec:.0f}s，本轮收手"
                            f"（剩余 {len(convs) - i} 个下一轮再扫）")
                break
            _touch()  # 看门狗保活
            nick = c["nick"]
            if skip_within and now - _scan_seen.get(nick, 0.0) < skip_within:
                continue
            if i != active_idx and not engine.open_conversation(nick, c["preview"]):
                continue  # 打不开就跳过，不影响其他买家
            if i != active_idx:
                switched = True
            _scan_seen[nick] = time.time()
            panel = read_panel_orders(engine)
            pendings = [o for o in panel["orders"] if _is_pending_shipment(o, "")]
            if not pendings:
                continue
            log("INFO", f"[定时扫描] {nick} 有 {len(pendings)} 笔待发货订单"
                        f"（静默买家，无新消息触发，主动捞单）")
            if handle_order(engine, nick, "", panel):
                shipped += 1
    finally:
        # 还原用户原来在看的会话（扫完列表归位，降低入侵感）；
        # 从未切走过（如只有一个会话且就是激活会话）则不必多点一次
        if active and switched:
            engine.open_conversation(active["nick"], active["preview"])
    return shipped


# ============ 主循环 ============ #
def count_missing_covers() -> int:
    """统计有多少商品的本地封面还没下载。

    【必须用 db.new_conn()】本函数在后台线程里跑，用全局 `db.conn()`
    会和主线程抢同一条 pymysql 连接（串包），结束时 close() 还会把
    主线程的连接关掉 → 引擎崩（2026-11-03 实测）。独占连接、用完自己关。
    """
    try:
        c = db.new_conn()
        try:
            with c.cursor() as cur:
                cur.execute("SELECT item_id FROM shop_items "
                            "WHERE item_id IS NOT NULL AND item_id <> '' "
                            "AND img IS NOT NULL AND img <> ''")
                ids = [str(r[0]) for r in cur.fetchall()]
        finally:
            c.close()
    except Exception:  # noqa: BLE001
        return 0
    base = Path(__file__).resolve().parents[1] / "data" / "product_images"
    miss = 0
    for iid in ids:
        d = base / iid
        if not any((d / f"cover{e}").is_file() for e in (".webp", ".jpg", ".png")):
            miss += 1
    return miss


def backfill_covers_bg() -> None:
    """引擎启动后**在后台**补齐商品封面，不阻塞任何主流程。

    为什么放在引擎而不只放在"获取商品"里：
      · 全新安装后用户点一次「获取商品」会自动下封面（那条链路已经接了）；
      · 但**升级安装**（数据库里已有商品、data/product_images 是空的/不全）
        不会触发采集，网页上图片就是空白 —— 用户还得手动跑脚本，很蠢。
    所以引擎一起来就自查一遍：缺几个就后台补几个，补失败也不影响发货
    （前端已有"本地 → CDN → 隐藏"三级回退）。
    """
    def _work() -> None:
        try:
            # 先等 20 秒再动手：引擎刚起来时主线程要连 CDP、读设置、写心跳，
            # 这时候再挤进来查库纯属抢资源（也是 2026-11-03 那次启动即崩的
            # 时序背景）。补图是后台收尾，晚 20 秒没有任何影响。
            time.sleep(20)
            n = count_missing_covers()
            if n <= 0:
                return
            log("INFO", f"[封面] 有 {n} 个商品缺本地封面，开始后台补齐…")
            import subprocess as _sp
            script = Path(__file__).resolve().parents[1] / "tools" / "download_covers.py"
            if not script.is_file():
                log("WARN", f"[封面] 找不到 {script}，跳过")
                return
            r = _sp.run([sys.executable, str(script)],
                        capture_output=True, text=True, timeout=1800,
                        encoding="utf-8", errors="replace")
            out = (r.stdout or "").strip().splitlines()
            # 取「完成：下载 x，跳过 y，失败 z」那一行，比最后一行有信息量
            line = next((x for x in reversed(out) if x.startswith("完成：")), "")
            line = line or (out[-1] if out else "")
            left = count_missing_covers()
            if left == 0:
                log("INFO", f"[封面] 已全部补齐（{line}）")
            else:
                log("WARN", f"[封面] 仍有 {left} 个缺图（{line}），下次启动会再试")
        except Exception as exc:  # noqa: BLE001
            log("WARN", f"[封面] 后台补图失败（不影响发货）：{exc}")

    threading.Thread(target=_work, daemon=True, name="cover-backfill").start()


def run() -> None:
    global _stop
    db.ensure_schema()
    # 建表之后再补一次列（幂等）：覆盖三种情况——
    #   ① init.sql 找不到（路径问题）导致一张表都没建；
    #   ② 老库用旧版 init.sql 建过，缺后加的 batch_*/scan_*/login_guard 列；
    #   ③ 建表中途失败留下的半成品库。
    # 不补的话，网页一保存设置就会 Unknown column（2026-10-29 实测）。
    try:
        _added = db.ensure_settings_columns()
        if _added:
            log("INFO", f"已补齐 settings 缺失列: {', '.join(_added)}")
    except Exception as exc:  # noqa: BLE001
        log("WARN", f"补列失败（可能表还未建）: {exc}")

    # 后台补商品封面：升级安装后库里已有商品、但本地没图时自动补，
    # 用户不用再手动跑脚本（2026-11-03 用户要求"不能自动吗"）
    try:
        backfill_covers_bg()
    except Exception as exc:  # noqa: BLE001
        log("WARN", f"启动封面补齐失败（忽略）: {exc}")

    settings = db.load_settings()
    selectors = {**DEFAULT_SELECTORS, **(settings.get("selectors") or {})}
    engine = XianyuIMEngine(
        int(settings["cdp_port"]), selectors, dry_run=bool(settings["dry_run"])
    )
    limiter = RateLimiter(limit=int(settings["max_replies_per_hour"]))
    handled: dict = {}
    # 启动即写心跳：否则首次 connect() 要轮询等待客户端就绪约 30s，
    # 这期间控制台「引擎进程」卡会误显「未启动」（2026-10-17 用户实测）。
    db.set_engine_status("running", pid=os.getpid(), heartbeat=True)
    log("INFO", f"网页引擎启动 | dry_run={engine.dry_run} | 端口={engine.cdp_port}")
    threading.Thread(target=_watchdog, daemon=True).start()
    # 扫描轮次内的后台心跳（主线程被 CDP 阻塞时续命，防误显已停止）
    threading.Thread(target=_heartbeat_thread, daemon=True).start()

    poll = float(settings["poll_interval"])
    # 全量扫描（滚轮滚整个会话列表）在 2026-10-17 实测会触发 CDP 调用
    # 永久挂起：渲染层无响应时 page.evaluate 没有超时保护，主线程永远
    # 阻塞在 asyncio select（py-spy 实锤），引擎僵尸化后被自己的看门狗
    # 强杀。日常新消息会把会话顶到列表最上方，增量扫描必然看得到，全量
    # 只是兜底。故默认降到每 300 轮（约 15 分钟）一次；数据库
    # settings.full_scan_every_cycles 存在时以其覆盖（0/负数视为默认）。
    full_every = int(settings.get("full_scan_every_cycles") or 300) or 300
    cycle = 0
    # 定时扫描（静默买家捞单）的上一次执行时间。初始化为当前时间：
    # 启动后先过一个间隔再扫，避免首轮连上 CDP 就立刻翻十几个会话。
    last_scan = time.time()
    while not _stop:
        cycle += 1
        _touch()
        full = cycle % full_every == 0
        # 每轮重载设置，网页端改动即时生效
        try:
            settings = db.load_settings()
            engine.dry_run = bool(settings["dry_run"])
            engine.selectors = {**DEFAULT_SELECTORS, **(settings.get("selectors") or {})}
            poll = float(settings["poll_interval"])
            limiter.limit = int(settings["max_replies_per_hour"])
            # 频率参数全部实时可调（网页设置页即改即生效）
            limiter.per_user_limit = max(1, int(settings.get("rate_limit_per_user_hour", 3) or 3))
            rl_window = max(0, int(settings.get("rate_limit_window_sec", 120) or 0))
            rl_on = bool(int(settings.get("rate_limit_enabled", 1) or 0))
            if rl_on != limiter.enabled:
                limiter.enabled = rl_on
                log("INFO", f"频率限制{'开启' if rl_on else '关闭（调试模式）'}")
        except Exception as exc:  # noqa: BLE001
            log("ERROR", f"读取设置失败: {exc}")
            rl_window = 120

        try:
            if engine._page is None:
                if not engine.connect():
                    db.set_engine_status("running", pid=os.getpid(), heartbeat=True)
                    log("WARN", "未连接卖家客户端（确认已用调试端口启动），5 秒后重试")
                    time.sleep(5)
                    continue
            if not engine.find_chat_page():
                engine._page = None
                log("WARN", "未定位到聊天页，重试")
                time.sleep(poll)
                continue

            db.set_engine_status("running", pid=os.getpid(), heartbeat=True)
            unread = engine.scan_unread(full=full)
            if unread:
                log("INFO", f"检测到 {len(unread)} 个待处理会话" + ("（全量）" if full else ""))
                process(engine, unread, handled, limiter, rl_window)
            # 定时扫描（静默买家捞单）：买家付款后不再发言则没有未读徽章，
            # 实时轮询看不见，按网页设置的间隔主动翻看前 N 个会话的面板。
            # 独立 try：扫描出问题不影响主轮询与连接（每轮重载设置，开关即时生效）。
            try:
                scan_on, scan_scope, scan_interval = _scan_settings(settings)
                if scan_on and time.time() - last_scan >= scan_interval:
                    last_scan = time.time()
                    # 主来源 1（用户 2026-10-18 21:21 明确的设计）：
                    # 逐个会话读**聊天消息区里的订单卡**——「我已付款，等待你
                    # 发货」= 已付款，「我已拍下，待付款」= 未付款。订单与会话
                    # 同源，卡密天然发在**这笔订单所在的聊天框**里，不会发错。
                    n0 = scan_chat_paid_orders(engine, scan_scope)
                    if n0:
                        log("INFO", f"[聊天订单卡扫描] {n0} 笔已付款订单完成"
                                    f"平台确认+发卡密")
                    # 主来源 2：卖家「订单管理/待发货」页（完整、文本可读），
                    # 用于兜住聊天卡还没渲染出来的静默订单。
                    n = ship_pending_from_manage(engine)
                    if n:
                        log("INFO", f"[订单管理扫描] {n} 笔待发货订单已走发货流程")
                    # 兜底：IM 面板扫描（特殊订单等仍可能在此命中）
                    n2 = scan_pending_orders(engine, scan_scope,
                                             skip_within=scan_interval)
                    if n2:
                        log("INFO", f"定时扫描: {n2} 个买家有待发货订单，已走发货流程")
                    # 兜底 2：补发「平台已确认但卡密从未送达」的订单。
                    # 这类订单已移出待发货列表，ship_pending_from_manage 扫不到，
                    # 不补发就是钱已过户、买家永远拿不到货（实测踩坑）。
                    n3 = resend_undelivered(engine)
                    if n3:
                        log("INFO", f"[卡密补发] {n3} 笔已确认订单补发卡密成功")
            except Exception as exc:  # noqa: BLE001
                log("ERROR", f"定时扫描异常: {exc}")
        except Exception as exc:  # noqa: BLE001
            log("ERROR", f"轮询异常: {exc}")
            try:
                engine.close()
            except Exception:
                pass
            time.sleep(poll)

        # 处理网页下发的命令
        for cmd in db.pop_commands():
            if cmd["cmd"] == "stop":
                log("INFO", "收到停止命令")
                _stop = True
        db.set_engine_status("running", pid=os.getpid(), heartbeat=True)
        if _stop:
            break
        time.sleep(poll)

    try:
        engine.close()
    except Exception:
        pass
    db.set_engine_status("stopped")
    log("INFO", "网页引擎已停止")


def _handle_signal(signum, _frame) -> None:
    global _stop
    log("INFO", f"收到信号 {signum}，准备停止")
    _stop = True


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(LOG_DIR / f"web_engine_{datetime.now():%Y%m%d}.log",
                                encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )
    # 日志写失败（如 GBK 流）不再往 stderr 刷 traceback，降级由 _emit 负责
    logging.raiseExceptions = False
    signal.signal(signal.SIGINT, _handle_signal)
    try:
        signal.signal(signal.SIGTERM, _handle_signal)
    except (AttributeError, ValueError):
        pass
    try:
        if not _wait_single_instance():
            log("ERROR", "其他引擎仍在运行，为避免重复回复放弃本次启动")
            return 3
        run()
    except Exception as exc:  # noqa: BLE001
        log("ERROR", f"引擎崩溃: {exc}")
        db.set_engine_status("error")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
