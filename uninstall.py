"""卸载程序：删除程序文件与桌面/开始菜单快捷方式。

============================================================================
重要：不删数据
============================================================================
卡密、商品、规则、订单都存在 **MySQL 数据库**里（你安装时填的那个库），
不在安装目录。所以删掉程序目录不会丢业务数据。
安装目录下的 data/ 只有本机配置（数据库连接、登录密码、内网穿透设置、日志）。

如果换台电脑或重装，想把配置也清掉，删完程序再把 data/ 手动删掉即可。

============================================================================
为什么用 Python 而不是纯 bat
============================================================================
.bat 的中文提示在不同代码页下会乱码（项目里已有血泪教训：bat 一律纯 ASCII）。
所以卸载逻辑放 Python（中文输出可控），bat 只做一行转发。
删除自己所在目录用「延迟执行」：本脚本退出后由 cmd 再删。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
CREATE_NO_WINDOW = 0x08000000
DETACHED = 0x00000008


def kill_processes() -> None:
    """结束占用安装目录的进程（否则文件删不掉）。"""
    for exe in ("php.exe", "xianyu_console.exe"):
        try:
            subprocess.run(["taskkill", "/f", "/im", exe],
                           capture_output=True, timeout=10,
                           creationflags=CREATE_NO_WINDOW)
        except Exception:  # noqa: BLE001
            pass
    # 引擎：只杀命令行里带本安装目录的那个 python，别误杀别的
    try:
        out = subprocess.run(
            ["wmic", "process", "where", "name='python.exe'", "get",
             "ProcessId,CommandLine", "/format:csv"],
            capture_output=True, text=True, timeout=10,
            creationflags=CREATE_NO_WINDOW)
        for line in (out.stdout or "").splitlines():
            if str(BASE).lower() in line.lower():
                parts = line.rsplit(",", 1)
                if len(parts) == 2 and parts[1].strip().isdigit():
                    subprocess.run(["taskkill", "/f", "/pid", parts[1].strip()],
                                   capture_output=True, timeout=8,
                                   creationflags=CREATE_NO_WINDOW)
    except Exception:  # noqa: BLE001
        pass


def remove_shortcuts() -> None:
    """删除桌面与开始菜单的快捷方式（含卸载入口自身）。"""
    home = Path(os.environ.get("USERPROFILE", "C:/"))
    appdata = Path(os.environ.get("APPDATA", "C:/"))
    targets = [
        home / "Desktop" / "闲鱼客服控制台.lnk",
        home / "Desktop" / "卸载 闲鱼客服控制台.lnk",
        appdata / "Microsoft/Windows/Start Menu/Programs" / "闲鱼客服控制台.lnk",
        appdata / "Microsoft/Windows/Start Menu/Programs" / "卸载 闲鱼客服控制台.lnk",
    ]
    for t in targets:
        try:
            if t.is_file():
                t.unlink()
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    print("=" * 56)
    print("  卸载  闲鱼客服控制台")
    print("=" * 56)
    print(f"\n程序目录：{BASE}\n")
    print("说明：")
    print("  · 数据库里的卡密、商品、订单**不会**被删除（在你装的 MySQL 里）")
    print("  · 会删除：程序文件、桌面和开始菜单的快捷方式、本机配置与日志")
    print()
    ans = input("确定卸载吗？输入 y 回车确认，其他任意键取消：").strip().lower()
    if ans not in ("y", "yes"):
        print("\n已取消，什么都没删。")
        return 0

    print("\n正在停止相关进程…")
    kill_processes()
    time.sleep(0.6)

    print("正在删除快捷方式…")
    remove_shortcuts()

    # 先尽量删掉非自身文件，再把整个目录交给延迟任务（脚本自己删不掉自己）
    print("正在删除程序文件…")
    for item in sorted(BASE.iterdir(), key=lambda p: p.name):
        if item.name == "uninstall.py":
            continue
        try:
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink()
        except Exception:  # noqa: BLE001
            pass

    print("\n✓ 卸载完成。目录会在几秒后自动清除。")
    print("  （如果残留了文件夹，直接手动删除即可。）\n")
    time.sleep(1.2)

    # 延迟删除整个目录（等本进程退出后执行）
    try:
        subprocess.Popen(
            f'timeout /t 2 >nul & rd /s /q "{BASE}"',
            shell=True, creationflags=CREATE_NO_WINDOW | DETACHED,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
