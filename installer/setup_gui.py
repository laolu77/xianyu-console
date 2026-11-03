"""闲鱼客服控制台 · 安装向导（UI 层用 installer_ui 组件库）。

============================================================================
结构
============================================================================
- **配置三步**：欢迎使用（安装位置）→ 数据库配置 → 客户端与登录密码
- 点「开始安装」后切到安装进度页，完成后显示结果与「打开控制台」

业务逻辑（建库、解压、写配置、建快捷方式、起 PHP）与原版完全一致，
只是把界面换成了 `installer_ui` 的组件库（深色令牌 + 步骤条 + 卡片单选）。

============================================================================
打包
============================================================================
用**系统 Python 3.12**（项目 runtime 那份精简 Python 没有 tkinter）：

    python -m PyInstaller --onefile --windowed --name XianyuSetup \
        --hidden-import pymysql --hidden-import bcrypt \
        --add-data "payload.zip;." --paths installer installer/setup_gui.py

注意 --paths installer：installer_ui 依赖同目录的 console_ui_installer，
不加这条会解析到项目根目录那份同名但内容不同的 console_ui.py。
"""
from __future__ import annotations

import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import zipfile
from pathlib import Path
from tkinter import filedialog, messagebox

# 让 installer_ui / console_ui_installer 能被找到（打包后它们在 _MEIPASS 根）
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from console_ui_installer import C, FONT_UI, FONT_UI_SM, apply_window_icon  # noqa: E402
from installer_ui import (ClientStep, DbStep, InstallerWizard,  # noqa: E402
                           RoundButton, StatusBanner, WelcomeStep)

APP_NAME = "XianyuConsole"
APP_TITLE = "闲鱼客服控制台"
CREATE_NO_WINDOW = 0x08000000
BASE_PORT = 8082


# ===========================================================================
# 路径与探测
# ===========================================================================
def default_home() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "Programs" / APP_NAME


def payload_zip() -> Path:
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        p = Path(meipass) / "payload.zip"
        if p.is_file():
            return p
    return _HERE / "payload.zip"


def is_installed(dst: Path) -> bool:
    return (dst / "runtime" / "php" / "php.exe").is_file() and \
           (dst / "frontend" / "index.php").is_file()


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def pick_port() -> int:
    for p in range(BASE_PORT, BASE_PORT + 20):
        if port_free(p):
            return p
    return BASE_PORT


def detect_client() -> str:
    cands = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "xianyu-seller-im",
        Path("C:/Program Files/xianyu-seller-im"),
        Path("D:/Programs/xianyu-seller-im"),
    ]
    for c in cands:
        if str(c) != "" and c.is_dir():
            return str(c).replace("\\", "/")
    return ""


# ===========================================================================
# 数据库 / 文件操作（与原版一致）
# ===========================================================================
def db_probe(host: str, port: int, user: str, password: str,
             dbname: str = "") -> tuple[bool, str, str]:
    """测试连接，返回 (是否成功, 标题, 详情)。

    先探端口再登录，把"端口不通"和"账号密码错"区分开，用户才知道该改哪里。
    """
    import pymysql
    try:
        s = socket.create_connection((host, int(port)), timeout=3)
        s.close()
    except OSError:
        # 这里最常见的是「虚拟机里跑安装器、MySQL 装在宿主机」——
        # 虚拟机的 127.0.0.1 是它自己，根本连不到宿主机，必须填宿主机 IP。
        tip = (f"{host}:{port} 端口不通。请确认：\n"
               "· MySQL 已启动（phpStudy 里点「启动」）\n"
               "· 端口是否正确（默认 3306）")
        if host in ("127.0.0.1", "localhost"):
            tip += ("\n· 如果你在虚拟机/另一台电脑上装，MySQL 在宿主机，"
                    "这里要填宿主机 IP，不能填 127.0.0.1")
        return False, "连不上数据库", tip
    try:
        conn = pymysql.connect(host=host, port=int(port), user=user,
                               password=password, charset="utf8mb4",
                               connect_timeout=6)
    except Exception as exc:  # noqa: BLE001
        return False, "登录失败", f"端口是通的，但账号或密码不对（{str(exc)[:60]}）"
    try:
        cur = conn.cursor()
        cur.execute("SELECT VERSION()")
        ver = cur.fetchone()[0]
        detail = f"MySQL {ver}"
        if dbname:
            try:
                cur.execute(f"USE `{dbname}`")
                cur.execute("SHOW TABLES")
                n = len(cur.fetchall())
                detail += f" · 库 {dbname} 已存在（{n} 张表）"
            except Exception:  # noqa: BLE001
                detail += f" · 库 {dbname} 不存在，安装时自动创建"
        return True, "连接成功", detail
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def ensure_database(cfg: dict) -> None:
    import pymysql
    conn = pymysql.connect(host=cfg["db_host"], port=int(cfg["db_port"]),
                           user=cfg["db_user"], password=cfg["db_pass"],
                           charset="utf8mb4", autocommit=True, connect_timeout=8)
    try:
        cur = conn.cursor()
        cur.execute(f"CREATE DATABASE IF NOT EXISTS `{cfg['db_name']}` "
                    "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    finally:
        conn.close()


def write_configs(dst: Path, cfg: dict) -> None:
    data = dst / "data"
    data.mkdir(parents=True, exist_ok=True)
    entry = {"host": cfg["db_host"], "port": int(cfg["db_port"]),
             "user": cfg["db_user"], "pass": cfg["db_pass"], "db": cfg["db_name"]}
    (data / "db_config.json").write_text(
        json.dumps({"mode": "local", "local": entry, "remote": entry},
                   ensure_ascii=False, indent=4), encoding="utf-8")
    import bcrypt
    h = bcrypt.hashpw(cfg["console_pass"].encode("utf-8"), bcrypt.gensalt()).decode()
    (data / "auth.json").write_text(
        json.dumps({"user": "admin", "hash": h,
                    "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                   ensure_ascii=False, indent=4), encoding="utf-8")
    if cfg.get("client_path"):
        (data / "client_path.txt").write_text(cfg["client_path"], encoding="utf-8")
    (data / "setup_done.flag").write_text(time.strftime("%Y-%m-%d %H:%M:%S"),
                                          encoding="utf-8")


def make_shortcut(dst: Path) -> None:
    """创建桌面/开始菜单快捷方式。

    优先指向 xianyu_console.exe（桌面控制台：能看到 PHP/MySQL/引擎状态、一键启停）；
    只有 exe 缺失时才退回 bat —— 那种会弹黑框，体验很差。
    """
    console_exe = dst / "xianyu_console.exe"
    starter = dst / "启动控制台.bat"
    if not starter.is_file():
        starter.write_text(
            "@echo off\r\n"
            "chcp 65001 > nul\r\n"
            f'cd /d "{dst}"\r\n'
            f'"{dst}\\runtime\\php\\php.exe" -S 127.0.0.1:{BASE_PORT} -t "{dst}\\frontend"\r\n',
            encoding="utf-8", errors="ignore")

    # 卸载入口放在**安装根目录**：优先独立图形卸载程序（XianyuUninstall.exe，
    # 深色 UI、带进度）；只有 exe 缺失时才退回 bat + uninstall.py（命令行版）。
    uninst_exe = dst / "XianyuUninstall.exe"
    if not uninst_exe.is_file():
        uninst_bat = dst / "卸载程序.bat"
        uninst_bat.write_text(
            "@echo off\r\n"
            "chcp 65001 > nul\r\n"
            "title Uninstall\r\n"
            f'"{dst}\\runtime\\python\\python.exe" "{dst}\\uninstall.py"\r\n'
            "if errorlevel 1 pause\r\n",
            encoding="ascii", errors="ignore")

    home = Path(os.environ.get("USERPROFILE", "C:/"))
    menu = Path(os.environ.get("APPDATA", "C:/")) / "Microsoft/Windows/Start Menu/Programs"
    target = console_exe if console_exe.is_file() else starter
    # 卸载程序**只放在安装根目录**（XianyuUninstall.exe），不建桌面/开始菜单入口
    # —— 卸载是低频操作，桌面上多一个"卸载"图标容易被误点；
    # 需要卸载时进安装目录双击即可（2026-11-03 用户要求）。
    pairs = [
        (home / "Desktop" / f"{APP_TITLE}.lnk", target, ""),
        (menu / f"{APP_TITLE}.lnk", target, ""),
    ]
    ps = "$ws = New-Object -ComObject WScript.Shell; " + "".join(
        f'$s = $ws.CreateShortcut("{lnk}"); '
        f'$s.TargetPath = "{tgt}"; '
        f'$s.WorkingDirectory = "{dst}"; '
        f'$s.Save(); '
        for lnk, tgt, _ in pairs)
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, timeout=25,
                       creationflags=CREATE_NO_WINDOW)
    except Exception:  # noqa: BLE001
        pass


def start_php(dst: Path, port: int) -> subprocess.Popen:
    php = dst / "runtime" / "php" / "php.exe"
    logdir = dst / "data" / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    log = open(logdir / "php_server.log", "a", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [str(php), "-S", f"127.0.0.1:{port}", "-t", str(dst / "frontend")],
        cwd=str(dst), stdout=log, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW, close_fds=True)
    for _ in range(40):
        time.sleep(0.25)
        if not port_free(port):
            return proc
        if proc.poll() is not None:
            raise RuntimeError("PHP 启动失败")
    return proc


# ===========================================================================
# 安装进度页（配置完成后替换向导显示）
# ===========================================================================
class ProgressPage(tk.Frame):
    """安装中 / 安装完成：进度条 + 状态条 + 结果摘要。"""

    def __init__(self, master, on_open, on_close):
        super().__init__(master, bg=C["bg_app"])
        self.on_open = on_open
        self.on_close = on_close

        wrap = tk.Frame(self, bg=C["bg_app"])
        wrap.pack(fill="both", expand=True, padx=40, pady=36)

        self.title = tk.Label(wrap, text="正在安装…", bg=C["bg_app"],
                              fg=C["text_1"],
                              font=("Microsoft YaHei UI", 11, "bold"), anchor="w")
        self.title.pack(anchor="w")

        self.sub = tk.Label(wrap, text="程序文件正在释放，请稍候",
                            bg=C["bg_app"], fg=C["text_2"], font=FONT_UI_SM,
                            anchor="w")
        self.sub.pack(anchor="w", pady=(6, 22))

        self.bar = tk.Canvas(wrap, height=8, bg=C["bg_app"],
                             highlightthickness=0, bd=0)
        self.bar.pack(fill="x")
        self.bar.bind("<Configure>", lambda e: self._draw_bar())

        self.pct_lbl = tk.Label(wrap, text="0%", bg=C["bg_app"], fg=C["text_3"],
                                font=FONT_UI_SM, anchor="w")
        self.pct_lbl.pack(anchor="w", pady=(8, 0))

        self.banner = StatusBanner(wrap)
        self.banner.pack(fill="x", pady=(22, 0))

        self.rows_box = tk.Frame(wrap, bg=C["bg_app"])
        self.btns = tk.Frame(wrap, bg=C["bg_app"])
        self.btn_open = RoundButton(self.btns, "打开控制台", "primary", self._open)
        self.btn_close = RoundButton(self.btns, "关闭", "ghost", self._close)
        self._pct = 0

    def _draw_bar(self):
        self.bar.delete("all")
        w = self.bar.winfo_width() or 600
        h = 8
        self.bar.create_rectangle(0, 0, w, h, fill=C["bg_raised"], outline="")
        if self._pct > 0:
            self.bar.create_rectangle(0, 0, int(w * self._pct / 100), h,
                                      fill=C["ok"], outline="")

    def set_progress(self, pct: int, text: str = ""):
        self._pct = max(0, min(100, pct))
        self.pct_lbl.configure(text=f"{self._pct}%")
        if text:
            self.sub.configure(text=text)
        self._draw_bar()

    def show_done(self, rows: list[tuple[str, str]]):
        self.title.configure(text="安装完成")
        self.sub.configure(text="以后可以从桌面快捷方式打开控制台")
        self.banner.set("ok", "安装成功", "程序文件和配置都已就绪")
        for label, value in rows:
            row = tk.Frame(self.rows_box, bg=C["bg_app"])
            row.pack(fill="x", pady=4)
            tk.Label(row, text=label, bg=C["bg_app"], fg=C["text_3"],
                     font=FONT_UI_SM, width=10, anchor="w").pack(side="left")
            tk.Label(row, text=value, bg=C["bg_app"], fg=C["text_1"],
                     font=FONT_UI, anchor="w").pack(side="left")
        self.rows_box.pack(fill="x", pady=(16, 0))
        self.btns.pack(fill="x", pady=(22, 0))
        self.btn_close.pack(side="right")
        self.btn_open.pack(side="right", padx=(0, 8))

    def show_error(self, title: str, detail: str):
        self.title.configure(text="安装失败")
        self.banner.set("err", title, detail)
        self.btns.pack(fill="x", pady=(22, 0))
        self.btn_close.pack(side="right")

    def _open(self):
        if self.on_open:
            self.on_open()

    def _close(self):
        if self.on_close:
            self.on_close()


# ===========================================================================
# 主程序
# ===========================================================================
class SetupApp:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title(f"{APP_TITLE} 安装向导")
        # 内容比 640 高一点（数据库那页字段多），给足高度避免出现滚动条
        self.root.geometry("740x716")
        self.root.minsize(700, 620)
        try:
            apply_window_icon(self.root)
        except Exception:  # noqa: BLE001
            pass
        self._dark_titlebar()

        self.q: queue.Queue = queue.Queue()
        self.dst = default_home()
        self.php_proc = None
        self.installing = False
        self.done_ok = False
        self._last_db_ok = None
        self.prog = None

        self.wiz = InstallerWizard(self.root)

        self.welcome = WelcomeStep(self.wiz.pages[0],
                                   default_dir=str(default_home()),
                                   on_browse=self._pick_dir)
        self.wiz.add_page(self.welcome, 0)

        self.db = DbStep(self.wiz.pages[1])
        self.wiz.add_page(self.db, 1)
        self.db.on_test_cb = self._test_db

        self.client = ClientStep(self.wiz.pages[2],
                                 default_client=detect_client(),
                                 on_browse=self._pick_client)
        self.wiz.add_page(self.client, 2)

        self.wiz.on_next = self._on_next
        self.wiz.on_finish = self._on_finish
        self.wiz.on_exit = self.root.destroy

        if is_installed(self.dst):
            self.welcome.set_desc(
                "这个程序会自动回复买家消息、付款后自动发卡密。\n"
                "检测到已安装过：继续会覆盖程序文件，卡密和订单数据不会丢失。")

        self.wiz.goto(0)
        self.root.after(80, self._poll)

    def _dark_titlebar(self):
        """把系统标题栏也切成深色（否则顶部一条白边和深色界面很割裂）。"""
        try:
            import ctypes
            self.root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            on = ctypes.c_int(1)
            for attr in (20, 19):        # DWMWA_USE_IMMERSIVE_DARK_MODE
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(on), ctypes.sizeof(on))
        except Exception:  # noqa: BLE001
            pass

    # ---------- 目录选择 ----------
    def _pick_dir(self):
        d = filedialog.askdirectory(title="选择安装位置",
                                    initialdir=str(default_home().parent))
        if d:
            self.welcome.f_dir.set(os.path.join(d, APP_NAME))

    def _pick_client(self):
        d = filedialog.askdirectory(title="选择闲鱼客户端目录")
        if d:
            self.client.f_client.set(d.replace("\\", "/"))

    # ---------- 消息泵 ----------
    def _poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "db":
                    _, ok, title, detail = item
                    self.db.btn_test.set_state(True, "测试连接")
                    self.db.status.set("ok" if ok else "err", title, detail)
                    self._last_db_ok = ok
                elif kind == "prog":
                    if self.prog is not None:
                        self.prog.set_progress(item[1], item[2])
                elif kind == "done":
                    if self.prog is not None:
                        self.prog.set_progress(100, "全部完成")
                        self.prog.show_done(item[1])
                    self.done_ok = True
                elif kind == "err":
                    if self.prog is not None:
                        self.prog.show_error(item[1], item[2])
                    self.installing = False
        except queue.Empty:
            pass
        self.root.after(80, self._poll)

    # ---------- 测试连接 ----------
    def _test_db(self):
        v = self.db.values()
        if not v["host"] or not v["user"]:
            self.db.status.set("err", "信息不全", "主机和账号必填")
            return
        if not v["port"].isdigit() or not (1 <= int(v["port"]) <= 65535):
            self.db.status.set("err", "端口不合法", "端口需为 1-65535 的数字")
            return
        self.db.btn_test.set_state(False, "测试中…")
        self.db.status.set("info", "正在连接…", f"{v['host']}:{v['port']}")
        self._last_db_ok = None
        cfg = dict(v)

        def worker():
            ok, title, detail = db_probe(cfg["host"], int(cfg["port"]),
                                         cfg["user"], cfg["password"], cfg["db"])
            self.q.put(("db", ok, title, detail))

        threading.Thread(target=worker, daemon=True).start()

    # ---------- 步骤校验 ----------
    def _on_next(self, step: int) -> bool:
        if step == 0:
            ok, _ = self.welcome.validate()
            if not ok:
                return False
            dst = Path(self.welcome.f_dir.get().strip() or str(default_home()))
            try:
                dst.mkdir(parents=True, exist_ok=True)
                p = dst / ".w"
                p.write_text("x")
                p.unlink()
            except OSError as exc:
                self.welcome.f_dir.set_error("目录不可写，请换一个")
                messagebox.showerror(
                    "无法写入该目录",
                    f"{dst}\n\n{exc}\n\n请换个位置（别装到 Program Files）。")
                return False
            self.dst = dst
            return True

        if step == 1:
            ok, msg = self.db.validate()
            if not ok:
                self.db.status.set("err", "配置有误", msg)
                return False
            # 没测过就先测一次，避免装完才发现连不上
            if self._last_db_ok is not True:
                v = self.db.values()
                self.db.status.set("info", "正在验证连接…", "请稍候")
                ok2, title, detail = db_probe(v["host"], int(v["port"]),
                                              v["user"], v["password"], v["db"])
                if not ok2:
                    self.db.status.set("err", title, detail)
                    return False
                self.db.status.set("ok", title, detail)
                self._last_db_ok = True
            return True

        if step == 2:
            ok, _ = self.client.validate()
            return ok
        return True

    # ---------- 开始安装 ----------
    def _on_finish(self):
        if self.installing:
            return
        ok, _ = self.client.validate()
        if not ok:
            return
        self.installing = True
        self.cfg = self._collect()
        self._show_progress()
        threading.Thread(target=self._worker, daemon=True).start()

    def _collect(self) -> dict:
        d = self.db.values()
        c = self.client.values()
        return {
            "db_host": d["host"], "db_port": d["port"], "db_user": d["user"],
            "db_pass": d["password"], "db_name": d["db"], "db_mode": d["mode"],
            "client_path": c["client"].strip(),
            "console_pass": c["password"],
        }

    def _show_progress(self):
        self.wiz.pack_forget()
        self.prog = ProgressPage(self.root, on_open=self._launch,
                                 on_close=self.root.destroy)
        self.prog.pack(fill="both", expand=True)
        self.prog.set_progress(1, "准备中…")


    def _worker(self):
        try:
            cfg = self.cfg
            self.q.put(("prog", 3, "正在创建数据库…"))
            ensure_database(cfg)

            self.q.put(("prog", 8, "数据库就绪，开始释放文件…"))
            dst = self.dst
            dst.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(payload_zip()) as z:
                infos = z.infolist()
                total = len(infos)
                for i, info in enumerate(infos, 1):
                    z.extract(info, dst)
                    if i % 60 == 0 or i == total:
                        self.q.put(("prog", 8 + i * 84 // total,
                                    f"正在释放文件… {i}/{total}"))

            self.q.put(("prog", 94, "正在写入配置…"))
            write_configs(dst, cfg)

            # 装的时候就把表建好并校验：不再等引擎首次启动才建，
            # 否则一旦建表链路有问题，用户只会看到"引擎启动后立即退出"。
            self.q.put(("prog", 95, "正在创建数据表…"))
            schema_ok, schema_msg = init_schema(dst, cfg)
            if not schema_ok:
                self.q.put(("err", "数据表创建失败", schema_msg))
                return
            self._schema_msg = schema_msg

            self.q.put(("prog", 97, "正在创建桌面快捷方式…"))
            make_shortcut(dst)

            self.q.put(("done", [
                ("安装目录", str(dst)),
                ("数据库", f"{cfg['db_user']}@{cfg['db_host']}:{cfg['db_port']}"
                          f"/{cfg['db_name']}"),
                ("数据表", schema_msg),
                ("登录账号", "admin"),
                ("访问地址", f"http://127.0.0.1:{BASE_PORT}"),
            ]))
        except Exception as exc:  # noqa: BLE001
            self.q.put(("err", "安装过程中出错", f"{type(exc).__name__}: {exc}"))

    def _launch(self):
        """启动桌面控制台（不是直接开浏览器）。"""
        exe = self.dst / "xianyu_console.exe"
        if exe.is_file():
            try:
                subprocess.Popen([str(exe)], cwd=str(self.dst),
                                 creationflags=CREATE_NO_WINDOW, close_fds=True)
                time.sleep(0.6)
                self.root.destroy()
                return
            except Exception as exc:  # noqa: BLE001
                messagebox.showwarning("桌面控制台启动失败",
                                       f"{exc}\n\n改为直接打开网页。")
        # 兜底：exe 缺失时起 PHP 再开浏览器
        try:
            port = pick_port()
            self.php_proc = start_php(self.dst, port)
            import webbrowser
            webbrowser.open(f"http://127.0.0.1:{port}/index.php")
            time.sleep(0.8)
            self.root.destroy()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("启动失败",
                                 f"{exc}\n\n可以稍后双击桌面快捷方式启动。")

    def run(self) -> int:
        self.root.mainloop()
        return 0


def init_schema(dst, cfg):
    """用 frontend/init.sql 建表（幂等：脚本内全是 CREATE TABLE IF NOT EXISTS）。

    为什么要在**安装阶段**就建表：
    原来建表完全依赖引擎首次启动时自己跑 init.sql，一旦那条链路有问题
    （比如脚本路径算错、注释切分把语句吞掉），用户看到的就是
    "装完打不开、引擎一启动就崩"，且错误埋在日志里。
    装的时候就建好并校验，问题能立刻暴露在向导界面上。

    Args:
        dst: 安装目录（含 frontend/init.sql）
        cfg: 数据库连接配置字典（db_host/db_port/db_user/db_pass/db_name）

    Returns:
        (是否成功, 说明文字)
    """
    import re as _re

    sql_file = dst / "frontend" / "init.sql"
    if not sql_file.is_file():
        return False, "找不到建表脚本：%s" % sql_file
    try:
        sql = sql_file.read_text(encoding="utf-8")
        # 先剥 `--` 行注释再按 ';' 切分，否则"注释 + 建表"整段会被当注释丢掉
        sql = _re.sub(r"^\s*--[^\n]*$", "", sql, flags=_re.M)
        stmts = [s.strip() for s in sql.split(";") if s.strip()]

        import pymysql
        conn = pymysql.connect(
            host=cfg["db_host"], port=int(cfg["db_port"]),
            user=cfg["db_user"], password=cfg["db_pass"],
            database=cfg["db_name"], charset="utf8mb4",
            autocommit=True, connect_timeout=6)
        try:
            with conn.cursor() as cur:
                for st in stmts:
                    try:
                        cur.execute(st)
                    except Exception:
                        pass          # 已存在等可忽略
                cur.execute("SELECT COUNT(*) FROM information_schema.tables "
                            "WHERE table_schema = DATABASE()")
                n_tables = int(cur.fetchone()[0])
                cur.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'settings'")
                cols = {row[0] for row in cur.fetchall()}
        finally:
            conn.close()

        need = {"batch_interval_min", "batch_daily_max",
                "scan_enabled", "login_guard_enabled"}
        missing = need - cols
        if missing:
            return False, "建了 %d 张表，但 settings 缺列：%s" % (
                n_tables, ", ".join(sorted(missing)))
        if n_tables < 10:
            return False, "只建出 %d 张表（应 >= 13）" % n_tables
        return True, "已建 %d 张表，settings 列齐全" % n_tables
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)


def main() -> int:
    return SetupApp().run()


if __name__ == "__main__":
    sys.exit(main())
