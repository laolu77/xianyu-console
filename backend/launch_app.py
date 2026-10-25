"""闲鱼卖家客服启动器（保持前台驻留，解决软件启动后被连带关闭）。

为什么必须常驻（关键实测结论）
------------------------------
Electron 应用必须**持有控制台**才能存活。实测三种启动方式：

1. ``subprocess.Popen`` + 日志句柄，父进程退出后约 1 分钟内
   所有 Electron 进程连带消失。
2. ``creationflags=DETACHED_PROCESS``（完全脱离控制台）—— 更糟，
   主进程直接退出，日志 0 字节，什么都拿不到。
3. ``start "" "%EXE%"``（bat 原生）—— 不传句柄时 Electron 可能
   不产生日志输出，无法排查问题。

所以本模块**不退出**，一直守着软件进程：
- 负责启动、持续健康监控、打印实时状态；
- 用户关掉本窗口 / 按 Ctrl+C 时，先询问是否一并关闭软件；
- 软件自己崩溃时立即报错并给出排查建议。

这与「双击启动后软件就关闭」的现象完全吻合：不是软件崩溃，
而是启动器先走，软件被连带收走。

日志同时写入 UTF-8 文件，控制台不会被软件日志刷屏，
需要看中文日志时运行「查看日志.bat」。

用法：
    python launch_app.py              前台驻留（默认，双击 bat 走这条）
    python launch_app.py --detach     短暂驻留确认健康后退出
"""

from __future__ import annotations

# 必须在其他 import 之前：保证中文提示在 Win10 控制台正确显示。
import win10_compat

win10_compat.setup()

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
CONSOLE_LOG = LOG_DIR / "xianyu_console.log"
EXE_NAME = "闲鱼卖家客服.exe"

# 按可能性排序的安装路径候选
def _client_candidates() -> tuple[Path, ...]:
    """客户端候选目录：① 配置里填的 ② 用户目录常见位置 ③ 程序自带的 app/。"""
    cands: list[Path] = []
    try:
        f = BASE_DIR.parent / "data" / "client_path.txt"
        if f.is_file():
            v = f.read_text(encoding="utf-8").strip()
            if v:
                cands.append(Path(v))
    except OSError:
        pass
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        cands.append(Path(local) / "Programs" / "xianyu-seller-im")
    for drive in ("C:/", "D:/", "E:/"):
        cands.append(Path(drive) / "Programs" / "xianyu-seller-im")
    cands.append(BASE_DIR / "app")
    return tuple(cands)


APP_DIR_CANDIDATES = _client_candidates()

# Electron 必需的启动参数。
# --remote-debugging-port=9222 是关键：自动回复脚本（main.py）通过 CDP
# 连这个端口读写聊天界面。历史上漏掉这个参数，导致按「启动软件.bat +
# python main.py」的标准流程永远连接超时，自动回复从根源上不工作。
LAUNCH_ARGS = ("--no-sandbox", "--disable-gpu", "--remote-debugging-port=9222")

# CDP 调试端口（自动回复脚本连接用，也是本启动器的健康信号）
CDP_PORT = 9222

# 健康观察窗口（秒）：覆盖历史上出现过的崩溃间隔
STABILITY_SECONDS = 30

# 结束旧实例后等待句柄释放的时间（秒）
KILL_GRACE = 2.0

# 监控心跳间隔（秒）
POLL_INTERVAL = 5


def find_exe() -> Path | None:
    """定位软件主程序。

    Returns:
        可执行文件路径；所有候选都不存在时返回 None。
    """
    for d in APP_DIR_CANDIDATES:
        if not str(d) or str(d) == "Programs\\xianyu-seller-im":
            continue
        exe = d / EXE_NAME
        if exe.is_file():
            return exe
    return None


def kill_running(proc: "subprocess.Popen[bytes] | None" = None) -> None:
    """关闭已运行的实例，避免多开导致端口与通知冲突。

    先按映像名整树关闭；再按 PID 兜底一次（映像名在某些环境下
    可能匹配不到，按 PID 一定有效）。

    Args:
        proc: 本启动器启动的软件主进程对象，用于按 PID 兜底关闭。
    """
    print("[*] 关闭已运行的实例...")
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/IM", EXE_NAME],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=30,
        )
    except FileNotFoundError:
        print("[!] 未找到 taskkill，跳过关闭旧实例")
        return
    except subprocess.TimeoutExpired:
        print("[!] 关闭旧实例超时，继续启动")
        return
    if proc is not None and proc.poll() is None:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=30,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    time.sleep(KILL_GRACE)


def clean_env() -> dict[str, str]:
    """构造清理过的环境变量。

    ELECTRON_RUN_AS_NODE=1 会让 Electron 主进程退化成普通 Node，
    于是拒绝 --no-sandbox 等 Chromium 参数并报 bad option，必须清除。
    PYTHONUTF8 会影响本脚本自身的中文输出（常由 IDE/CI 注入）。

    Returns:
        可直接传给 Popen 的环境变量字典。
    """
    env = dict(os.environ)
    for key in ("ELECTRON_RUN_AS_NODE", "NODE_OPTIONS", "PYTHONUTF8", "PYTHONIOENCODING"):
        env.pop(key, None)
    return env


def count_instances() -> int:
    """统计当前运行的闲鱼软件进程数（仅供参考，不作生死判据）。

    注意：此函数在某些环境（如被安全软件/沙箱托管的进程树）下会漏计——
    实测应用刚启动的头几十秒内 tasklist 完全看不到它的进程，
    但应用其实活着。因此启动器不能仅凭它判断软件已死，
    生死判据用 proc.poll()，见 wait_stable/supervise。

    Returns:
        进程数量；tasklist 不可用时返回 -1。
    """
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {EXE_NAME}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=20,
            check=False,
        ).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return -1
    # tasklist 输出为 GBK，且进程名含中文，必须按 GBK 解码后再计数
    return out.decode("gbk", errors="replace").count(EXE_NAME)


def port_open(port: int = CDP_PORT) -> bool:
    """探测 CDP 调试端口是否已就绪（应用完全启动的标志）。"""
    import socket
    with socket.socket() as s:
        s.settimeout(1.0)
        return s.connect_ex(("127.0.0.1", port)) == 0


def start_app(exe: Path) -> "subprocess.Popen[bytes] | None":
    """启动软件，输出重定向到 UTF-8 日志文件。

    注意：不能使用 DETACHED_PROCESS。那样进程失去控制台后
    Electron 主进程会直接退出（实测日志 0 字节）。

    Args:
        exe: 主程序路径。

    Returns:
        进程对象；启动失败返回 None。
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_handle = open(CONSOLE_LOG, "wb")
    try:
        proc = subprocess.Popen(  # noqa: S603
            [str(exe), *LAUNCH_ARGS],
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=clean_env(),
            cwd=str(exe.parent),
            close_fds=True,
        )
    except OSError as exc:
        log_handle.close()
        print(f"[x] 启动失败: {exc}")
        return None

    # 句柄交由子进程与本模块共同持有，不在此关闭。
    # 子进程退出时本模块会负责关闭。
    _LOG_HANDLES.append(log_handle)
    return proc


# 持有日志句柄引用，避免被 GC 提前关闭
_LOG_HANDLES: list = []


def wait_stable(proc: "subprocess.Popen[bytes]", seconds: int) -> tuple[bool, str]:
    """在观察窗口内确认软件是否稳定存活。

    生死判据是 proc.poll()（父进程握着子进程句柄，退出立即可见），
    不拿 tasklist 计数当判据——实测该计数在软件刚启动的几十秒内
    可能完全看不到进程（安全软件/沙箱环境），曾导致启动器误判
    「进程全部消失」而退出，把软件连带杀掉。CDP 端口就绪作为
    「完全启动」的加分信号打印出来。

    Args:
        proc: 主进程对象。
        seconds: 观察时长。

    Returns:
        (是否稳定, 描述) 二元组。
    """
    elapsed = 0
    port_ready_at = -1
    while elapsed < seconds:
        time.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL
        rc = proc.poll()
        if rc is not None:
            return False, f"软件主进程在 {elapsed} 秒后退出（退出码 {rc}）"
        if port_ready_at < 0 and port_open():
            port_ready_at = elapsed
        state = "调试端口就绪" if port_ready_at > 0 else "启动中"
        print(f"[*] {elapsed:3d} 秒  主进程存活（PID {proc.pid}）  {state}")
    if port_ready_at > 0:
        return True, f"稳定（{port_ready_at} 秒时调试端口就绪）"
    return True, "稳定（调试端口尚未就绪，聊天页可能仍在加载）"


def supervise(proc: "subprocess.Popen[bytes]") -> int:
    """常驻守护，直到软件退出或用户中断。

    这是关键：Electron 必须有控制台才能存活，启动器不能提前退出。
    软件关闭时本进程才随之结束。

    Args:
        proc: 主进程对象。

    Returns:
        退出码。
    """
    print()
    print("=" * 60)
    print("  软件已启动，本窗口需保持打开")
    print("=" * 60)
    print()
    print("  · 直接关闭本窗口会一并关闭软件")
    print("  · 想退出软件，请在此窗口按 Ctrl+C")
    print("  · 按 Ctrl+C 后可选择是否同时关闭软件")
    print()
    print(f"  调试端口: 127.0.0.1:{CDP_PORT}（自动回复脚本通过它连接）")
    print(f"  日志文件: {CONSOLE_LOG}")
    print(f"  查看中文日志: 双击「查看日志.bat」（可另开窗口）")
    print()
    print("-" * 60)

    port_noted = False
    try:
        while True:
            time.sleep(POLL_INTERVAL)
            rc = proc.poll()
            if rc is not None:
                print()
                print(f"[!] 软件已退出（退出码 {rc}）")
                print(f"    日志: {CONSOLE_LOG}")
                print()
                print("    若反复退出，常见原因：")
                print("      1. ToDesk / 向日葵等远程软件的虚拟显示驱动冲突")
                print("      2. 上一次进程未退干净，文件锁残留")
                print("      3. 显卡驱动过旧")
                print()
                print("    可先重启电脑再试，或把日志内容发出来分析。")
                return rc or 0
            if not port_noted and port_open():
                port_noted = True
                print(f"[+] 调试端口 {CDP_PORT} 已就绪，可以运行 "
                      f"python main.py 启动自动回复")
    except KeyboardInterrupt:
        print()
        print()
        answer = input("是否同时关闭闲鱼软件？输入 y 关闭，直接回车保留：").strip().lower()
        if answer in ("y", "yes"):
            kill_running(proc)
            print("[*] 已关闭软件，再见。")
            return 0
        print("[*] 软件将继续运行，本窗口即将关闭。")
        print("[*] 提示：软件依赖本窗口的控制台，窗口关闭后软件会一并退出。")
        return 0


def main() -> int:
    """启动器入口。

    Returns:
        进程退出码。
    """
    parser = argparse.ArgumentParser(
        description="闲鱼卖家客服启动器（保持前台驻留，避免软件被连带关闭）"
    )
    parser.add_argument(
        "--detach",
        action="store_true",
        help="确认健康后立即退出（注意：可能导致软件被连带关闭，不推荐）",
    )
    args = parser.parse_args()

    exe = find_exe()
    if exe is None:
        print("[x] 找不到闲鱼卖家客服主程序，已尝试以下路径：")
        for d in APP_DIR_CANDIDATES:
            if str(d) and str(d) != "Programs\\xianyu-seller-im":
                print(f"    {d / EXE_NAME}")
        print()
        print("请确认软件已安装，或修改 launch_app.py 里的 APP_DIR_CANDIDATES。")
        return 1

    kill_running()

    print(f"[*] 启动: {exe.name} {' '.join(LAUNCH_ARGS)}")
    proc = start_app(exe)
    if proc is None:
        return 1
    print(f"[+] 进程已启动，PID={proc.pid}")

    print()
    print(f"[*] 正在观察 {STABILITY_SECONDS} 秒确认稳定性...")
    ok, detail = wait_stable(proc, STABILITY_SECONDS)
    if not ok:
        print(f"[x] {detail}")
        print(f"    日志: {CONSOLE_LOG}")
        return 1

    print()
    print(f"[+] 启动成功，{detail}")

    if args.detach:
        print("[!] --detach 模式：启动器即将退出，软件可能被连带关闭。")
        return 0

    return supervise(proc)


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        for h in _LOG_HANDLES:
            try:
                h.close()
            except Exception:
                pass