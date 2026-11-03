"""一键产出**两份**安装包（用户 2026-11-03 定的发版规范）。

为什么要两份：
  · 大安装包（Full）：把 payload.zip 塞进 exe，约 110MB。
    完全离线，拷 U 盘就能装；缺点是每次发版都要重传 100MB。
  · 小安装包（Lite）：exe 只有约 12MB，安装时从服务器下载 payload.zip。
    分发快、更新便宜；缺点是要保证服务器上的 payload.zip 是最新的。

所以每次发版**两个都出**，按场景挑一个用。

用法：
    python tools/build_release.py                 # 全自动：重打控制台+卸载器+payload+两个安装包
    python tools/build_release.py --skip-exe      # 只重打 payload 和两个安装包（改后端时够用）
    python tools/build_release.py --url http://x/payload.zip
    python tools/build_release.py --skip-payload  # 复用已有 payload.zip

产物（都在 ../_build 下）：
    payload.zip                        服务器端（小包安装时下载它）
    dist_gui/XianyuSetup-Full.exe      大安装包（离线）
    dist_gui/XianyuSetup.exe           小安装包（服务器下载）
    dist_gui/payload_url.txt           小包的下载地址（可改，不需重打包）
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # xianyu-software
WORKSPACE = ROOT.parent
BUILD = WORKSPACE / "_build"
DIST_GUI = BUILD / "dist_gui"
DEFAULT_PY = Path(r"<用户名>/AppData/Local/Programs/Python/Python312/python.exe")
DEFAULT_URL = "http://124.221.253.153/xianyu/payload.zip"
# 打包小包时要确保「没有」payload.zip 参与，否则会退化成大包


def human(n: int) -> str:
    return "%.1f MB" % (n / 1048576)


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def pyinstaller(py: Path, name: str, src: Path, extra: list[str], dist: Path,
                work: Path) -> None:
    cmd = [str(py), "-m", "PyInstaller", "--onefile", "--windowed",
           "--name", name, "--distpath", str(dist), "--workpath", str(work),
           "--specpath", str(BUILD), "--noconfirm", *extra, str(src)]
    print(f"[release] 打包 {name} …")
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    exe = dist / (name + ".exe")
    if r.returncode != 0 or not exe.is_file():
        tail = (r.stderr or r.stdout or "")[-600:]
        raise SystemExit(f"打包 {name} 失败：\n{tail}")


def zip_payload() -> Path:
    src = BUILD / "payload"
    out = BUILD / "payload.zip"
    if out.exists():
        out.unlink()
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for f in src.rglob("*"):
            if f.is_file():
                try:
                    z.write(f, f.relative_to(src).as_posix())
                    n += 1
                except OSError:
                    pass
    print(f"[release] payload.zip：{n} 个文件，{human(out.stat().st_size)}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--py", default=str(DEFAULT_PY), help="打 exe 用的系统 Python")
    ap.add_argument("--url", default=DEFAULT_URL, help="小安装包的内置下载地址")
    ap.add_argument("--skip-exe", action="store_true",
                    help="不重打控制台/卸载器（只改后端时用）")
    ap.add_argument("--skip-payload", action="store_true",
                    help="复用已有 payload.zip")
    args = ap.parse_args()
    py = Path(args.py)
    t0 = time.time()
    DIST_GUI.mkdir(parents=True, exist_ok=True)

    # 1) 控制台 exe / 卸载器（payload 会用到，改了它们必须重打）
    if not args.skip_exe:
        pyinstaller(py, "xianyu_console", ROOT / "app_launcher.py",
                    ["--hidden-import", "pymysql", "--paths", str(ROOT)],
                    BUILD / "dist_console", BUILD / "work_console")
        pyinstaller(py, "XianyuUninstall", ROOT / "uninstall_ui.py",
                    ["--hidden-import", "psutil", "--paths", str(ROOT)],
                    BUILD / "dist_uninstall", BUILD / "work_uninstall")

    # 2) payload 目录 + zip
    if args.skip_payload and (BUILD / "payload.zip").is_file():
        pz = BUILD / "payload.zip"
        print(f"[release] 复用 {pz}（{human(pz.stat().st_size)}）")
    else:
        print("[release] 生成 payload …")
        r = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "build_payload.py")],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            raise SystemExit("build_payload 失败：\n" + (r.stdout or r.stderr)[-800:])
        for line in (r.stdout or "").splitlines():
            if "剔除" in line or "脱敏" in line or "payload 完成" in line:
                print("  " + line)
        pz = zip_payload()

    # 3) 大安装包：payload 打进 exe（离线可用）
    pyinstaller(py, "XianyuSetup-Full", ROOT / "installer" / "setup_gui.py",
                ["--hidden-import", "pymysql", "--hidden-import", "bcrypt",
                 "--paths", str(ROOT / "installer"),
                 "--add-data", f"{pz};."],
                DIST_GUI, BUILD / "work_gui")

    # 4) 小安装包：不带 payload，运行时从服务器下载
    pyinstaller(py, "XianyuSetup", ROOT / "installer" / "setup_gui.py",
                ["--hidden-import", "pymysql", "--hidden-import", "bcrypt",
                 "--paths", str(ROOT / "installer")],
                DIST_GUI, BUILD / "work_stub")

    # 5) 地址文件（改地址只要改这个 txt，不用重打包）
    (DIST_GUI / "payload_url.txt").write_text(args.url + "\n", encoding="utf-8")

    # 6) 清单
    full = DIST_GUI / "XianyuSetup-Full.exe"
    lite = DIST_GUI / "XianyuSetup.exe"
    print()
    print("=" * 72)
    print("发版产物")
    print("=" * 72)
    rows = [
        ("大安装包（离线）", full),
        ("小安装包（服务器）", lite),
        ("服务器端 payload", pz),
    ]
    for label, f in rows:
        print(f"  {label:<18} {f.name:<24} {human(f.stat().st_size):>9}")
        print(f"  {'':<18} SHA256 {sha256(f)}")
    print()
    print(f"  小包下载地址：{args.url}")
    print(f"  发布提醒：payload.zip 必须和这版代码一致，装之前先把它传到服务器。")
    print(f"  用时 {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
