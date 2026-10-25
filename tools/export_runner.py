# -*- coding: utf-8 -*-
"""export_runner.py - 网页端「导出消息」的后台执行包装器。

背景
----
桌面旧「导出消息」按钮直接裸跑 ``backend/export_messages.py``，输出只进
桌面控制台的日志终端，网页上无从触发也看不到进度。本包装器让同一套导出
逻辑可以被网页按钮驱动（2026-10-18 用户要求"把这个移植到网页上去"）：

    网页按钮 -> api.php 写 sw_control('export', 时间戳)
             -> 桌面控制台 monitor_loop 监听到触发器，拉起本脚本
             -> 本脚本跑导出，并把进度实时写 data/export_progress.json
             -> 网页轮询该 JSON 渲染进度条，完成后列出可下载文件

与裸跑 export_messages.py 的差异
--------------------------------
1. **传 ``--keep``**：导出结束后保留闲鱼客户端运行。导出过程会重启客户端
   （脚本自身的 kill_app/start_app），不 keep 的话客户端直接死掉，引擎
   要等用户手动重启才能恢复自动回复；keep 则引擎几秒内自动重连。
2. **进度上报**：stdout 套一层 Tee，原文照常进日志文件，同时按行解析
   成进度 JSON（格式与商品采集的 collect_progress.json 一致，前端复用
   同一套进度条组件）。
3. **单实例保护**：progress 为 running 且 10 分钟内刷新过则拒绝重复跑
   （api.php 侧还有一道同样的闸，双保险防并发导出互相踩踏 CDP）。
4. **trigger 印记**：把桌面监听到的触发器时间戳写进 progress JSON，api.php
   据此区分"触发器已写入但桌面还没接手"（pending）与正常 running。

已知冲突（有意接受的降级，非缺陷）
----------------------------------
导出独占客户端期间，引擎的定时扫描/自动回复仍会尝试点击会话，两边可能
互相顶掉对方刚打开的会话。导出侧有 3 次重试 + 顶栏校验兜底（失败仅该
会话标 ⚠️），引擎侧点击失败会跳过。网页按钮上也有确认对话框告知用户。

用法
----
    python tools/export_runner.py                 # 全量导出（网页/桌面触发形态）
    python tools/export_runner.py <trigger_ts>    # 附带 sw_control 触发器时间戳
    python tools/export_runner.py --max 1         # 只导前 1 个会话（调试用）
"""

from __future__ import annotations

import json
import re
import sys
import traceback
from datetime import datetime
from pathlib import Path

from _common import check_single_instance as _check_single_instance, write_progress as _c_write

# export_messages / cdp / win10_compat 都在 backend/ 下；
# 桌面启动器 spawn 时已带 PYTHONPATH=backend，这里再兜底一次（直接手跑也认）。
BASE = Path(__file__).resolve().parent.parent
BACKEND = BASE / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

PROGRESS_JSON = BASE / "data" / "export_progress.json"
STALE_SEC = 600  # progress running 的超龄窗口：超过视为上次导出已死

# ---- 导出脚本输出行的进度语义（全部实测自 export_messages.py 的 print） ----
# `[+] 共 98 个会话条目`
RE_TOTAL = re.compile(r"^\[\+\]\s*共\s*(\d+)\s*个会话条目")
# `[*] (12/98) 打开「买家昵称」（预览）...`
RE_STEP = re.compile(r"^\[\*\]\s*\((\d+)/(\d+)\)")
# `[+] 完成：98 个用户文件 -> ...（成功 95，失败/跳过 3）`
RE_DONE = re.compile(
    r"^\[\+\]\s*完成：(\d+)\s*个用户文件.*?成功\s*(\d+).*?失败/跳过\s*(\d+)")
# `[x] 找不到软件: ...` / `[x] 未找到已加载的聊天界面` 等致命行
RE_FATAL = re.compile(r"^\[x\]")


def write_progress(status: str, current: int = 0, total: int = 0,
                   message: str = "", trigger: str = "", **extra) -> None:
    """写导出进度（网页进度条数据源；写失败不影响导出本身）。"""
    data = {
        "status": status,
        "current": current,
        "total": total,
        "message": message,
        "trigger": trigger,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    data.update(extra)
    _c_write(PROGRESS_JSON, data)


# 单实例检查统一走 tools/_common.py
def check_single_instance() -> bool:
    """已有鲜活导出在跑时返回 False（防重复）。"""
    return _check_single_instance(PROGRESS_JSON, STALE_SEC)


def parse_progress_line(line: str, state: dict) -> dict | None:
    """把导出脚本的一行 stdout 翻译成进度更新；无关行返回 None。

    只有脚本级输出（行首 ``[``）会刷新 message；缩进的会话明细行
    （如 ``    1. 昵称  未读- ...``）是噪声，直接忽略。
    返回值恒定带 current/total（取自跨行状态）——否则普通 ``[*]`` 行
    会把进度条冲成 0/0（2026-10-18 单测抓到的 bug）。
    """
    if not line.startswith("["):
        return None
    upd: dict = {"message": line,
                 "current": state.get("current", 0),
                 "total": state.get("total", 0)}
    m = RE_TOTAL.match(line)
    if m:
        # 列表加载完毕，得知总数；若step已开始（--max 形态）则以step的总数为准
        if not state.get("step_seen"):
            state["total"] = int(m.group(1))
    else:
        m = RE_STEP.match(line)
        if m:
            state["step_seen"] = True
            state["current"] = int(m.group(1))
            state["total"] = int(m.group(2))
        else:
            m = RE_DONE.match(line)
            if m:
                state["done"] = True
                state["current"] = int(m.group(1))
                state["total"] = int(m.group(1))
                state["ok"] = int(m.group(2))
                state["fail"] = int(m.group(3))
                upd["ok"] = int(m.group(2))
                upd["fail"] = int(m.group(3))
            elif RE_FATAL.match(line):
                state["fatal"] = line
    upd["current"] = state.get("current", 0)
    upd["total"] = state.get("total", 0)
    return upd


class _Tee:
    """stdout 包装：原文透传（进日志文件），同时按行喂给进度解析器。"""

    def __init__(self, raw, on_line):
        self._raw = raw
        self._on_line = on_line
        self._pending = ""

    def write(self, s):
        try:
            self._raw.write(s)
        except Exception:  # noqa: BLE001
            pass  # 日志写失败不阻断导出
        self._pending += s
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            self._on_line(line.rstrip("\r"))

    def flush(self):
        try:
            self._raw.flush()
        except Exception:  # noqa: BLE001
            pass


def _parse_runner_args(argv: list) -> tuple[str, list]:
    """拆分本包装器自己的参数：仅当**首个参数是纯数字**才视为 trigger
    （sw_control 触发器时间戳），其余全部透传给 export_messages。

    为什么不能"第一个非选项参数即 trigger"：``--max 1`` 的 ``1`` 会被
    误吞成 trigger，导致 export_messages 只收到光杆 ``--max`` 报错
    （2026-10-18 单测抓到）。时间戳恒为纯数字，判定无歧义。
    """
    trigger = argv[0] if argv and argv[0].isdigit() else ""
    passthrough = argv[1:] if trigger else list(argv)
    return trigger, passthrough


def run_export(trigger: str = "", passthrough: list[str] | None = None) -> int:
    """执行一次导出（核心逻辑，唯一实现）。

    Args:
        trigger: 触发来源标记（写进进度文件，前端用来区分是谁点的）。
        passthrough: 透传给 export_messages.py 的额外参数。

    Returns:
        0 成功；非 0 失败（失败原因已写进进度文件）。
    """
    passthrough = list(passthrough or [])
    # 首次写进度：让网页立刻看到"已启动"（也占住单实例位）
    if not check_single_instance():
        write_progress("error", message="已有导出任务在进行中（或 10 分钟内"
                                        "刚跑过），请稍候再试", trigger=trigger)
        return 1
    write_progress("running", message="正在启动导出（重启闲鱼客户端）…",
                   trigger=trigger)

    # import export_messages 会顺带执行 win10_compat.setup()：
    # 输出被重定向到日志文件时强制 UTF-8 + errors=replace，编码确定。
    try:
        import export_messages  # noqa: E402
    except Exception as e:  # noqa: BLE001
        write_progress("error",
                       message=f"导出模块加载失败：{type(e).__name__}: {e}",
                       trigger=trigger)
        return 1

    state: dict = {}
    stdout_raw = sys.stdout
    sys.stdout = _Tee(stdout_raw,
                      lambda ln: _on_line(ln, state, trigger))  # type: ignore[assignment]

    rc = 0
    try:
        # --keep：导出后保留客户端运行（引擎可自动重连，不必手动重启软件）
        sys.argv = ["export_messages.py", "--keep", *passthrough]
        rc = export_messages.main()
    except SystemExit as e:  # 防御：脚本内若有人 sys.exit 也别炸掉包装器
        rc = int(e.code or 0)
    except Exception as e:  # noqa: BLE001
        rc = 1
        state["fatal"] = f"{type(e).__name__}: {e}"
        try:
            stdout_raw.write(traceback.format_exc())
        except Exception:  # noqa: BLE001
            pass
    finally:
        sys.stdout = stdout_raw

    if state.get("done") or (rc == 0 and not state.get("fatal")):
        msg = state.get("done_msg") or "导出完成"
        write_progress("done", current=state.get("current", 0),
                       total=state.get("total", 0), message=msg,
                       trigger=trigger,
                       ok=state.get("ok", 0), fail=state.get("fail", 0))
        return 0
    write_progress("error", current=state.get("current", 0),
                   total=state.get("total", 0),
                   message=state.get("fatal") or f"导出失败（退出码 {rc}）",
                   trigger=trigger)
    return rc or 1


def _on_line(line: str, state: dict, trigger: str) -> None:
    """Tee 的逐行回调：解析进度并落 JSON。"""
    upd = parse_progress_line(line, state)
    if not upd:
        return
    if state.get("done"):
        state["done_msg"] = line  # 完成行带「成功 X，失败/跳过 Y」汇总，优先展示
    write_progress("running", trigger=trigger, **upd)


def main() -> int:
    """命令行入口：只做参数解析。"""
    trigger, passthrough = _parse_runner_args(sys.argv[1:])
    return run_export(trigger, passthrough)


if __name__ == "__main__":
    sys.exit(main())
