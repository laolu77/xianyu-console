"""图形卸载程序（深色 UI，复用 console_ui 的设计令牌）。

============================================================================
为什么重写
============================================================================
旧版 `uninstall.py` 是命令行版，两个硬伤：
  1. **没有界面**：用户在控制台里敲 y 确认，不符合"装的时候是图形向导"的体验；
  2. **删不掉自己**：延迟删除写的是
       subprocess.Popen('timeout /t 2 >nul & rd /s /q "..."', shell=True, ...)
     `timeout` 在没有控制台句柄时会直接报错退出（DETACHED + NO_WINDOW），
     后面的 `rd` 于是根本不执行 —— 表现就是"卸载完了目录还在"。
     这里改用 `ping -n 3`（不需要控制台）+ **列表参数**（不经过 shell 字符串解析，
     中文路径不会被编码搞坏）。

============================================================================
不删什么
============================================================================
卡密、商品、规则、订单都在 **MySQL** 里（安装时填的那个库），不在安装目录。
所以删程序目录不会丢业务数据。安装目录下 data/ 只有本机配置
（数据库连接、登录密码、内网穿透、日志）——是否一并删除交给用户勾选。

用法：
    python uninstall_ui.py                 # 图形界面
    python uninstall_ui.py --silent --yes  # 无界面静默卸载（供批处理/测试）
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

def _app_dir() -> Path:
    """安装目录。

    【必须区分打包前后】PyInstaller onefile 运行时会把程序解压到
    `%TEMP%\\_MEIxxxx`，此时 `__file__` 指向那个临时目录 —— 直接用
    `Path(__file__).parent` 会去删 `_MEI`（把卸载器自己删崩），
    而真正的安装目录纹丝不动（2026-11-03 实测：日志里打印的
    "安装目录" 是 _MEI000029dc2，安装目录一个文件都没删）。
    打包后用 `sys.executable` 的父目录才正确。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BASE = _app_dir()
TITLE = "卸载 闲鱼客服控制台"
NO_WINDOW = 0x08000000
DETACHED = 0x00000008

# 复用主控制台的字体/颜色令牌，保证视觉一致
sys.path.insert(0, str(BASE))
try:
    import tkinter as tk
    from tkinter import font as tkfont
    from console_ui import C, FONT_UI, FONT_UI_SM, FlatButton
    from uninstaller_ui import Uninstaller
except Exception:  # noqa: BLE001
    tk = None
    Uninstaller = None


# --------------------------------------------------------------------------
# 卸载动作
# --------------------------------------------------------------------------
def self_names() -> set[str]:
    """当前卸载器自身的进程名（不能杀、不能删）。"""
    names = {Path(sys.executable).name.lower(), "xianyuuninstall.exe",
             "uninstall_ui.exe", "python.exe", "pythonw.exe"}
    return names


def _run(args, timeout=15) -> None:
    try:
        subprocess.run(args, capture_output=True, timeout=timeout,
                       creationflags=NO_WINDOW)
    except Exception:  # noqa: BLE001
        pass


def kill_processes(log) -> None:
    """结束占用安装目录的进程。**绝不杀卸载器自己**。"""
    for exe in ("php.exe", "xianyu_console.exe"):
        log(f"  结束 {exe} …")
        _run(["taskkill", "/f", "/im", exe])

    # 引擎：只杀命令行里带本安装目录的 python，别误伤别的 Python 项目
    try:
        import psutil
        killed = 0
        for p in psutil.process_iter(["pid", "name", "cmdline", "exe"]):
            try:
                if (p.info.get("name") or "").lower() not in (
                        "python.exe", "pythonw.exe"):
                    continue
                cmd = " ".join(p.info.get("cmdline") or [])
                exe_path = p.info.get("exe") or ""
                # 判断依据：解释器或脚本位于本安装目录
                if str(BASE).lower() not in (cmd + " " + exe_path).lower():
                    continue
                if p.info["pid"] == os.getpid():
                    continue
                p.kill()
                killed += 1
            except Exception:  # noqa: BLE001
                continue
        if killed:
            log(f"  结束 {killed} 个引擎进程")
    except Exception:  # noqa: BLE001
        # 没有 psutil 就退回到"带本目录的 python"粗筛。
        # 【不能用 wmic】Win11 已默认移除 WMIC（可选功能），而且本机安全策略
        # 直接把 wmic.exe 拉黑 —— 回退分支等于永远失效。改用 PowerShell 的
        # Get-CimInstance（和 wmic 同源，但系统保证存在）。
        try:
            ps = (
                "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                "ForEach-Object { $_.ProcessId.ToString() + '|' + $_.CommandLine }"
            )
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True, text=True, timeout=20,
                creationflags=NO_WINDOW).stdout or ""
            base_l = str(BASE).lower()
            for line in out.splitlines():
                if base_l not in line.lower():
                    continue
                pid = line.split("|", 1)[0].strip()
                if pid.isdigit() and int(pid) != os.getpid():
                    _run(["taskkill", "/f", "/pid", pid])
        except Exception:  # noqa: BLE001
            pass


def remove_shortcuts(log) -> None:
    """删除桌面 / 开始菜单快捷方式（含卸载入口本身）。"""
    home = Path(os.environ.get("USERPROFILE", "C:/"))
    appdata = Path(os.environ.get("APPDATA", "C:/"))
    prog = appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    targets = [
        home / "Desktop" / "闲鱼客服控制台.lnk",
        home / "Desktop" / "卸载 闲鱼客服控制台.lnk",
        prog / "闲鱼客服控制台.lnk",
        prog / "卸载 闲鱼客服控制台.lnk",
        prog / "闲鱼客服控制台" / "闲鱼客服控制台.lnk",
        prog / "闲鱼客服控制台" / "卸载 闲鱼客服控制台.lnk",
    ]
    n = 0
    for t in targets:
        try:
            if t.is_file():
                t.unlink()
                n += 1
        except Exception:  # noqa: BLE001
            pass
    log(f"  删除快捷方式 {n} 个")


def delete_tree(path: Path, skip: set[str], log, label: str) -> int:
    """删除目录内容，跳过 skip 里的名字（通常是卸载器自己）。返回删除项数。"""
    if not path.is_dir():
        return 0
    n = 0
    for item in sorted(path.iterdir(), key=lambda p: (p.is_dir(), p.name)):
        if item.name.lower() in skip:
            continue
        try:
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink()
            n += 1
        except Exception:  # noqa: BLE001
            pass
    log(f"  {label}：删除 {n} 项")
    return n


def schedule_self_delete(base: Path, log) -> None:
    """本进程退出后，删除整个安装目录（含卸载器自己）。

    踩过的坑（2026-11-03）：
      1. `timeout /t 2` 依赖控制台输入句柄，在无窗口模式下直接失败退出，
         后面的 `rd` 根本不执行 → 目录永远残留。改用 `ping -n 3`。
      2. **`DETACHED_PROCESS` 不能隐藏窗口**（2026-11-03 用户截图：
         卸载完成后弹出黑底 cmd 窗口，里面在刷 ping 输出）。
         DETACHED 的语义只是"不继承父进程的控制台"，而 cmd.exe 是
         控制台程序，系统会给它**新建一个控制台窗口** —— 看起来就像
         程序弹了个黑框。真正隐藏窗口要用 `CREATE_NO_WINDOW`。
         （早先把它和 DETACHED 一起用导致进程起不来，所以只用 NO_WINDOW。）
      3. PyInstaller onefile 的 exe 退出时还要清理 `_MEI` 临时目录，
         2 秒不一定够；exe 句柄释放也有延迟 → 用循环重试。
         **但重试必须有上限**：旧版是无限 `goto retry`，一旦删不掉
         （杀软锁文件、用户没关窗口）就永远 ping 下去，窗口一直挂着。
         现在最多 20 次（约 40 秒），到点就自己退出。
      4. 所有输出都重定向到 nul：即使窗口意外可见，也不该刷屏。
    """
    tmp = Path(os.environ.get("TEMP") or "C:/Windows/Temp")
    bat = tmp / f"_xianyu_uninst_{os.getpid()}.bat"
    # 中文安装路径：cmd 默认按 GBK 解析批处理，纯 ASCII 时用 ascii 更安全
    try:
        str(base).encode("ascii")
        enc = "ascii"
    except UnicodeEncodeError:
        enc = "gbk"
    # 不用 `(...)` 复合块：路径里带括号/引号时容易解析错，
    # 用 goto 拼平铺结构最稳。
    script = "\r\n".join([
        "@echo off",
        "setlocal",
        "set /a tries=0",
        "ping -n 3 127.0.0.1 >nul 2>&1",
        ":retry",
        f'rd /s /q "{base}" >nul 2>&1',
        f'if not exist "{base}" goto done',
        "set /a tries+=1",
        "if %tries% GEQ 20 goto done",
        "ping -n 2 127.0.0.1 >nul 2>&1",
        "goto retry",
        ":done",
        'del "%~f0" >nul 2>&1',
    ]) + "\r\n"
    try:
        bat.write_text(script, encoding=enc, errors="replace")
        subprocess.Popen(["cmd", "/c", str(bat)],
                         creationflags=NO_WINDOW,
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         close_fds=True)
        log("  已安排退出后清理安装目录（约 2~40 秒，无窗口）")
    except Exception as exc:  # noqa: BLE001
        log(f"  安排清理失败：{exc}")


def do_uninstall(del_data: bool, log, progress=None, scope: str = "full") -> None:
    """执行卸载。

    Args:
        del_data: True 时连 data/（本机配置、登录密码、日志）一起删。
        log: 进度文本输出函数。
        progress: `progress(ratio, text)` 回调，供界面更新进度条；
                  命令行版传 None 即可。
        scope: "full"=删程序文件；"shortcut"=只删快捷方式，程序文件保留。

    顺序固定：停引擎 → 删程序文件 → 清快捷方式 → 清本机配置。
    """
    say = progress or (lambda ratio, text: None)
    full = (scope != "shortcut")

    log(f"安装目录：{BASE}")
    if not full:
        log("模式：仅删除快捷方式（程序文件保留）")

    say(0.08, "正在停止引擎进程…")
    kill_processes(log)
    time.sleep(0.8)

    if full:
        say(0.35, "正在删除程序文件…")
        skip = {Path(sys.executable).name.lower(), "uninstall.py",
                "uninstall_ui.py", "xianyuuninstall.exe"}
        if not del_data:
            skip |= {"data"}
        delete_tree(BASE, skip, log, "程序文件")
    else:
        say(0.35, "已跳过程序文件")

    say(0.62, "正在清理快捷方式…")
    remove_shortcuts(log)

    if del_data and full:
        say(0.80, "正在删除本机配置 data/ …")
        delete_tree(BASE / "data", set(), log, "配置与日志")
    else:
        say(0.80, "保留 data/（数据库连接、登录密码、日志）")

    if full:
        say(0.93, "正在安排退出后清理…")
        schedule_self_delete(BASE, log)
    else:
        say(1.0, "完成")
    log("完成。")


# --------------------------------------------------------------------------
# 图形界面
# --------------------------------------------------------------------------
def _enable_dpi_awareness() -> None:
    """声明 DPI 感知。

    不声明的话，Windows 在 125%/150% 缩放下会把 Tk 窗口当位图拉伸：
    设定 560x470 实际渲染出来又小又糊，文字挤成一团（2026-11-03 实测截图）。
    声明之后 Tk 用真实物理像素，界面按设计尺寸呈现。
    """
    try:
        import ctypes
        # 2 = Per-Monitor DPI Aware；1 = System DPI Aware（更兼容）
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass


def _chdir_away() -> None:
    """把进程工作目录切到 %TEMP%。

    【必须做】桌面/开始菜单快捷方式的 WorkingDirectory 就是安装目录，
    进程的 cwd 落在那里时 Windows **不允许删除"当前工作目录"** ——
    文件能删光，但目录永远剩个空壳（2026-11-03 实测：
    文件剩 0 个，目录 16 秒都没消失）。
    切到 TEMP 之后 rd 才能把安装目录本身也删掉。
    """
    try:
        os.chdir(os.environ.get("TEMP") or "C:/Windows/Temp")
    except Exception:  # noqa: BLE001
        pass


def run_gui() -> int:
    """图形卸载界面。

    界面层用 uninstaller_ui.Uninstaller（设计见 UNINSTALL_SPEC.md），
    业务层仍是 do_uninstall()，两者通过 on_confirm 回调解耦。
    """
    _chdir_away()
    _enable_dpi_awareness()

    root = tk.Tk()
    root.title(TITLE)
    root.configure(bg=C["bg_app"])
    root.geometry("700x750")
    root.resizable(True, True)
    # 置于最前：卸载是短时操作，被浏览器挡住会让用户以为没反应
    try:
        root.attributes("-topmost", True)
    except Exception:  # noqa: BLE001
        pass

    root.update_idletasks()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    w, h = 700, 750
    root.geometry("%dx%d+%d+%d" % (w, h, (sw - w) // 2, max(30, (sh - h) // 3)))

    un = Uninstaller(root, install_dir=str(BASE), version="v2.0.1")
    un.set_install_dir(str(BASE))

    def fit_window():
        """按实际溢出量逐次加高窗口（最多 4 轮，够了就停）。

        为什么不写死高度：系统缩放 125% / 150% 时字体和间距一起变大，
        100% 下刚好的尺寸到 125% 就会超出可视区、右侧冒出滚动条。
        也不信 `inner.winfo_reqheight()`：首次 Configure 阶段量到的值偏小，
        据此算出来的窗口仍然装不下。直接比较 **画布内容 bbox 与可视高度**，
        溢出多少加多少，直到不再溢出，最稳。
        """
        try:
            for _ in range(4):
                root.update_idletasks()
                bbox = un.canvas.bbox("all")
                view_h = un.canvas.winfo_height()
                if not bbox or view_h <= 1:
                    break
                overflow = bbox[3] - view_h
                if overflow <= 0:
                    break
                cur_w, cur_h = root.winfo_width(), root.winfo_height()
                new_h = min(cur_h + overflow + 4, sh - 70)
                if new_h <= cur_h:
                    break
                root.geometry("%dx%d+%d+%d" % (
                    cur_w, new_h, max(0, root.winfo_x()),
                    max(8, root.winfo_y() - overflow // 2)))
            # 加高后可能已贴到屏幕上沿，重新居中一次
            root.update_idletasks()
            ww, wh = root.winfo_width(), root.winfo_height()
            root.geometry("+%d+%d" % (max(0, (sw - ww) // 2),
                                      max(8, (sh - wh) // 3)))
        except Exception:  # noqa: BLE001
            pass

    root.after(60, fit_window)

    # ---- 后台线程 → 主线程 的界面更新队列 ----
    # tkinter 不是线程安全的，工作线程只能往队列里塞闭包，由主线程定时消费。
    pending = []

    def pump():
        while pending:
            fn = pending.pop(0)
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass
        try:
            root.after(80, pump)
        except Exception:  # noqa: BLE001
            pass

    def post(fn):
        pending.append(fn)

    logfile = Path(os.environ.get("TEMP") or "C:/Windows/Temp") / "xianyu_uninstall.log"

    def log(msg: str) -> None:
        """卸载过程没有日志框了（新设计是进度条 + 阶段文字），
        明细仍落一份到 %TEMP%，出问题可查。"""
        try:
            with open(logfile, "a", encoding="utf-8") as f:
                f.write(msg + "\n")
        except Exception:  # noqa: BLE001
            pass

    def on_confirm(opts):
        del_data = bool(opts.get("delete_data"))
        scope = opts.get("scope") or "full"

        def worker():
            try:
                try:
                    logfile.unlink()
                except Exception:  # noqa: BLE001
                    pass
                do_uninstall(
                    del_data, log,
                    progress=lambda r, t: post(lambda: un.set_progress(r, t)),
                    scope=scope)
                time.sleep(0.5)
                post(lambda: un.finish(True, "卸载完成"))
                if scope == "full":
                    # 程序文件已删，进程退出后清理脚本才能把目录和 exe 一起删掉
                    post(lambda: root.after(2600, root.destroy))
            except Exception as exc:  # noqa: BLE001
                log("卸载出错：%s: %s" % (type(exc).__name__, exc))
                msg = "%s: %s" % (type(exc).__name__, exc)
                post(lambda: un.finish(False, "卸载未完成"))

        threading.Thread(target=worker, daemon=True).start()

    un.on_confirm = on_confirm
    un.on_cancel = root.destroy
    root.protocol("WM_DELETE_WINDOW",
                  lambda: None if un.running else root.destroy())

    log("开始卸载。安装目录：%s" % BASE)
    pump()
    root.mainloop()
    return 0


def run_silent(del_data: bool) -> int:
    _chdir_away()

    def log(msg: str) -> None:
        print(msg)
    do_uninstall(del_data, log)
    return 0


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    args = [a.lower() for a in sys.argv[1:]]
    if "--silent" in args:
        return run_silent("--del-data" in args)
    if tk is None:
        print("当前环境没有 tkinter，改用静默卸载。")
        return run_silent(True)
    return run_gui()


if __name__ == "__main__":
    sys.exit(main())
