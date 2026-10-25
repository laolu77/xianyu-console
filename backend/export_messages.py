"""会话导出器：把闲鱼客服软件里的会话按用户归档到本地文件。

用途
----
用户要求「创建消息文件夹，每个文件用用户名命名，内容是当前用户的具体信息，
并把每个用户的聊天消息放进对应文件」。

本工具做四件事：
1. 滚动加载**完整**的左侧会话列表，提取每个用户的昵称、订单状态标签、
   未读数、最后消息预览、时间；
2. 逐个打开会话，读取中间聊天列里真实的聊天消息（不只是列表预览），
   并把聊天区向上滚动以尽量捞回更早的历史；
3. 抓取右侧「买家信息」面板（信用、地区、购买次数、累计消费等）；
4. 按 ``messages/<昵称>.md`` 落盘，另生成 ``messages/_索引.md`` 汇总。

DOM 结构（实测，2026-10-15，均用**类名前缀**匹配，不写死运行时哈希）
---------------------------------------------------------------------
* 会话列表项：``div[class*="conversation-item"]``
  内部文本子元素靠几何特征区分：昵称（x≈66、h≥17、w≤200）、
  预览（x≈66、h≤16、w≤175）、时间（x≈66、h≤16、w>175）；
  未读数在 ``[class*="ant-badge"]`` 内。列表项**没有稳定 id**，
  同一买家可能因多笔订单出现多条同名会话，需用「昵称+预览」区分。
* 列表是 **rc-virtual-list 虚拟滚动**：只渲染可见窗口（约 10~12 项），
  ``scrollHeight`` 随滚动增长（98 个会话 → 7616px）。
* 打开会话时应用会自动把列表往下蹭一格（68px）。
* 聊天消息行：``div[class*="message-row"]``
  ``message-text-left`` = 买家发来；``message-text-right`` = 我（卖家）发出；
  已读状态在 ``[class*="read-status"]``。
* 订单/系统操作卡片：``div[class*="msg-dx-content"]``，自带 left/right 方向。
* 系统提示条：``div[class*="msg-tips"]``（如「退款成功，钱款已原路退返」）。
* 评价邀请等富文本卡片：``div[class*="msg-text-card"]``。
* 时间分隔符：消息列表里文本形如 ``15:19`` / ``2026-09-15`` 的独立叶子元素。
* 聊天列顶栏：``div[class*="message-topbar"]``，含买家昵称与闲鱼号。
* 买家信息面板：``div[class*="right-panel-container"]``。

致命坑（全部实测踩过，改代码前先读）
------------------------------------
1. **CDP evaluate 不接受带参函数**：传 ``(nick) => {...}`` 时 CDP 把
   「函数本身」当返回值，Python 侧只收到 ``{}``。参数必须由 Python 烘焙进 JS。
2. **不要用 JS 赋值 ``scrollTop`` 驱动虚拟列表**：rc-virtual-list 靠原生滚动
   事件同步内部偏移，直接改 scrollTop 会让组件失联——容器 scrollTop 一路涨到
   7008/7616，渲染窗口却冻在原处不动，之后所有「按昵称查找」全部失败
   （实测 64/98 个会话因此全军覆没）。**必须用真实鼠标滚轮事件**。
3. **合成点击不可靠**：``el.click()`` 对 React 虚拟列表时常无效；
   ``scrollIntoView`` 默认 smooth 动画还会让紧随其后读到的坐标停在旧位置 → 点错人。
   必须分两步：``scrollIntoView({behavior:'instant'})`` → 等稳定 → 重新定位取实时矩形。
   cdp.py 里另备 ``mouse_click``（Input.dispatchMouseEvent）可用。
4. **虚拟列表滚动步长要小**，且找不到目标时要能回顶部重找；
   大步长 + 单向往下 = 跳过头就永远错过。
5. **Electron 依赖父进程存活**：所有步骤必须在同一个 Python 进程里跑完，
   脚本一退软件立刻关、CDP 端口拒绝连接。

已知限制
--------
* 图片、语音、文件等非文本内容只以占位符出现（如 ``[语音]``、
  「此消息不支持在PC端浏览」）。
* 「通知消息」是系统通知会话，没有聊天消息结构。
* 买家信息面板并非每个会话都有（无订单/无资料的买家可能不渲染）。
* 同名会话若连预览都完全一致，会被当成同一个（列表项无稳定 id，无法区分）。

用法
----
    python export_messages.py            全量导出
    python export_messages.py --max 3    只导前 3 个会话（调试用）
    python export_messages.py --keep     导出后保留软件运行
"""

from __future__ import annotations

import win10_compat

win10_compat.setup()

import argparse  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

import cdp  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
OUT_DIR = BASE_DIR / "messages"
DUMP_JSON = LOG_DIR / "sessions_dump.json"


def _read_client_exe() -> Path:
    """定位闲鱼卖家客户端 exe。

    优先读 data/client_path.txt（安装向导里用户填的 / 控制台「选择位置」写的），
    再退回常见安装位置。原实现写死了作者机器上的 <用户名>\\...，
    换台电脑必然找不到。
    """
    base = Path(__file__).resolve().parents[1]
    cands: list[Path] = []
    try:
        f = base / "data" / "client_path.txt"
        if f.is_file():
            p = f.read_text(encoding="utf-8").strip()
            if p:
                cands.append(Path(p))
    except OSError:
        pass
    local = os.environ.get("LOCALAPPDATA")
    if local:
        cands.append(Path(local) / "Programs" / "xianyu-seller-im")
    for drive in ("C:/", "D:/", "E:/"):
        cands.append(Path(drive) / "Programs" / "xianyu-seller-im")
    for d in cands:
        try:
            exe = d / "闲鱼卖家客服.exe"
            if exe.is_file():
                return exe
            if d.is_dir():
                for f in d.glob("*.exe"):
                    if "闲鱼" in f.name and "客服" in f.name:
                        return f
        except OSError:
            continue
    return Path("闲鱼卖家客服.exe")


def _read_seller_name(default: str = "") -> str:
    """读 data/seller_name.txt（导出消息时用于识别「我发的」消息）。"""
    try:
        f = Path(__file__).resolve().parents[1] / "data" / "seller_name.txt"
        if f.is_file():
            return f.read_text(encoding="utf-8").strip() or default
    except OSError:
        pass
    return default


EXE = _read_client_exe()
PORT = 9222
LAUNCH_ARGS = ("--no-sandbox", "--disable-gpu", f"--remote-debugging-port={PORT}")

READY_MIN_CHARS = 200
COL_ANCHOR = 66
SELLER = _read_seller_name()   # 卖家昵称：读 data/seller_name.txt
                                # （空值会导致识别不出"我发的"消息，按需配置）

# 滚轮事件落点：必须悬停在目标列表区域上
LIST_WHEEL = (165, 400)     # 左侧会话列表
CHAT_WHEEL = (700, 400)     # 中间聊天列

WHEEL_STEP = 200            # 单次滚轮像素（约 3 个会话）
WHEEL_UP_BIG = -700         # 快速回顶/回底
SCROLL_SETTLE = 0.5         # 每次滚动后等待渲染
SWITCH_WAIT = 2.5           # 点击会话后等待切换

LIST_MAX_STEPS = 70         # 单次查找最多滚多少轮
CHAT_MAX_STEPS = 40

_ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]')


def clean_env() -> dict:
    """构造清理过的环境变量。"""
    env = dict(os.environ)
    for k in ("ELECTRON_RUN_AS_NODE", "NODE_OPTIONS", "PYTHONUTF8", "PYTHONIOENCODING"):
        env.pop(k, None)
    return env


def kill_app() -> None:
    """关闭所有闲鱼实例。"""
    subprocess.run(["taskkill", "/F", "/T", "/IM", EXE.name], capture_output=True)
    time.sleep(2)


def start_app():
    """带调试端口启动软件。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = open(LOG_DIR / "export_console.log", "wb")
    proc = subprocess.Popen(  # noqa: S603
        [str(EXE), *LAUNCH_ARGS],
        stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        env=clean_env(), cwd=str(EXE.parent), close_fds=True,
    )
    print(f"[*] 已启动软件，PID={proc.pid}")
    return proc


def wait_targets(timeout: float = 60.0) -> list:
    """等待 CDP target 列表稳定。"""
    deadline = time.time() + timeout
    stable, hits, last = -1, 0, []
    while time.time() < deadline:
        try:
            last = cdp.list_targets(PORT) or []
        except Exception:
            last = []
        if len(last) == stable and last:
            hits += 1
            if hits >= 4:
                return last
        else:
            stable, hits = len(last), 0
        time.sleep(2.0)
    print("[!] 等待 target 稳定超时")
    return last


def wait_ready(sess: "cdp.CDPSession", timeout: float = 90.0) -> int:
    """轮询等待页面正文加载。"""
    deadline = time.time() + timeout
    last = 0
    while time.time() < deadline:
        try:
            last = sess.evaluate(
                "document.body ? document.body.innerText.length : 0") or 0
        except Exception:
            last = 0
        if last >= READY_MIN_CHARS:
            return last
        time.sleep(2.5)
    return last


# ================================================================ JS：会话列表

LIST_JS = r"""
() => {
  const out = [];
  for (const it of document.querySelectorAll('[class*="conversation-item"]')) {
    const r = it.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    const texts = [];
    for (const e of it.querySelectorAll('*')) {
      if (e.children.length) continue;
      const t = (e.innerText || '').trim();
      if (!t) continue;
      const rr = e.getBoundingClientRect();
      if (rr.width === 0 || rr.height === 0) continue;
      texts.push({text: t, x: Math.round(rr.x), y: Math.round(rr.y),
                  w: Math.round(rr.width), h: Math.round(rr.height)});
    }
    texts.sort((a, b) => (a.y - b.y) || (a.x - b.x));
    let unread = '';
    for (const b of it.querySelectorAll('[class*="ant-badge"]')) {
      const t = (b.innerText || '').trim();
      if (t) { unread = t; break; }
    }
    if (!unread) {
      for (const f of texts) {
        if (f.x < 60 && /^\d{1,2}$/.test(f.text)) { unread = f.text; break; }
      }
    }
    out.push({y: Math.round(r.y), unread, texts});
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
    atBottom: box.scrollTop + box.clientHeight >= box.scrollHeight - 4,
  });
}
"""

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

CLICK_TMPL = r"""
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
    const cx = Math.round(r.x + r.width / 2), cy = Math.round(r.y + r.height / 2);
    const el = document.elementFromPoint(cx, cy);
    if (!el) return 'no-element@' + cx + ',' + cy;
    el.click();
    return 'clicked@' + cx + ',' + cy;
  }
  return 'not-visible';
}
"""

TOPBAR_JS = r"""
() => {
  const el = document.querySelector('[class*="message-topbar"]');
  return el ? (el.innerText || '').replace(/\s+/g, ' ').trim() : '';
}
"""

PROFILE_JS = r"""
() => {
  const el = document.querySelector('[class*="right-panel-container"]');
  return el ? (el.innerText || '').replace(/\n{2,}/g, '\n').trim() : '';
}
"""

# ================================================================ JS：聊天消息

MSG_JS_TMPL = r"""
() => {
  const BUYER = __BUYER__;
  const SELLER = __SELLER__;
  const out = [];
  const seen = new Set();
  const push = (o) => {
    const k = o.kind + '|' + o.side + '|' + (o.text || '') + '|' + o.y;
    if (seen.has(k)) return;
    seen.add(k);
    out.push(o);
  };

  for (const row of document.querySelectorAll('[class*="message-row"]')) {
    const r = row.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    // 订单卡片 / 富文本卡片本身也包在 message-row 里，
    // 交给下面的专用分支处理，否则会重复采集。
    if (row.querySelector('[class*="msg-dx-content"], [class*="msg-text-card"]')) continue;
    const body = row.querySelector('[class*="message-text-"]');
    const status = row.querySelector('[class*="read-status"]');
    let side = 'unknown';
    if (row.querySelector('[class*="message-text-right"]')) side = 'me';
    else if (row.querySelector('[class*="message-text-left"]')) side = 'buyer';
    else if (row.querySelector('[class*="right--"]')) side = 'me';
    else if (row.querySelector('[class*="left--"]')) side = 'buyer';
    let name = '';
    let best = 1e9;
    for (const e of row.querySelectorAll('*')) {
      if (e.children.length) continue;
      const t = (e.innerText || '').trim();
      if (!t || t.length > 24) continue;
      if (e.closest('[class*="message-text-"]')) continue;
      if (e.closest('[class*="read-status"]')) continue;
      // 发送者昵称标签优先：图片/语音/位置等卡片行里有多个叶子
      // （昵称、图片占位、语音时长、地名），"最短文本"兜底会选错
      // （实测选出了「图片」「1s」「小还庄」「01:05」）。
      if (t === BUYER || t === SELLER) { name = t; break; }
      if (t.length < best) { best = t.length; name = t; }
    }
    // 方向兜底1：无任何标记类时（位置卡/图片/语音行），
    // 买家行左对齐（x≈330），我的行右对齐（x≈604）。
    // 用行中心 vs 列表中心比较，与窗口宽度无关。
    if (side === 'unknown') {
      const list = document.querySelector('[class*="message-list"], [class*="scroll-container"]');
      if (list) {
        const lr = list.getBoundingClientRect();
        side = (r.x + r.width / 2 < lr.x + lr.width / 2) ? 'buyer' : 'me';
      }
    }
    // 方向兜底2：会话里只有两个人——买家和我。
    if (side === 'unknown' && name) {
      side = (name === BUYER) ? 'buyer' : 'me';
    }
    push({kind: 'message', side, name,
          text: body ? (body.innerText || '').trim() : (row.innerText || '').trim(),
          read: status ? (status.innerText || '').trim() : '',
          y: Math.round(r.y)});
  }

  for (const el of document.querySelectorAll('[class*="msg-dx-content"]')) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    const cls = (typeof el.className === 'string') ? el.className : '';
    const side = cls.indexOf('msg-text-right') >= 0 ? 'me' : 'buyer';
    const title = el.querySelector('[class*="msg-dx-title"]');
    const desc = el.querySelector('[class*="msg-dx-desc"]');
    const btn = el.querySelector('[class*="msg-dx-button-text"]');
    push({kind: 'order-card', side,
          text: (title ? title.innerText.trim() : '') +
                (desc ? ' — ' + desc.innerText.trim() : ''),
          button: btn ? btn.innerText.trim() : '',
          y: Math.round(r.y)});
  }

  for (const el of document.querySelectorAll('[class*="msg-tips"]')) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    push({kind: 'tip', side: 'system', text: (el.innerText || '').trim(),
          y: Math.round(r.y)});
  }

  for (const el of document.querySelectorAll('[class*="msg-text-card"]')) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    push({kind: 'card', side: 'system',
          text: (el.innerText || '').replace(/\s+/g, ' ').trim(),
          y: Math.round(r.y)});
  }

  // 时间分隔符：消息列表里形如 15:19 / 2026-09-15 的独立叶子元素
  const list = document.querySelector('[class*="message-list"], [class*="scroll-container"]');
  if (list) {
    for (const e of list.querySelectorAll('*')) {
      if (e.children.length) continue;
      const t = (e.innerText || '').trim();
      if (!t || t.length > 20) continue;
      const rr = e.getBoundingClientRect();
      if (rr.width === 0 || rr.height === 0) continue;
      if (!/^(\d{1,2}:\d{2}|\d{4}-\d{1,2}-\d{1,2}|\d{1,2}-\d{1,2}|昨天.*|星期.+)$/.test(t)) continue;
      push({kind: 'time', side: 'system', text: t, y: Math.round(rr.y)});
    }
  }

  out.sort((a, b) => a.y - b.y);
  return JSON.stringify(out);
}
"""

CHAT_STATE_JS = r"""
() => {
  const row = document.querySelector('[class*="message-row"], [class*="msg-dx-content"]');
  if (!row) return JSON.stringify({found: false});
  let p = row.parentElement, box = null;
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
    atBottom: box.scrollTop + box.clientHeight >= box.scrollHeight - 4,
  });
}
"""


def _msg_js(buyer: str) -> str:
    """把买家/卖家昵称烘焙进消息提取脚本（CDP 不接受带参函数）。"""
    return (MSG_JS_TMPL.replace("__BUYER__", json.dumps(buyer))
            .replace("__SELLER__", json.dumps(SELLER)))


# ================================================================ 滚轮驱动


def wheel(sess: "cdp.CDPSession", at: tuple, delta_y: int) -> None:
    """在指定视口坐标处派发一次滚轮事件。"""
    sess.mouse_move(at[0], at[1])
    sess.mouse_wheel(at[0], at[1], delta_y)


def _state(sess: "cdp.CDPSession", js: str) -> dict:
    raw = sess.evaluate(js)
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return raw if isinstance(raw, dict) else {}


def scroll_list_to_top(sess: "cdp.CDPSession") -> bool:
    """用滚轮把会话列表滚回顶部。"""
    for _ in range(LIST_MAX_STEPS):
        st = _state(sess, LIST_STATE_JS)
        if not st.get("found"):
            return False
        if st.get("atTop"):
            return True
        wheel(sess, LIST_WHEEL, WHEEL_UP_BIG)
        time.sleep(0.25)
    return False


def scroll_list_step(sess: "cdp.CDPSession", dy: int = WHEEL_STEP) -> dict:
    """会话列表向下滚**一步**，返回滚动后的状态。

    注意：必须是一步一返回。若在这个函数里一路滚到底，
    调用方就只能在「滚之前」和「滚之后」各采一次样，
    中间的会话全部漏掉（实测因此只加载到 18 条而非 98 条）。
    """
    wheel(sess, LIST_WHEEL, dy)
    time.sleep(SCROLL_SETTLE)
    return _state(sess, LIST_STATE_JS)


# ================================================================ Python 逻辑


def parse_sessions(rows: list) -> list:
    """把列表行还原成会话对象。

    同一买家可能有多条会话（多笔订单），列表项又没有稳定 id，
    因此用「昵称 + 预览」作为身份，**不去重**，保留全部条目。
    """
    sessions: list = []
    index: dict = {}
    for row in rows:
        s = {"nick": "", "preview": "", "time": "", "tags": [],
             "unread": row.get("unread", ""), "y": row["y"]}
        for f in row["texts"]:
            near = abs(f["x"] - COL_ANCHOR) <= 6
            if near and f["h"] >= 17 and f["w"] <= 200:
                if not s["nick"]:
                    s["nick"] = f["text"]
            elif near and f["h"] <= 16 and f["w"] <= 175:
                if not s["preview"]:
                    s["preview"] = f["text"]
            elif near and f["h"] <= 16:
                if not s["time"]:
                    s["time"] = f["text"]
            else:
                if f["text"] not in s["tags"]:
                    s["tags"].append(f["text"])
        if not s["nick"]:
            continue
        key = (s["nick"], s["preview"])
        if key in index:
            # 同一会话在不同滚动位置渲染出的字段可能不同，互相补全
            old = index[key]
            for k in ("time", "unread"):
                if not old[k] and s[k]:
                    old[k] = s[k]
            for t in s["tags"]:
                if t not in old["tags"]:
                    old["tags"].append(t)
            continue
        index[key] = s
        sessions.append(s)
    return sessions


def load_all_sessions(sess: "cdp.CDPSession") -> list:
    """用滚轮一步步滚动列表，沿途采集，加载全部会话。"""
    scroll_list_to_top(sess)
    rows: list = []
    for _ in range(LIST_MAX_STEPS):
        raw = sess.evaluate(LIST_JS)
        got = json.loads(raw) if isinstance(raw, str) else (raw or [])
        for r in got:
            if r not in rows:
                rows.append(r)
        st = _state(sess, LIST_STATE_JS)
        if st.get("atBottom"):
            break
        scroll_list_step(sess)
    return parse_sessions(rows)


def _locate(sess: "cdp.CDPSession", nick: str, preview: str) -> str:
    js = (LOCATE_TMPL.replace("__NICK__", json.dumps(nick))
          .replace("__PREVIEW__", json.dumps(preview or "")))
    res = sess.evaluate(js)
    return res if isinstance(res, str) else "error"


def _click(sess: "cdp.CDPSession", nick: str, preview: str) -> str:
    js = (CLICK_TMPL.replace("__NICK__", json.dumps(nick))
          .replace("__PREVIEW__", json.dumps(preview or "")))
    res = sess.evaluate(js)
    return res if isinstance(res, str) else "error"


def _ensure_visible(sess: "cdp.CDPSession", nick: str, preview: str) -> bool:
    """只向下滚，找到目标即停。"""
    for _ in range(LIST_MAX_STEPS):
        if _locate(sess, nick, preview) == "located":
            return True
        st = _state(sess, LIST_STATE_JS)
        if st.get("atBottom"):
            return False
        wheel(sess, LIST_WHEEL, WHEEL_STEP)
        time.sleep(SCROLL_SETTLE)
    return False


def open_session(sess: "cdp.CDPSession", nick: str,
                 preview: str = "", retries: int = 3) -> str:
    """打开指定会话，并用顶栏昵称校验是否真的切换成功。

    顺序：先只向下找（多数情况一步到位）→ 找不到就回顶部再找一遍 →
    定位后等滚动稳定 → 重新取实时坐标点击 → 顶栏校验 → 失败重试。

    Args:
        sess: CDP 会话。
        nick: 买家昵称。
        preview: 列表预览文本（用于区分同名会话）。
        retries: 校验失败时的重试次数。

    Returns:
        ``clicked@x,y`` / ``not-found`` / ``verify-failed``。
    """
    for attempt in range(retries):
        if not _ensure_visible(sess, nick, preview):
            scroll_list_to_top(sess)
            if not _ensure_visible(sess, nick, preview):
                return "not-found"

        # 等滚动稳定后再取实时坐标点击（避免 smooth 动画拿到旧坐标）
        time.sleep(SCROLL_SETTLE)
        res = _click(sess, nick, preview)
        if not res.startswith("clicked"):
            continue
        time.sleep(SWITCH_WAIT)
        topbar = sess.evaluate(TOPBAR_JS) or ""
        if (not topbar) or (nick in topbar):
            return res
        print(f"    [!] 第 {attempt + 1} 次点击后顶栏仍显示「{topbar[:24]}」，重试")

    return "verify-failed"


def read_chat(sess: "cdp.CDPSession", buyer: str) -> tuple:
    """读取当前会话的聊天消息。

    策略：先往上滚若干次捞更早的历史（消息按内容去重，重复无害），
    没有新增就停；再滚回底部保证最新消息在 DOM 里，最后统一采集一次。
    """
    js = _msg_js(buyer)

    def collect() -> list:
        raw = sess.evaluate(js)
        return json.loads(raw) if isinstance(raw, str) else (raw or [])

    # 向上捞历史
    stale = 0
    for _ in range(CHAT_MAX_STEPS):
        before = len(collect())
        wheel(sess, CHAT_WHEEL, WHEEL_UP_BIG)
        time.sleep(SCROLL_SETTLE)
        if len(collect()) <= before:
            stale += 1
            if stale >= 3:
                break
        else:
            stale = 0

    # 滚回底部
    for _ in range(CHAT_MAX_STEPS):
        st = _state(sess, CHAT_STATE_JS)
        if not st.get("found") or st.get("atBottom"):
            break
        wheel(sess, CHAT_WHEEL, WHEEL_STEP)
        time.sleep(SCROLL_SETTLE)

    msgs = collect()
    msgs.sort(key=lambda m: m["y"])

    # 清理兜底提取的文本：去掉开头的发送者昵称与结尾的已读标记
    for m in msgs:
        if m["kind"] != "message":
            continue
        t = m.get("text") or ""
        name = m.get("name") or ""
        if name and t.startswith(name):
            t = t[len(name):].lstrip("\r\n")
        if m.get("read") and re.search(r"(?:^|\n)(?:已读|未读)\s*$", t):
            t = re.sub(r"(?:\n)?(?:已读|未读)\s*$", "", t)
        m["text"] = t.strip()

    topbar = sess.evaluate(TOPBAR_JS) or ""
    profile = sess.evaluate(PROFILE_JS) or ""
    return msgs, topbar, profile


# ================================================================ 输出


def safe_name(nick: str) -> str:
    """把昵称转成安全的文件名。"""
    name = _ILLEGAL.sub("_", nick).strip().strip(".")
    return (name or "未命名会话")[:80]


SIDE_LABEL = {"buyer": "买家", "me": "我", "system": "系统", "unknown": "未知"}
KIND_LABEL = {"message": "", "order-card": "[订单] ", "tip": "[提示] ",
              "card": "[卡片] ", "time": ""}


def render_md(s: dict, exported_at: str) -> str:
    """渲染单个用户的 Markdown 文件。"""
    lines: list = [f"# {s['nick']}", "", "## 用户信息", ""]
    lines.append(f"- 导出时间：{exported_at}")
    lines.append(f"- 会话昵称：{s['nick']}")
    if s.get("topbar"):
        lines.append(f"- 聊天栏信息：{s['topbar']}")
    lines.append(f"- 订单状态标签：{' / '.join(s['tags']) or '无'}")
    lines.append(f"- 未读消息数：{s['unread'] or '0'}")
    lines.append(f"- 列表中的最后消息：{s['preview'] or '无'}")
    lines.append(f"- 列表中的时间：{s['time'] or '未知'}")
    lines.append(f"- 采集到聊天条目：{len(s['messages'])} 条")
    if s.get("status") and s["status"] != "ok":
        reason = {"not-found": "列表中未能定位到该会话",
                  "verify-failed": "点击后顶栏校验未通过（可能为系统会话）"}
        desc = reason.get(s["status"], s["status"])
        lines.append(f"- ⚠️ 导出状态：{desc}")
    lines.append("")

    if s.get("profile"):
        lines.append("## 买家信息面板（软件右侧原始内容）")
        lines.append("")
        lines.append("```text")
        lines.append(s["profile"])
        lines.append("```")
        lines.append("")

    lines.append("## 聊天记录")
    lines.append("")
    if not s["messages"]:
        lines.append("（未采集到聊天消息：该会话可能无历史消息，或属于通知类会话）")
        lines.append("")
    for m in s["messages"]:
        if m["kind"] == "time":
            lines.append("")
            lines.append(f"`── {m['text']} ──`")
            lines.append("")
            continue
        label = SIDE_LABEL.get(m.get("side", "unknown"), "未知")
        head = f"**{KIND_LABEL.get(m['kind'], '')}{label}**"
        if m.get("name") and m["kind"] == "message":
            head += f"（{m['name']}）"
        if m.get("button"):
            head += f"［可操作：{m['button']}］"
        body = m.get("text") or ""
        if m.get("read"):
            body = f"{body}　〔{m['read']}〕"
        lines.append(f"- {head}：{body}")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("> 说明：条目按软件界面从上到下的实际顺序排列；`时间`行为软件自带")
    lines.append("> 的时间分隔符。图片/语音/文件等非文本内容只以占位符形式出现。")
    return "\n".join(lines).rstrip() + "\n"


def render_index(entries: list, exported_at: str) -> str:
    """渲染汇总索引文件。"""
    ok = sum(1 for s in entries if s.get("status") == "ok")
    lines = ["# 会话索引", "", f"- 导出时间：{exported_at}",
             f"- 会话条目总数：{len(entries)}（成功 {ok}，失败/跳过 {len(entries) - ok}）",
             "",
             "| # | 用户 | 订单状态 | 未读 | 最后消息 | 时间 | 消息条数 | 文件 |",
             "|---|---|---|---|---|---|---|---|"]
    for i, s in enumerate(entries, 1):
        flag = "" if s.get("status") == "ok" else " ⚠️"
        lines.append(
            f"| {i} | {s['nick']}{flag} | {' / '.join(s['tags']) or '-'} "
            f"| {s['unread'] or '-'} | {(s['preview'] or '-')[:24]} "
            f"| {s['time'] or '-'} | {len(s['messages'])} | {s['file']} |")
    lines.append("")
    lines.append("> ⚠️ 标记表示该会话未能完整导出（列表中定位失败或点击后校验未通过）。")
    lines.append("")
    return "\n".join(lines)


# ================================================================ 主流程


RESULTS_JSONL = LOG_DIR / "results.jsonl"


def reconnect(port: int = PORT):
    """CDP 断线后重新附着聊天页面。

    返回新的 CDPSession；软件已死则返回 None。
    """
    for attempt in range(6):
        try:
            targets = cdp.list_targets(port) or []
        except Exception:
            targets = []
        for t in [x for x in targets if x["type"] == "page"]:
            try:
                s = cdp.CDPSession(t["webSocketDebuggerUrl"])
                n = s.evaluate("document.body?document.body.innerText.length:0") or 0
                if n >= READY_MIN_CHARS:
                    print(f"    [*] CDP 已重连：「{t.get('title', '')[:30]}」正文 {n} 字")
                    return s
                s.close()
            except Exception:
                pass
        print(f"    [!] 重连失败（第 {attempt + 1}/6 次），3 秒后重试...")
        time.sleep(3)
    return None


def load_resumed() -> dict:
    """读取增量结果文件，按 (昵称, 预览) 索引。仅保留成功的记录。"""
    done: dict = {}
    if RESULTS_JSONL.is_file():
        for line in RESULTS_JSONL.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("status") == "ok":
                done[(r.get("nick", ""), r.get("preview", ""))] = r
    return done


def append_result(rec: dict) -> None:
    """把一条会话结果追加到增量文件（崩溃不丢已完成的会话）。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with RESULTS_JSONL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def main() -> int:
    """导出入口。"""
    ap = argparse.ArgumentParser(description="按用户导出闲鱼客服会话")
    ap.add_argument("--max", type=int, default=0, help="只导前 N 个会话（0=全部）")
    ap.add_argument("--keep", action="store_true", help="导出后保留软件运行")
    ap.add_argument("--resume", action="store_true",
                    help="断点续导：读取 logs/results.jsonl，跳过已成功的会话")
    args = ap.parse_args()

    if not EXE.is_file():
        print(f"[x] 找不到软件: {EXE}")
        return 1

    kill_app()
    proc = start_app()
    targets = wait_targets()
    if not targets:
        print("[x] 调试端口未就绪")
        kill_app()
        return 1
    print(f"[+] CDP target {len(targets)} 个")

    sess = None
    for t in [x for x in targets if x["type"] == "page"]:
        try:
            s = cdp.CDPSession(t["webSocketDebuggerUrl"])
            size = wait_ready(s)
            if size >= READY_MIN_CHARS:
                sess = s
                print(f"[+] 已附着聊天界面：「{t.get('title','')[:40]}」正文 {size} 字")
                break
            s.close()
        except Exception as exc:
            print(f"[!] 附着失败: {exc}")

    if sess is None:
        print("[x] 未找到已加载的聊天界面")
        kill_app()
        return 1

    # ---- 第一步：加载完整会话列表 ----
    print("[*] 滚动加载会话列表...")
    sessions = load_all_sessions(sess)
    print(f"[+] 共 {len(sessions)} 个会话条目")
    for i, s in enumerate(sessions[:15], 1):
        print(f"    {i:>2}. {s['nick']:<22} 未读{s['unread'] or '-'} "
              f"[{'/'.join(s['tags']) or '-'}] {s['preview'][:20]} ({s['time']})")
    if len(sessions) > 15:
        print(f"    ... 其余 {len(sessions) - 15} 条从略")

    if not sessions:
        print("[x] 没有可导出的会话")
        if not args.keep:
            kill_app()
        return 1

    # ---- 断点续导：读取已完成记录 ----
    if args.resume:
        resumed = load_resumed()
        if resumed:
            print(f"[*] 续导模式：已有 {len(resumed)} 个成功会话将跳过")
    else:
        resumed = {}
        if RESULTS_JSONL.exists():
            RESULTS_JSONL.unlink()

    # 同名会话分配不同文件名：昵称.md / 昵称-2.md / ...
    # 注意：必须在**完整会话列表**上分配，保证与断点续导前后的命名一致。
    used: dict = {}
    for s in sessions:
        base = safe_name(s["nick"])
        n = used.get(base, 0) + 1
        used[base] = n
        s["file"] = base if n == 1 else f"{base}-{n}"

    limit = len(sessions) if not args.max else min(args.max, len(sessions))
    exported_at = time.strftime("%Y-%m-%d %H:%M:%S")
    results: list = []
    aborted = False

    # ---- 第二步：逐个打开并采集（单会话异常不影响全局） ----
    for idx in range(limit):
        s = sessions[idx]
        key = (s["nick"], s["preview"])
        if key in resumed:
            print(f"[*] ({idx + 1}/{limit}) 「{s['nick']}」已导出过，跳过")
            results.append(resumed[key])
            continue

        print(f"[*] ({idx + 1}/{limit}) 打开「{s['nick']}」"
              f"{'（' + s['preview'][:16] + '）' if s['preview'] else ''}...")
        try:
            hit = open_session(sess, s["nick"], s["preview"])
            if hit in ("not-found", "verify-failed"):
                print(f"    [!] 未能打开该会话（{hit}），已跳过")
                rec = dict(s)
                rec.update({"messages": [], "topbar": "", "profile": "",
                            "status": hit})
                append_result(rec)
                results.append(rec)
                continue
            msgs, topbar, profile = read_chat(sess, s["nick"])
            ok = (not topbar) or (s["nick"] in topbar)
            rec = dict(s)
            rec.update({"messages": msgs, "topbar": topbar, "profile": profile,
                        "status": "ok"})
            append_result(rec)
            results.append(rec)
            print(f"    {hit}  条目 {len(msgs)}  资料 {len(profile)} 字  "
                  f"校验={'通过' if ok else '顶部栏不匹配: ' + topbar[:30]}")
        except Exception as exc:
            # 单会话崩溃（如 CDP 超时）：记录后尝试重连，软件死了才中止
            print(f"    [!] 会话处理异常：{type(exc).__name__}: {exc}")
            rec = dict(s)
            rec.update({"messages": [], "topbar": "", "profile": "",
                        "status": f"error:{type(exc).__name__}"})
            append_result(rec)
            results.append(rec)
            new_sess = reconnect()
            if new_sess is None:
                print("[x] 软件已无响应，保存已有结果后退出")
                aborted = True
                break
            sess = new_sess

    # ---- 第三步：落盘 ----
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    DUMP_JSON.write_text(
        json.dumps({"generated": exported_at, "sessions": results},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[+] 原始数据已存: {DUMP_JSON}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for old in OUT_DIR.glob("*.md"):
        old.unlink()
    for s in results:
        path = OUT_DIR / (s["file"] + ".md")
        path.write_text(render_md(s, exported_at), encoding="utf-8")
        print(f"    [+] {path.name}  ({len(s['messages'])} 条)")
    (OUT_DIR / "_索引.md").write_text(
        render_index(results, exported_at), encoding="utf-8")
    ok_count = sum(1 for r in results if r.get("status") == "ok")
    fail_count = len(results) - ok_count
    print(f"[+] 完成：{len(results)} 个用户文件 -> {OUT_DIR}"
          f"（成功 {ok_count}，失败/跳过 {fail_count}）")
    if aborted:
        print("[!] 本次为中断导出；修复后可加 --resume 继续未完成的会话")

    if args.keep:
        print("[*] --keep：软件保持运行")
    else:
        kill_app()
    try:
        proc.wait(timeout=8)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
