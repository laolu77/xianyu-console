# -*- coding: utf-8 -*-
"""
闲鱼卖家客服 · 自动回复 / 自动发货控制台 —— 桌面启动器

职责（唯一进程宿主）：
  1. 环境检测：PHP（内置便携运行时）、MySQL、闲鱼卖家客服
  2. 一键开启网站：启动网页 + 引擎，并自动打开浏览器
  3. 一键关闭网站：连带关闭引擎、闲鱼客服，并释放 8082 / 9222 端口
  4. 集成工具：安装依赖 / 查看日志 / 初始化数据库 / 打开网站
     （消息导出已于 2026-10-18 移植到网页「会话/消息」页，经 sw_control
       触发器由本程序在后台执行，进度见网页）
  5. 闲鱼客服缺失时提供下载入口，安装后可自动写入配置
  （引擎/客服也可由网页端按钮通过 sw_control 表下发意图启停）

网站（frontend/）只负责展示与下发控制意图（写入 sw_control 表），
本程序监听并执行真正的进程启停。
"""

import os
import sys
import time
import threading
import subprocess
import json
import webbrowser
from pathlib import Path

try:
    import pymysql
    HAVE_PYMYSQL = True
except Exception:
    HAVE_PYMYSQL = False

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, filedialog

from console_ui import ConsoleUI, C

# ----------------------------------------------------------------------------
# 路径与常量
# ----------------------------------------------------------------------------
if getattr(sys, "frozen", False):
    # 打包后：exe 与 frontend/backend/runtime/data 同级（单文件或单目录同理）
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parent
# 找到同时含 frontend/ 与 backend/ 的目录作为 BASE
if (ROOT / "frontend").is_dir() and (ROOT / "backend").is_dir():
    BASE = ROOT
else:
    for p in [ROOT, ROOT.parent, ROOT.parent.parent]:
        if (p / "frontend").is_dir() and (p / "backend").is_dir():
            BASE = p
            break
    else:
        BASE = ROOT

FRONTEND = BASE / "frontend"
BACKEND = BASE / "backend"
DATA = BASE / "data"
TOOLS = BASE / "tools"
RUNTIME = BASE / "runtime"
LOGS = DATA / "logs"
LOGS.mkdir(parents=True, exist_ok=True)

# 便携运行时（优先内置，否则回退系统）
PHP_EXE = RUNTIME / "php" / "php.exe"
if not PHP_EXE.exists():
    PHP_EXE = Path("php.exe")


def _find_python() -> Path:
    """定位内置 Python 解释器。

    必须覆盖两种目录布局（2026-10-23 实锤踩坑）：
      · **venv**：`runtime/python/Scripts/python.exe`（开发机上就是这种）
      · **独立 Python**：`runtime/python/python.exe`（安装包里打包的是这种）
    原实现只认 venv 布局，找不到就回退到 `sys.executable` —— 而安装包里的
    sys.executable 是 `xianyu_console.exe`（PyInstaller 产物），于是引擎被
    "用自己启动自己"，参数被忽略、进程立刻退出，表现为**引擎永远起不来**，
    且在我的开发机上完全复现不了（因为那边的 venv 布局是对的）。
    """
    for c in (
        RUNTIME / "python" / "Scripts" / "python.exe",   # venv（Windows）
        RUNTIME / "python" / "python.exe",               # 独立 Python（Windows）
        RUNTIME / "python" / "bin" / "python.exe",       # venv（POSIX 布局）
    ):
        if c.is_file():
            return c
    return Path(sys.executable)


PY_EXE = _find_python()
# 回退到自身 exe 时说明运行时缺失，启动引擎前必须拦住（否则等于自己启动自己）
PY_RUNTIME_OK = PY_EXE.name.lower() in ("python.exe", "python", "pythonw.exe")

WEB_PORT = 8082
WEB_HOST = "127.0.0.1"
WEB_URL = f"http://{WEB_HOST}:{WEB_PORT}/"
CDP_PORT = 9222

# 所有子进程一律无窗口（CREATE_NO_WINDOW：控制台存在但隐藏，
# 不会被 python / node 驱动 AllocConsole 弹出黑窗）
NO_WINDOW = 0x08000000

def _load_db_cfg() -> dict:
    """读取安装时写入的数据库配置（data/db_config.json）。

    为什么不能写死 127.0.0.1：用户可能把库装在服务器上（安装向导里填的远程库），
    写死本地地址会导致面板一直显示「MySQL 未连接」、引擎状态也读错库 ——
    而网页那边读同一份配置是正常的，两边表现不一致（2026-10-23 用户实测踩到）。
    """
    defaults = dict(host="127.0.0.1", port=3306, user="root", password="",
                    database="xianyu_autoreply", connect_timeout=3)
    try:
        f = DATA / "db_config.json"
        if not f.is_file():
            return defaults
        raw = json.loads(f.read_text(encoding="utf-8"))
        mode = "remote" if raw.get("mode") == "remote" else "local"
        ch = raw.get(mode) or raw.get("local") or {}
        host = ch.get("host") or "127.0.0.1"
        # 远程库连不上时也要能回退，交给 pymysql 报错即可
        return dict(
            host=host,
            port=int(ch.get("port") or 3306),
            user=ch.get("user") or "root",
            # 配置文件里 PHP 侧写 pass、Python 侧写 password，两种都认
            password=ch.get("pass") if ch.get("pass") is not None
            else (ch.get("password") or ""),
            database=ch.get("db") or ch.get("database") or "xianyu_autoreply",
            connect_timeout=3,
        )
    except Exception:  # noqa: BLE001
        return defaults


DB_CFG = _load_db_cfg()
CLIENT_NAME = "闲鱼卖家客服.exe"
# 闲鱼官方站点（原值误写成千牛 qianniu.taobao.com，用户点了不知道跳到哪）
CLIENT_DOWNLOAD_URL = "https://www.goofish.com/"


APP_WINDOW_TITLE = "闲鱼卖家客服 · 控制台启动器"
SINGLE_INSTANCE_MUTEX_NAME = "XianyuSellerConsole_SingleInstance_Mutex"
SINGLE_INSTANCE_HOST = "127.0.0.1"
SINGLE_INSTANCE_PORT = 47911  # 单实例判定专用 loopback 端口（实测未被占用）
_single_instance_socket = None
_single_instance_mutex = None

# ----------------------------------------------------------------------------
# 单实例判定（防止重复启动多个软件，互相抢占端口 / 浏览器控制）
# ----------------------------------------------------------------------------
def _activate_existing_window():
    """把已在运行的软件窗口带到前台（还原最小化 + 前置）。返回是否找到窗口。"""
    try:
        import ctypes
        user32 = ctypes.WinDLL("user32")
        user32.FindWindowW.restype = ctypes.c_void_p
        user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
        hwnd = user32.FindWindowW(None, APP_WINDOW_TITLE)
        if hwnd:
            SW_RESTORE = 9
            user32.ShowWindow(ctypes.c_void_p(hwnd), SW_RESTORE)
            user32.SetForegroundWindow(ctypes.c_void_p(hwnd))
            return True
        return False
    except Exception:
        return False


def _show_single_instance_message():
    """弹窗提示软件已打开。"""
    try:
        import ctypes
        user32 = ctypes.WinDLL("user32")
        user32.MessageBoxW.restype = ctypes.c_int
        user32.MessageBoxW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                       ctypes.c_wchar_p, ctypes.c_uint]
        user32.MessageBoxW(
            None,
            "软件已经打开了，已为你切换到正在运行的窗口。\n\n"
            "请勿重复启动，避免多个实例互相抢占端口与浏览器控制。",
            APP_WINDOW_TITLE,
            0x40,  # MB_ICONINFORMATION
        )
    except Exception:
        pass


def _create_instance_mutex():
    """创建并持有命名互斥体（进程退出时系统自动释放）。

    用途：互斥体在 main() 里、窗口创建之前就存在，可覆盖
    "第一个实例刚启动、窗口还没建出来"的竞态窗口。
    只看创建动作的返回值持有，不依赖错误码（ctypes 读 Win32
    错误码不可靠，实测踩坑：GetLastError 会被前后调用污染）。
    """
    global _single_instance_mutex
    try:
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool,
                                          ctypes.c_wchar_p]
        _single_instance_mutex = kernel32.CreateMutexW(
            None, False, SINGLE_INSTANCE_MUTEX_NAME)
    except Exception:
        pass


def _mutex_exists():
    """探测命名互斥体是否存在（只看返回值，不读错误码）。"""
    try:
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.OpenMutexW.restype = ctypes.c_void_p
        kernel32.OpenMutexW.argtypes = [ctypes.c_uint, ctypes.c_bool,
                                        ctypes.c_wchar_p]
        SYNCHRONIZE = 0x00100000
        h = kernel32.OpenMutexW(SYNCHRONIZE, False, SINGLE_INSTANCE_MUTEX_NAME)
        if h:
            kernel32.CloseHandle(ctypes.c_void_p(h))
            return True
        return False
    except Exception:
        return False


def ensure_single_instance():
    """单实例守卫。

    判定方式（按可靠性排序）：
      1. bind 127.0.0.1:47911 —— 内核级原子操作，第一个实例绑定成功并持有，
         第二个实例必然绑定失败；只看返回值，不会误判；
      2. 绑定失败时再确认是否真的是本软件的另一实例：找到已有窗口，
         或探测到本软件的命名互斥体（覆盖启动竞态）；
      3. 两者都没有 → 端口被其他软件占用，放行启动，避免误拦。

    - 确认是重复启动：弹窗提示"软件已打开"、把已有窗口带到前台，
      返回 False，调用方直接退出，不再打开第二个窗口。
    """
    global _single_instance_socket
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            # 不设 SO_REUSEADDR：Windows 下两个都设了的 socket 会同时 bind
            # 成功导致单实例失效；默认独占绑定才保证只有一个进程抢到。
            s.bind((SINGLE_INSTANCE_HOST, SINGLE_INSTANCE_PORT))
            s.listen(1)
            _single_instance_socket = s  # 进程存活期间持有，退出时系统回收
            _create_instance_mutex()
            return True
        except OSError:
            try:
                s.close()
            except Exception:
                pass
            if _activate_existing_window() or _mutex_exists():
                _show_single_instance_message()
                return False
            # 端口被其他软件占用但本软件并未运行：放行，避免误拦
            return True
    except Exception:
        # 非 Windows / socket 不可用时放行，不影响正常启动
        return True

# ----------------------------------------------------------------------------
# 日志（输出到 ConsoleUI 的彩色终端）
# ----------------------------------------------------------------------------
# 旧内部级别 → 终端语义色级别
_LEVEL_MAP = {"RUN": "DEBUG", "INFO": "INFO", "WARN": "WARN", "ERROR": "ERROR"}


class Logger:
    """把各处的日志送进 ConsoleUI 终端（线程安全：统一 after 回主线程）。"""

    def __init__(self):
        self.ui = None
        self._lock = threading.Lock()

    def bind(self, ui):
        self.ui = ui

    def log(self, msg, level="INFO"):
        ts = time.strftime("%H:%M:%S")
        lvl = _LEVEL_MAP.get(level, "INFO")
        ui = self.ui
        if ui is None:
            return

        def _append():
            try:
                ui.append_log(ts, lvl, msg)
            except Exception:
                pass
        try:
            ui.after(0, _append)
        except Exception:
            pass


# ----------------------------------------------------------------------------
# 进程管理
# ----------------------------------------------------------------------------
def _current_db_desc() -> str:
    """读当前数据库配置，用于在报错里显示"现在连的是哪儿"。"""
    try:
        import json as _json
        d = _json.loads((DATA / "db_config.json").read_text(encoding="utf-8"))
        mode = d.get("mode", "local")
        c = d.get(mode) or d.get("local") or {}
        return (f"{c.get('host', '?')}:{c.get('port', '?')} / "
                f"库 {c.get('db', '?')} / 用户 {c.get('user', '?')}")
    except Exception:  # noqa: BLE001
        return "（读不到 data/db_config.json）"


def explain_engine_failure(text: str) -> str:
    """把引擎崩溃日志翻译成「能照着做」的中文提示。

    为什么需要它：原始输出是整段 Python traceback，全是
    `site-packages/pymysql/connections.py` 这类路径，用户看到只会一脸懵
    （2026-11-03 用户反馈"这个报错优化一下"）。
    这里按 MySQL 错误码 / 常见异常归类，直接给排查步骤。

    Args:
        text: 日志尾部文本（只做关键词匹配，不要求格式）

    Returns:
        多行中文提示；无法识别时返回空串，由调用方回退到原始日志。
    """
    t = text or ""
    low = t.lower()

    if ("2003" in t or "10061" in t or "connection refused" in low
            or "can't connect to mysql" in low):
        return ("MySQL 服务没有启动（或端口不对）\n"
                f"       当前配置：{_current_db_desc()}\n"
                "       解决：① 打开 phpStudy / 服务管理器，把 MySQL 启动起来\n"
                "             ② 或管理员命令行执行：net start mysql\n"
                "             ③ 确认端口还是 3306（改过就在网页「设置 → 数据库连接」改回来）")
    if "1045" in t or "access denied" in low:
        return ("MySQL 账号或密码不正确\n"
                f"       当前配置：{_current_db_desc()}\n"
                "       解决：网页「设置 → 数据库连接」里改成正确的账号密码")
    if "1049" in t or "unknown database" in low:
        return ("数据库不存在\n"
                f"       当前配置：{_current_db_desc()}\n"
                "       解决：重新运行安装程序（会自动建库并建表）")
    if "1146" in t or "doesn't exist" in low:
        return ("数据库里缺表\n"
                "       解决：重启一次引擎会自动补建；仍失败请重新运行安装程序")
    if "1054" in t or "unknown column" in low:
        return ("数据表缺少字段\n"
                "       解决：重启一次引擎会自动补列；仍失败请重新安装")
    if "modulenotfounderror" in low or "no module named" in low:
        return ("Python 运行时缺少依赖库\n"
                "       解决：安装包不完整，请重新安装（不要只拷部分文件）")
    if "9222" in t or "econnrefused" in low:
        return ("连不上闲鱼客户端的调试端口 9222\n"
                "       解决：① 控制台点「启动客户端」，等它完全登录；\n"
                "             ② 再点启动引擎；\n"
                "             ③ 客户端已在运行的话，用控制台重启它（必须带调试端口）")
    return ""


class Controller:
    def __init__(self, logger):
        self.log = logger
        self.web_proc = None
        self.engine_proc = None
        self.client_proc = None
        self._client_log_f = None
        self._lock = threading.Lock()
        self.stop_evt = threading.Event()
        # 启动冷却时间戳：见 monitor_loop 里的注释（防止「检测不到就重启」的风暴）
        self._last_engine_start = 0.0
        self._last_client_start = 0.0
        # 客户端连续启动失败次数：超过阈值就停止自动重试，
        # 否则会陷入"杀进程→重启→仍不通→再杀"的循环（2026-10-29）
        self._client_fail_count = 0

    # ---- 网页 ----
    def start_web(self):
        with self._lock:
            if self.web_proc and self.web_proc.poll() is None:
                return
            try:
                f = open(LOGS / "web.log", "ab", buffering=0)
                self.web_proc = subprocess.Popen(
                    [str(PHP_EXE), "-S", f"{WEB_HOST}:{WEB_PORT}", "-t", str(FRONTEND)],
                    cwd=str(FRONTEND), stdout=f, stderr=f,
                    creationflags=NO_WINDOW)
                self.log.log(f"网页已启动：{WEB_URL}（PHP {PHP_EXE}）")
            except Exception as e:
                self.log.log(f"网页启动失败：{e}", "ERROR")

    def stop_web(self):
        with self._lock:
            if self.web_proc and self.web_proc.poll() is None:
                try:
                    self.web_proc.terminate()
                except Exception:
                    pass
                self.web_proc = None
        # 兜底释放端口：只杀 LISTEN 该端口的 php.exe，绝不碰浏览器连接
        self._free_port(WEB_PORT, only_exes=["php.exe"])
        self.log.log("网页已关闭，端口 8082 已释放")

    def is_web_up(self):
        try:
            import socket
            s = socket.create_connection((WEB_HOST, WEB_PORT), timeout=1.0)
            s.close()
            return True
        except Exception:
            return False

    # ---- 引擎 ----
    def start_engine(self):
        if not PY_RUNTIME_OK:
            self.log.log(
                f"找不到内置 Python 运行时：{PY_EXE}（应存在 runtime/python/python.exe "
                f"或 runtime/python/Scripts/python.exe）。安装包可能不完整，"
                f"请重新安装", "ERROR")
            return
        with self._lock:
            if self.engine_proc and self.engine_proc.poll() is None:
                return
            try:
                f = open(LOGS / "engine.log", "ab", buffering=0)
                env = os.environ.copy()
                env["PYTHONPATH"] = str(BACKEND)
                env["PYTHONIOENCODING"] = "utf-8"
                env["PYTHONUTF8"] = "1"
                self.engine_proc = subprocess.Popen(
                    [str(PY_EXE), "web_engine.py"],
                    cwd=str(BACKEND), env=env, stdout=f, stderr=f,
                    creationflags=NO_WINDOW)
                pid = self.engine_proc.pid
            except Exception as e:
                self.log.log(f"引擎启动失败：{e}", "ERROR")
                return
        self.log.log(f"自动回复引擎已拉起（PID {pid}）")

        # 【等心跳再返回】引擎起来后要先连客户端 CDP、再写 engine_status 心跳。
        # 不等的话监听循环会判定"没起来"又拉一个，日志刷屏（2026-10-23 用户实测）。
        for _ in range(40):              # 最多等 20 秒
            if self.is_engine_up():
                self.log.log("引擎已就绪")
                return
            if self.engine_proc and self.engine_proc.poll() is not None:
                # 把引擎日志尾部打出来：否则用户只看到"引擎未启动"，无从判断
                try:
                    tail = (LOGS / "engine.log").read_text(
                        encoding="utf-8", errors="replace").strip().splitlines()[-6:]
                    detail = " / ".join(x.strip() for x in tail if x.strip())
                except Exception:  # noqa: BLE001
                    detail = ""
                # 先尝试把错误"翻译"成能照着做的中文；识别不了才回退原始日志
                hint = explain_engine_failure(detail)
                if hint:
                    for i, line in enumerate(hint.splitlines()):
                        self.log.log(line, "ERROR" if i == 0 else "INFO")
                    self.log.log("完整日志：data/logs/engine.log", "INFO")
                else:
                    self.log.log("引擎启动后立即退出："
                                 + (detail[-300:] if detail
                                    else "详情见 data/logs/engine.log"),
                                 "ERROR")
                return
            time.sleep(0.5)
        self.log.log("引擎已启动，但心跳尚未上报（可能在等客户端连上 9222）",
                     "WARN")

    def stop_engine(self):
        with self._lock:
            if self.engine_proc and self.engine_proc.poll() is None:
                try:
                    self.engine_proc.terminate()
                except Exception:
                    pass
                self.engine_proc = None
        # 同时让引擎自己优雅退出
        self._db_cmd("stop")
        self.log.log("引擎已停止")

    def is_engine_up(self):
        st = self.engine_status()
        if not st:
            return False
        if st.get("state") != "running" or not st.get("last_heartbeat"):
            return False
        try:
            import datetime
            ts = datetime.datetime.strptime(str(st["last_heartbeat"]), "%Y-%m-%d %H:%M:%S")
            # 阈值 45s：客户端未上线时引擎每轮 connect() 要轮询等待约 30s，
            # 重试周期 ≈36s；20s 阈值会让卡片在「未启动/运行中」之间闪烁。
            # 引擎真死由看门狗（stale>150s 杀重启）兜底，显示层放宽无风险。
            return (datetime.datetime.now() - ts).total_seconds() < 45
        except Exception:
            return False

    # ---- 闲鱼客户端 ----
    def find_client_exe(self):
        """找闲鱼卖家客户端 exe。

        顺序：① 安装时填的路径（data/client_path.txt）→ ② 常见安装位置。
        绝不能写死某个用户名下的路径（原来写着 <用户名>/...，
        在别人机器上永远找不到，且没读安装向导里填的路径）。
        """
        dirs = []
        try:
            saved = (DATA / "client_path.txt")
            if saved.is_file():
                p = saved.read_text(encoding="utf-8").strip()
                if p:
                    dirs.append(Path(p))
        except Exception:  # noqa: BLE001
            pass

        local = os.environ.get("LOCALAPPDATA")
        if local:
            dirs.append(Path(local) / "Programs" / "xianyu-seller-im")
        for drive in ("C:/", "D:/", "E:/", "F:/"):
            dirs.append(Path(drive) / "Programs" / "xianyu-seller-im")
            dirs.append(Path(drive) / "xianyu-seller-im")

        for d in dirs:
            try:
                if not d.is_dir():
                    continue
                # 先按固定文件名找，找不到就找名字含「闲鱼/客服」的 exe
                cand = d / CLIENT_NAME
                if cand.is_file():
                    return cand
                for f in d.glob("*.exe"):
                    if f.name == CLIENT_NAME or ("闲鱼" in f.name and "客服" in f.name):
                        return f
            except Exception:  # noqa: BLE001
                continue
        return None

    def set_client_dir(self, path: str) -> bool:
        """手动指定客户端目录：校验后写进 data/client_path.txt。"""
        try:
            p = Path(path)
            if not p.is_dir():
                return False
            exe = None
            if (p / CLIENT_NAME).is_file():
                exe = p / CLIENT_NAME
            else:
                for f in p.glob("*.exe"):
                    if "闲鱼" in f.name or "客服" in f.name:
                        exe = f
                        break
            if exe is None:
                return False
            (DATA / "client_path.txt").write_text(
                str(p).replace("\\", "/"), encoding="utf-8")
            return True
        except Exception:  # noqa: BLE001
            return False

    def _kill_existing_clients(self) -> int:
        """结束所有已在运行的闲鱼客户端进程，返回结束的数量。

        【为什么必须杀】Electron 客户端是**单实例**应用：如果已经有一个实例在跑
        （典型是用户自己双击打开的，**没带** --remote-debugging-port），我们再
        启动带端口的实例时，新进程会把启动参数转交给旧实例、然后自己退出。
        结果就是：9222 永远没人监听 → is_client_up() 一直 False →
        控制台认为"未运行"并反复重启，而任务管理器里客户端明明开着。
        （2026-10-29 用户实测："显示已启动，下一秒又变未运行，日志一直重试"）

        原代码的写法正好相反：`if self.is_client_up(): self.stop_client()` ——
        只有端口通时才去重启（把好实例杀掉），端口不通时反而不清理。
        """
        killed = 0
        try:
            import psutil
            for p in psutil.process_iter(["pid", "name"]):
                name = (p.info.get("name") or "")
                if "闲鱼" in name and "客服" in name:
                    try:
                        p.kill()
                        killed += 1
                    except Exception:  # noqa: BLE001
                        pass
        except Exception:  # noqa: BLE001
            # psutil 不可用时退回 taskkill（中文进程名可能有编码坑，尽力而为）
            try:
                subprocess.call(["taskkill", "/F", "/IM", "闲鱼卖家客服.exe"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=NO_WINDOW)
                killed = 1
            except Exception:  # noqa: BLE001
                pass
        # 等进程真正退出、单实例锁释放，否则新实例仍会被旧锁挡掉
        for _ in range(10):
            time.sleep(0.5)
            try:
                import psutil
                still = [p for p in psutil.process_iter(["name"])
                         if "闲鱼" in (p.info.get("name") or "")
                         and "客服" in (p.info.get("name") or "")]
                if not still:
                    break
            except Exception:  # noqa: BLE001
                break
        return killed

    def start_client(self):
        exe = self.find_client_exe()
        if not exe:
            self.log.log("未找到闲鱼卖家客服，无法启动", "ERROR")
            return False
        # ① 端口已通 = 有个带调试端口的实例在正常工作 → 直接复用，不要重启
        if self.is_client_up():
            self.log.log(f"客户端已在运行且 {CDP_PORT} 可连接，无需重启")
            return True
        # ② 端口不通 → 可能有个"没带调试端口"的实例占着单实例锁，必须先清掉
        killed = self._kill_existing_clients()
        if killed:
            self.log.log(f"已结束 {killed} 个旧的客户端进程"
                         f"（避免单实例抢占调试端口），重新带端口启动")
        else:
            time.sleep(1)
        try:
            # 启动配方必须与 backend/launch_app.py（前台驻留版，经过实战）
            # 和 backend/export_messages.py 保持一致，缺一不可（2026-10-18 实测：
            # 只传 --remote-debugging-port 时客户端能起来但调试端口始终不开放、
            # 约一分半后进程消失，网页「启动软件」按钮形同虚设）：
            #   1. --no-sandbox --disable-gpu：本环境 Chromium 沙箱/GPU 会崩；
            #   2. clean_env：ELECTRON_RUN_AS_NODE（哪怕空串）会让 Electron 主进程
            #      退化成 Node，拒绝全部 Chromium 参数直接报 bad option 退出；
            #   3. cwd=exe 父目录：相对路径资源定位；
            #   4. stdout 落盘 client_boot.log：客户端没有任何窗口输出，排错全靠它。
            # 句柄必须由 self 持有，被 GC 关掉会把客户端连带收走。
            env = {k: v for k, v in os.environ.items()
                   if k not in ("ELECTRON_RUN_AS_NODE", "NODE_OPTIONS",
                                "PYTHONUTF8", "PYTHONIOENCODING")}
            LOGS.mkdir(parents=True, exist_ok=True)
            self._client_log_f = open(LOGS / "client_boot.log", "ab",
                                      buffering=0)
            self.client_proc = subprocess.Popen(
                [str(exe), "--no-sandbox", "--disable-gpu",
                 f"--remote-debugging-port={CDP_PORT}"],
                stdout=self._client_log_f, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=env, cwd=str(exe.parent), close_fds=True,
                creationflags=NO_WINDOW)
            self.log.log(f"闲鱼卖家客服已启动（PID {self.client_proc.pid}），"
                         f"监听 {CDP_PORT}，启动日志见 data/logs/client_boot.log")
            self._write_app_path(exe)
            # 【等就绪再返回】Electron 冷启动要 10-20 秒，9222 才会监听。
            # 不等的话，监听循环下一轮（3 秒后）会判定"客户端没起来"再启一个，
            # 表现就是客户端窗口反复弹出、端口互相抢占（2026-10-23 用户实测风暴）。
            for _ in range(60):          # 最多等 30 秒
                if self.is_client_up():
                    self.log.log(f"客户端调试端口 {CDP_PORT} 已就绪")
                    # 不再自动打开商品页：闲鱼客户端是封闭的单窗口 IM 应用，
                    # window.open 被它转交给系统浏览器（打开的是未登录的买家视角），
                    # CDP Target.createTarget 也被拒绝。用户 2026-11-03 明确要求关掉。
                    return True
                if self.client_proc.poll() is not None:
                    self.log.log("客户端启动后立即退出，详情见 data/logs/client_boot.log",
                                 "ERROR")
                    return False
                time.sleep(0.5)
            # 进程还在但端口没起（可能停在登录页/被安全软件拦）：
            # 返回 True 避免被反复重启，让用户自己去处理
            self.log.log(f"客户端已运行，但 {CDP_PORT} 端口 30 秒内未监听；"
                         f"如果停在登录页，请先登录再点「重新检测」", "WARN")
            return True
        except Exception as e:
            self.log.log(f"客户端启动失败：{e}", "ERROR")
            return False

    def stop_client(self):
        # 先终止我们跟踪的客户端进程（精准，不会误伤其他程序）
        with self._lock:
            if self.client_proc and self.client_proc.poll() is None:
                try:
                    self.client_proc.terminate()
                except Exception:
                    pass
                self.client_proc = None
            # 日志句柄一并关掉：留着会占住 client_boot.log 不让轮转
            if self._client_log_f is not None:
                try:
                    self._client_log_f.close()
                except Exception:
                    pass
                self._client_log_f = None
        # 兜底：只杀 LISTEN 9222 的闲鱼客户端 exe，绝不杀浏览器等其它进程
        self._free_port(CDP_PORT, only_exes=[CLIENT_NAME])
        self.log.log("闲鱼卖家客服已关闭")

    def is_client_up(self):
        try:
            import socket
            s = socket.create_connection((WEB_HOST, CDP_PORT), timeout=1.0)
            s.close()
            return True
        except Exception:
            return False

    # ---- 采集（一次性） ----
    def run_collect(self):
        script = TOOLS / "collect_shop_products.py"
        if not script.exists():
            self.log.log(f"采集脚本缺失：{script}", "ERROR")
            return
        try:
            f = open(LOGS / "collect.log", "ab", buffering=0)
            subprocess.Popen(
                [str(PY_EXE), str(script)],
                cwd=str(BASE), env={**os.environ, "PYTHONPATH": str(BACKEND)},
                stdout=f, stderr=f, creationflags=NO_WINDOW)
            self.log.log("店铺商品采集已启动（后台运行，进度见网页）")
        except Exception as e:
            self.log.log(f"采集启动失败：{e}", "ERROR")

    # ---- 工具：安装依赖 ----
    def install_deps(self, on_line):
        req = BACKEND / "requirements.txt"
        cmds = [[str(PY_EXE), "-m", "pip", "install", "-r", str(req)],
                [str(PY_EXE), "-m", "playwright", "install", "chromium"]]
        threading.Thread(target=self._stream, args=(cmds, on_line), daemon=True).start()

    # ---- 工具：初始化数据库 ----
    def init_db(self, on_line):
        cli = FRONTEND / "init_db_cli.php"
        if not cli.exists():
            on_line("init_db_cli.php 缺失")
            return
        threading.Thread(target=self._stream,
                        args=([[str(PHP_EXE), str(cli)]], on_line), daemon=True).start()

    # ---- 导出消息（网页触发，一次性；旧桌面按钮已改为「打开网站」） ----
    def run_export(self, trigger: str = ""):
        """网页端「导出消息」的后台执行：拉起 export_runner.py。

        与旧桌面裸跑 export_messages.py 的差异由包装器负责：进度写
        data/export_progress.json 供网页轮询；传 --keep 导出后保留客户端
        运行（引擎几秒内自动重连，不必手动重启软件）。trigger 为 sw_control
        触发器时间戳，透传给包装器写进进度 JSON 供 pending 检测。
        """
        script = TOOLS / "export_runner.py"
        if not script.exists():
            self.log.log(f"导出脚本缺失：{script}", "ERROR")
            return
        try:
            LOGS.mkdir(parents=True, exist_ok=True)
            f = open(LOGS / "export.log", "ab", buffering=0)
            argv = [str(PY_EXE), str(script)]
            if trigger:
                argv.append(trigger)
            subprocess.Popen(
                argv,
                cwd=str(BASE), env={**os.environ, "PYTHONPATH": str(BACKEND)},
                stdout=f, stderr=f, creationflags=NO_WINDOW)
            self.log.log("消息导出已启动（后台运行，进度见网页「会话/消息」页）")
        except Exception as e:
            self.log.log(f"导出启动失败：{e}", "ERROR")

    def _stream(self, cmds, on_line):
        for cmd in cmds:
            on_line("> " + " ".join(cmd))
            try:
                p = subprocess.Popen(cmd, cwd=str(BASE),
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding="utf-8", errors="replace",
                                     creationflags=NO_WINDOW)
                for bl in p.stdout:
                    on_line(bl.rstrip("\n"))
                p.wait()
                on_line(f"（退出码 {p.returncode}）")
            except Exception as e:
                on_line(f"执行异常：{e}")

    # ---- 底层辅助 ----
    def _pid_image(self, pid):
        """返回进程镜像名（如 php.exe / 闲鱼卖家客服.exe），拿不到返回 None。
        仅用 tasklist，不依赖 wmic。"""
        try:
            out = subprocess.check_output(
                f'tasklist /fi "PID eq {pid}" /fo csv /nh', shell=True,
                stderr=subprocess.DEVNULL, text=True,
                encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
            row = out.strip().split("\r\n")[0].strip()
            if not row or row.startswith('"",'):
                return None
            return row.split('","')[0].strip('"')
        except Exception:
            return None

    def _free_port(self, port, only_exes=None):
        """释放端口：只杀「正在 LISTEN 该端口」且镜像名在白名单内的进程。
        绝不杀 ESTABLISHED 的连接方（如浏览器访问网站的连接），避免误杀浏览器。"""
        try:
            out = subprocess.check_output(
                f'netstat -ano | findstr /R ":{port} " | findstr "LISTENING"',
                shell=True, stderr=subprocess.DEVNULL, text=True,
                encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
            pids = set()
            for line in out.splitlines():
                parts = line.split()
                if parts:
                    pids.add(parts[-1])
            for pid in pids:
                img = self._pid_image(pid)
                if only_exes and img and img.lower() not in [e.lower() for e in only_exes]:
                    self.log.log(f"端口 {port} 被 {img}(PID {pid}) 占用，非本软件进程，已跳过", "WARN")
                    continue
                try:
                    subprocess.call(f"taskkill /F /PID {pid}", shell=True,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    creationflags=NO_WINDOW)
                except Exception:
                    pass
        except Exception:
            pass

    def _db(self):
        if not HAVE_PYMYSQL:
            return None
        try:
            return pymysql.connect(**DB_CFG)
        except Exception:
            return None

    def _db_cmd(self, cmd):
        c = self._db()
        if not c:
            return
        try:
            with c.cursor() as cur:
                cur.execute("INSERT INTO commands (cmd) VALUES (%s)", (cmd,))
            c.commit()
        except Exception:
            pass
        finally:
            c.close()

    def engine_status(self):
        c = self._db()
        if not c:
            return None
        try:
            with c.cursor(pymysql.cursors.DictCursor) as cur:
                cur.execute("SELECT * FROM engine_status WHERE id=1")
                return cur.fetchone()
        except Exception:
            return None
        finally:
            c.close()

    def get_control(self, key, default=""):
        self._ensure_control()
        c = self._db()
        if not c:
            return default
        try:
            with c.cursor() as cur:
                cur.execute("SELECT val FROM sw_control WHERE `key`=%s", (key,))
                r = cur.fetchone()
                return r[0] if r else default
        except Exception:
            return default
        finally:
            c.close()

    def set_control(self, key, val):
        self._ensure_control()
        c = self._db()
        if not c:
            return
        try:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO sw_control (`key`,`val`) VALUES (%s,%s) "
                    "ON DUPLICATE KEY UPDATE val=%s", (key, val, val))
            c.commit()
        except Exception:
            pass
        finally:
            c.close()

    def _ensure_control(self):
        c = self._db()
        if not c:
            return
        try:
            with c.cursor() as cur:
                cur.execute(
                    "CREATE TABLE IF NOT EXISTS sw_control ("
                    "`key` VARCHAR(32) NOT NULL PRIMARY KEY, "
                    "`val` VARCHAR(64) NOT NULL DEFAULT '', "
                    "updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP "
                    "ON UPDATE CURRENT_TIMESTAMP) ENGINE=InnoDB DEFAULT CHARSET=utf8")
        except Exception:
            pass
        finally:
            c.close()

    def _write_app_path(self, exe: Path):
        cfg = BACKEND / "config.yaml"
        if not cfg.exists():
            return
        try:
            txt = cfg.read_text(encoding="utf-8")
            import re
            new = re.sub(r'app_path:\s*".*?"', f'app_path: "{exe}"', txt)
            cfg.write_text(new, encoding="utf-8")
        except Exception:
            pass

    # ---- 监听循环：把网站下发的控制意图变成真实进程 ----
    def monitor_loop(self, ui):
        last_collect = "0"
        last_export = "0"
        while not self.stop_evt.is_set():
            try:
                # 引擎
                want_engine = self.get_control("engine", "")
                if want_engine == "run":
                    if not self.is_engine_up():
                        # 【必须冷却】引擎启动后要连客户端 CDP，没连上会立刻退出；
                        # 若每轮都重启，就会变成「启动→崩溃→再启动」的刷屏风暴
                        # （2026-10-23 用户实测：日志每秒一条、客户端狂弹窗）。
                        if time.time() - self._last_engine_start > 25:
                            self._last_engine_start = time.time()
                            self.start_engine()
                elif want_engine == "stop":
                    if self.engine_proc and self.engine_proc.poll() is None:
                        self.stop_engine()
                # 客户端
                want_client = self.get_control("client", "")
                if want_client == "run":
                    if self.is_client_up():
                        self._client_fail_count = 0      # 通了就清零
                    else:
                        # 客户端冷启动要 10-20 秒，期间 9222 还没监听。
                        # 冷却时间必须比它长，否则会一边启动一边被判定"没起来"再启一个，
                        # 表现就是闲鱼客户端反复弹窗、端口互相抢占。
                        if time.time() - self._last_client_start > 45:
                            self._client_fail_count += 1
                            if self._client_fail_count <= 3:
                                self._last_client_start = time.time()
                                self.start_client()
                            elif self._client_fail_count == 4:
                                # 连续失败就停手：再自动重试只会反复杀掉用户正在
                                # 使用的客户端窗口，且永远好不了，必须让人介入。
                                self.log.log(
                                    "客户端连续 4 次启动后调试端口仍未开放，"
                                    "已停止自动重试。请检查：① 是否停在扫码登录页；"
                                    "② 是否被安全软件拦截；"
                                    "③ 手动打开客户端后再点「重新检测」", "ERROR")
                elif want_client == "stop":
                    if self.is_client_up():
                        self.stop_client()
                    self._client_fail_count = 0
                # 采集（一次性）
                want_collect = self.get_control("collect", "0")
                if want_collect not in ("", "0") and want_collect != last_collect:
                    last_collect = want_collect
                    self.run_collect()
                    self.set_control("collect", "0")
                # 导出消息（一次性；网页「会话/消息」页触发，进度写
                # data/export_progress.json，由 export_runner.py 上报）
                want_export = self.get_control("export", "0")
                if want_export not in ("", "0") and want_export != last_export:
                    last_export = want_export
                    self.run_export(want_export)
                    self.set_control("export", "0")
                # 刷新 UI 状态
                ui.refresh_status()
            except Exception as e:
                self.log.log(f"监听异常：{e}", "WARN")
            self.stop_evt.wait(3)


# ----------------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------------
class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title(APP_WINDOW_TITLE)
        self.root.geometry("880x640")
        self.root.minsize(720, 520)

        self._busy = False        # 服务启动/关闭过渡中（禁止轮询覆盖按钮形态）
        self._closing = False     # 正在退出（禁止刷新触碰已销毁控件）

        self.logger = Logger()
        # 新 UI：ConsoleUI 组件库（指标卡 / 客户端卡 / 三档按钮 / 彩色日志终端）
        self.ui = ConsoleUI(self.root)
        self.logger.bind(self.ui)
        self.ctrl = Controller(self.logger)

        self._wire_ui()

        # 启动监听线程（把网站下发的控制意图变成真实进程，并刷新状态）
        self.monitor = threading.Thread(target=self.ctrl.monitor_loop, args=(self,),
                                        daemon=True)
        self.monitor.start()

        self.root.after(300, self.do_detect)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------- 界面接线（只绑命令，不改组件内部状态机） ----------
    def _wire_ui(self):
        ui = self.ui
        # 服务开关：启动 / 关闭（含二次确认）由 ConsoleUI 状态机驱动
        ui.on_start = self.on_start
        ui.on_stop = self.on_stop
        # 工具栏次操作
        ui.btns["deps"].command = self.on_install
        ui.btns["log"].command = self.on_view_log
        ui.btns["db"].command = self.on_init_db
        ui.btns["web"].command = self.on_open_web
        # 客户端卡内的两个动作
        ui.btns["install"].command = self.on_download_client
        ui.btns["recheck"].command = self.do_detect
        # MySQL 卡：点击重新检测连接（MySQL 常被外部启停，控制台只会在启动时
        # 检测一次，用户需要能手动刷新，2026-10-19 用户要求）
        ui.set_stat_click("mysql", self.on_check_mysql)

    # ---------- 检测 ----------
    def do_detect(self):
        def work():
            # PHP（便携运行时优先，其次系统 PATH）
            php_ok = PHP_EXE.exists() or self._which("php")
            php_sub = str(PHP_EXE) if PHP_EXE.exists() else "系统 PHP（PATH）"
            self.root.after(0, lambda: self.ui.set_stat(
                "php", "正常" if php_ok else "异常 / 缺失", php_sub,
                "ok" if php_ok else "err"))
            # MySQL（副信息带操作提示：这张卡可以点击重新检测）
            mysql_ok = self.ctrl._db() is not None
            self.root.after(0, lambda: self.ui.set_stat(
                "mysql", "已连接" if mysql_ok else "未连接",
                f"{DB_CFG['host']}:{DB_CFG['port']} · 点击刷新",
                "ok" if mysql_ok else "err"))
            # 闲鱼客服客户端（先置「检测中」，结果走 ClientCard 状态机）
            self.root.after(0, lambda: self.ui.set_client_state("off", "检测中", "—"))
            exe = self.ctrl.find_client_exe()
            if exe:
                self.root.after(0, lambda: self.ui.set_client_state(
                    "ok", "已安装", str(exe)))
            else:
                self.root.after(0, lambda: self.ui.set_client_state(
                    "off", "未安装", "—"))
            self.refresh_status()
        threading.Thread(target=work, daemon=True).start()

    # ---------- MySQL 卡片：点击重新检测 ----------
    def on_check_mysql(self):
        """点击 MySQL 指标卡 → 重新探测数据库连接并刷新卡片。

        为什么需要手动刷新：控制台只在启动时（do_detect）检测一次 MySQL，
        而 MySQL 常被外部工具/服务单独启停，界面就会一直停在旧结论。
        这里每次点击都新建连接实测一次（Controller._db 不缓存连接），
        结果写回卡片并留一条日志。

        不阻塞 UI：真正的 connect 放后台线程（最坏情况要等 connect_timeout）。
        """
        if getattr(self, "_closing", False):
            return
        if getattr(self, "_mysql_checking", False):
            return  # 连点防抖：上一次还没回来
        self._mysql_checking = True
        try:
            self.ui.set_stat_pending("mysql", "检测中…")
        except Exception:
            pass
        self.logger.log("重新检测 MySQL 连接…")

        def work():
            t0 = time.time()
            ok, err = False, ""
            conn = None
            try:
                conn = self.ctrl._db()
                ok = conn is not None
            except Exception as exc:      # noqa: BLE001 - 只为把原因写进日志
                err = str(exc)
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
            ms = int((time.time() - t0) * 1000)
            addr = f"{DB_CFG['host']}:{DB_CFG['port']}"
            sub = f"{addr} · 点击刷新"

            def apply():
                self._mysql_checking = False
                if getattr(self, "_closing", False):
                    return
                self.ui.set_stat("mysql", "已连接" if ok else "未连接",
                                 sub, "ok" if ok else "err")
                if ok:
                    self.logger.log(f"MySQL 已连接（{addr}，耗时 {ms}ms）")
                else:
                    self.logger.log(
                        f"MySQL 未连接（{addr}，耗时 {ms}ms）"
                        f"{('：' + err) if err else '，请确认 MySQL 服务已启动'}",
                        "WARN")
            self.root.after(0, apply)

        threading.Thread(target=work, daemon=True).start()

    def _which(self, name):
        try:
            r = subprocess.run(["where", name], capture_output=True, text=True,
                               shell=True)
            return r.returncode == 0 and r.stdout.strip() != ""
        except Exception:
            return False

    # ---------- 状态刷新 ----------
    def refresh_status(self):
        # 正在关闭时窗口可能被销毁，跳过刷新避免触碰失效控件
        if getattr(self, "_closing", False):
            return
        web = self.ctrl.is_web_up()
        eng = self.ctrl.is_engine_up()
        cli = self.ctrl.is_client_up()
        self.root.after(0, lambda: self._apply_status(web, eng, cli))

    def _engine_pid(self):
        """取引擎 PID：优先心跳表，其次本进程拉起的子进程。"""
        try:
            st = self.ctrl.engine_status() or {}
            if st.get("pid"):
                return st["pid"]
        except Exception:
            pass
        p = getattr(self.ctrl, "engine_proc", None)
        if p is not None and p.poll() is None:
            return p.pid
        return None

    def _apply_status(self, web, eng, cli):
        # 卡片 + 服务开关；开关形态只能由 set_service_state 驱动，
        # 且启动/关闭过渡期（_busy）不被轮询覆盖。
        try:
            # 网站服务卡
            if web:
                self.ui.set_stat("web", "运行中", f"{WEB_HOST}:{WEB_PORT}", "ok")
            else:
                self.ui.set_stat("web", "已停止", f"{WEB_HOST}:{WEB_PORT}", "off")
            # 引擎进程卡
            pid = self._engine_pid()
            if eng:
                sub = f"PID {pid} · CDP {CDP_PORT}" if pid else f"CDP {CDP_PORT}"
                self.ui.set_stat("engine", "运行中", sub, "ok")
            else:
                self.ui.set_stat("engine", "未启动", "未启动", "off")
            # 服务开关（轮询发现进程已死 → 自动拉回绿色「启动服务」）
            if not self._busy:
                self.ui.set_service_state(web)
        except Exception:
            # 窗口已销毁时静默忽略
            pass

    # ---------- 服务开关（合并了原「开启网站 / 关闭网站」） ----------
    def on_start(self):
        """点「启动服务」：起网页 + 引擎，就绪后自动打开浏览器。"""
        self._busy = True
        self.ui.set_service_state(False, busy=True)   # 按钮进入「启动中…」
        self.logger.log("正在启动服务（网页 + 引擎）…")

        def work():
            try:
                self.ctrl.start_web()
                # 引擎随网站一并拉起（自动回复/发货依赖）
                self.ctrl.set_control("engine", "run")
                self.ctrl.start_engine()
                self.logger.log("网站已开启，请等待引擎开启…")
                # 等网站就绪后自动打开浏览器；引擎就绪后提示去网页启动软件
                threading.Thread(target=self._wait_and_open, daemon=True).start()
            except Exception as e:
                self.logger.log(f"启动失败：{e}", "ERROR")
            finally:
                # 给 PHP 一点监听时间，再以真实端口状态定按钮形态
                for _ in range(10):
                    if self.ctrl.is_web_up():
                        break
                    time.sleep(0.3)
                self._busy = False
                self.root.after(0, self.refresh_status)

        threading.Thread(target=work, daemon=True).start()

    def on_stop(self):
        """点「关闭服务」（已二次确认）：全停并释放端口。"""
        self._busy = True
        self.ui.set_service_state(True, busy=True)    # 按钮进入「关闭中…」
        # 收尾（netstat 查端口、MySQL 写控制意图等 I/O）放后台线程，
        # 避免阻塞界面主线程造成「卡顿一下」。
        self.logger.log("正在关闭网站与相关进程…")
        threading.Thread(target=self._do_close_site, daemon=True).start()

    def _do_close_site(self):
        try:
            self.ctrl.set_control("engine", "stop")
            self.ctrl.set_control("client", "stop")
            self.ctrl.stop_engine()
            self.ctrl.stop_client()
            self.ctrl.stop_web()
            self.logger.log("网站、引擎、闲鱼客服均已关闭，端口 8082 / 9222 已释放")
        except Exception as e:
            self.logger.log(f"关闭过程异常：{e}", "WARN")
        finally:
            # 切回主线程刷新 UI（服务开关自动回绿色「启动服务」）
            self._busy = False
            if not getattr(self, "_closing", False):
                self.root.after(0, self.refresh_status)

    def _wait_and_open(self):
        # 等网站起来
        for _ in range(20):
            if self.ctrl.is_web_up():
                break
            time.sleep(0.3)
        if self.ctrl.is_web_up():
            webbrowser.open(WEB_URL)
            self.logger.log(f"已在浏览器打开 {WEB_URL}")
        else:
            self.logger.log("网站未就绪，未能自动打开浏览器", "WARN")
        # 等引擎就绪（最多约 40 秒）
        for _ in range(80):
            if self.ctrl.is_engine_up():
                break
            time.sleep(0.5)
        if self.ctrl.is_engine_up():
            self.logger.log("引擎已开启。请在网页中点击「一键启动软件」启动闲鱼客服。")
        else:
            self.logger.log("引擎尚未就绪，请稍候在网页查看引擎状态。", "WARN")

    def on_download_client(self):
        """「选择位置」：让用户手动指到闲鱼客户端目录。

        为什么改成选目录而不是直接开下载页：用户经常已经装了、只是装在
        非常规路径（D 盘、自定义目录），自动探测找不到，却又不给手动指路的
        机会（2026-10-23 用户反馈）。选中后写入 data/client_path.txt，下次自动认。
        """
        d = filedialog.askdirectory(title="选择「闲鱼卖家客服」安装目录")
        if d:
            if self.ctrl.set_client_dir(d):
                messagebox.showinfo("已设置", f"客户端路径已保存：\n{d}\n\n"
                                              "正在重新检测，请稍候。")
                self.refresh_status()
            else:
                messagebox.showwarning(
                    "没找到客户端",
                    f"在下面这个目录里没找到「闲鱼卖家客服.exe」：\n{d}\n\n"
                    "请选择**包含该 exe 的那一层目录**（不是它的上级）。")
            return
        # 用户取消 → 主动问要不要去官网下载
        if messagebox.askyesno(
                "还没安装？",
                "没有选择目录。\n\n如果你还没安装闲鱼卖家客户端，"
                "现在打开闲鱼官网去下载吗？"):
            webbrowser.open(CLIENT_DOWNLOAD_URL)

    def on_install(self):
        self.ctrl.install_deps(self._log_line)
        self.logger.log("开始安装依赖（pip + playwright chromium）…")

    def on_init_db(self):
        self.ctrl.init_db(self._log_line)
        self.logger.log("开始初始化数据库…")

    def on_open_web(self):
        """点「打开网站」：浏览器跳转网页控制台（消息导出等已移植到网页）。

        网站没起来时先询问是否代为启动，避免甩一个打不开的标签页给用户。
        """
        if self.ctrl.is_web_up():
            webbrowser.open(WEB_URL)
            self.logger.log(f"已在浏览器打开 {WEB_URL}")
            return
        if not messagebox.askyesno(
                "网站未运行",
                "网页控制台尚未启动，是否现在启动服务（网页 + 引擎）？"):
            self.logger.log("已取消（网站未运行）", "WARN")
            return
        self.on_start()

    def on_view_log(self):
        win = tk.Toplevel(self.root)
        win.title("日志查看")
        win.geometry("680x460")
        win.configure(bg=C["bg_app"])
        st = scrolledtext.ScrolledText(
            win, font=("Consolas", 9), bg=C["bg_app"], fg=C["text_1"],
            insertbackground=C["text_1"], relief="flat", highlightthickness=0)
        st.pack(fill="both", expand=True, padx=8, pady=8)
        files = sorted([p for p in LOGS.glob("*.log")],
                       key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            st.insert("end", "（暂无日志文件）")
        for p in files[:6]:
            st.insert("end", f"\n===== {p.name} (最后 40 行) =====\n")
            try:
                lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
                st.insert("end", "\n".join(lines[-40:]))
            except Exception as e:
                st.insert("end", f"读取失败：{e}")
        st.configure(state="disabled")

    def _log_line(self, line):
        self.logger.log(line, "RUN")

    # ---------- 退出 ----------
    def on_close(self):
        if not messagebox.askokcancel("退出", "退出将关闭网站、引擎与闲鱼客服并释放端口，确定退出？"):
            return
        # 立即标记关闭 + 停止监控，随后销毁窗口（无 I/O，瞬间完成，不卡顿）。
        # 真正的收尾（netstat 查端口、MySQL 写控制意图、停进程）放后台线程，
        # 保证窗口关闭后引擎/客服/网页仍被正常关闭、端口被释放。
        self._closing = True
        self.ctrl.stop_evt.set()
        try:
            self.root.destroy()
        except Exception:
            pass
        threading.Thread(target=self._do_shutdown, daemon=False).start()

    def _do_shutdown(self):
        try:
            self.ctrl.set_control("engine", "stop")
            self.ctrl.stop_engine()
        except Exception:
            pass
        try:
            self.ctrl.set_control("client", "stop")
            self.ctrl.stop_client()
        except Exception:
            pass
        try:
            self.ctrl.stop_web()
        except Exception:
            pass

    def run(self):
        self.root.mainloop()


def main():
    # 单实例判定：已在运行则提示"软件已打开"并退出，不再开第二个窗口
    if not ensure_single_instance():
        return
    try:
        app = App()
        app.run()
    except Exception:
        # 窗口化模式下崩溃无控制台，落盘便于排查
        import traceback
        try:
            if getattr(sys, "frozen", False):
                err_path = Path(sys.executable).resolve().parent / "launcher_err.log"
            else:
                err_path = Path(__file__).resolve().parent / "data" / "logs" / "launcher_err.log"
            err_path.parent.mkdir(parents=True, exist_ok=True)
            err_path.write_text(traceback.format_exc(), encoding="utf-8", errors="replace")
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
