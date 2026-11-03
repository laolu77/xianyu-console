"""
console_ui.py — 闲鱼卖家客服控制台 · Tkinter 组件库
设计稿落地版：语义色令牌 / 指标卡 / 三档按钮 / 彩色日志终端

用法：
    from console_ui import ConsoleUI
    ui = ConsoleUI(root)
    ui.set_stat("php", "正常", "响应 24 ms", "ok")
    ui.append_log("19:53:55", "INFO", "网页已启动")
"""

import os
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

# ── 1. 设计令牌 ──────────────────────────────────────────────
C = {
    "bg_app":     "#121211",
    "bg_surface": "#1C1C1A",
    "bg_raised":  "#232320",
    "border":     "#33332F",
    "text_1":     "#EDECE8",
    "text_2":     "#A3A29C",
    "text_3":     "#6E6D68",
    "ok":         "#1D9E75",
    "ok_bg":      "#12241E",
    "warn":       "#EF9F27",
    "warn_bg":    "#2A1F0E",
    "err":        "#E24B4A",
    "err_bg":     "#2A1413",
    "accent":     "#378ADD",
}

S = {"1": 4, "2": 8, "3": 12, "4": 16, "6": 24, "8": 32}
FONT_UI = ("Microsoft YaHei UI", 9)      # ≈13px
FONT_UI_SM = ("Microsoft YaHei UI", 8)   # ≈11px
FONT_MONO = ("Cascadia Mono", 8)         # ≈11.5px

LEVEL_COLOR = {"INFO": C["ok"], "WARN": C["warn"],
               "ERROR": C["err"], "DEBUG": C["text_3"]}

ICON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")


def load_icon(size=26, kind="png"):
    """加载应用图标；找不到文件返回 None，由调用方回退到手绘方案。

    kind='png' → PhotoImage（顶栏内嵌用）
    kind='ico' → 图标资源（root.iconbitmap 用）
    """
    if kind == "ico":
        # 多尺寸 ICO 只有一份固定文件名 icon.ico，size 参数忽略
        path = os.path.join(ICON_DIR, "icon.ico")
    else:
        path = os.path.join(ICON_DIR, f"icon_{size}.{kind}")
    if not os.path.exists(path):
        return None
    try:
        if kind == "ico":
            return path
        return tk.PhotoImage(file=path)
    except (tk.TclError, RuntimeError):
        # RuntimeError：Tk() 之前调用 / 根窗口已销毁
        return None


def apply_window_icon(root, size=32):
    """把图标设到窗口标题栏；跨平台分别处理，失败静默跳过。"""
    p = load_icon(size, "ico")
    if p:
        try:
            root.iconbitmap(p)
            return True
        except tk.TclError:
            pass
    img = load_icon(size, "png")
    if img:
        try:
            root.iconphoto(True, img)
            return True
        except tk.TclError:
            pass
    return False


def _rounded(w, h, r, fill, outline, width=1):
    """在任意 Canvas 上画圆角矩形。"""
    return w.create_rectangle(0, 0, w.winfo_width(), w.winfo_height(),
                              fill=fill, outline=outline, width=width)


class StatCard(tk.Canvas):
    """指标卡：状态点 + 标签 / 主值 / 副信息，支持点击复制。"""

    def __init__(self, master, key, label, value, sub, state="ok"):
        super().__init__(master, height=76, bg=C["bg_app"],
                         highlightthickness=0, cursor="hand2")
        self.key, self.state, self.sub = key, state, sub
        self.f1 = (tkfont.Font(self, family=FONT_UI[0], size=FONT_UI[1],
                               weight="normal"),)
        self.f_val = tkfont.Font(self, family=FONT_UI[0], size=13,
                                 weight="bold")
        self.f_sub = tkfont.Font(self, family=FONT_UI_SM[0], size=FONT_UI_SM[1])
        self._outline = C["border"]
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Button-1>", self._copy)
        self.set(label, value, sub, state)

    def _draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        self.create_rectangle(0, 0, w - 1, h - 1, fill=C["bg_surface"],
                              outline=self._outline, width=1)
        cy = 20
        color = {"ok": C["ok"], "warn": C["warn"],
                 "err": C["err"], "off": "#4A4A46"}[self.state]
        if self.state == "ok":
            self.create_oval(16, cy - 4, 24, cy + 4, fill=color, outline="")
        else:
            self.create_oval(16, cy - 4, 24, cy + 4, fill=color, outline="")
        self.create_text(32, cy, text=self._label, anchor="w",
                         fill=C["text_2"], font=self.f1)
        self.create_text(16, 46, text=self._value, anchor="w",
                         fill=C["text_1"], font=self.f_val)
        sub = self.sub
        while sub and tkfont.Font(font=self.f_sub).measure(sub) > w - 32:
            sub = sub[:-1]
        self.create_text(16, 64, text=sub, anchor="w",
                         fill=C["text_3"], font=self.f_sub)

    def set(self, label, value, sub, state="ok"):
        self._label, self._value, self.sub, self.state = label, value, sub, state
        self._outline = C["err"] if state == "err" else C["border"]
        self._draw()

    def _copy(self, _=None):
        try:
            self.clipboard_clear()
            self.clipboard_append(f"{self._label}: {self._value} {self.sub}")
            self.configure(cursor="arrow")
            self.after(1200, lambda: self.configure(cursor="hand2"))
        except tk.TclError:
            pass


class ClientCard(tk.Frame):
    """客户端卡：闲鱼客服 exe 安装状态 + 路径 + 下载安装 / 重新检测。

    整行铺满，替代原界面里被挤在环境状态行尾部、按钮悬空的一行。
    """

    def __init__(self, master):
        super().__init__(master, bg=C["bg_surface"], height=72)
        self.pack_propagate(False)
        self.state = "off"
        self.path = "—"
        self.version = "—"
        self.f_val = tkfont.Font(self, family=FONT_UI[0], size=13,
                                 weight="bold")
        self.f_sub = tkfont.Font(self, family=FONT_UI_SM[0], size=FONT_UI_SM[1])
        self.f_lbl = tkfont.Font(self, family=FONT_UI[0], size=FONT_UI[1])

        left = tk.Frame(self, bg=C["bg_surface"])
        left.pack(side="left", fill="both", expand=True, padx=(14, 8))
        top = tk.Frame(left, bg=C["bg_surface"])
        top.pack(anchor="w")
        self._dot = tk.Canvas(top, width=7, height=12, bg=C["bg_surface"],
                              highlightthickness=0)
        self._dot.pack(side="left", padx=(0, 6), pady=(1, 0))
        tk.Label(top, text="闲鱼客服客户端", bg=C["bg_surface"],
                 fg=C["text_2"], font=self.f_lbl).pack(side="left")
        self.val_lbl = tk.Label(left, text="检测中", bg=C["bg_surface"],
                                fg=C["text_1"], font=self.f_val)
        self.val_lbl.pack(anchor="w", pady=(4, 0))
        self.path_lbl = tk.Label(left, text="—", bg=C["bg_surface"],
                                 fg=C["text_3"], font=self.f_sub, anchor="w")
        self.path_lbl.pack(anchor="w", fill="x")

        right = tk.Frame(self, bg=C["bg_surface"])
        right.pack(side="right", padx=(0, 14))
        self.btn_recheck = FlatButton(right, "重新检测", "ghost")
        self.btn_recheck.pack(side="left", padx=(8, 0))
        self.btn_install = FlatButton(right, "下载安装", "ghost")
        self.btn_install.pack(side="left")

        self.set_state("off", "—", "—")

    def _paint(self):
        color = {"ok": C["ok"], "warn": C["warn"], "err": C["err"],
                 "off": "#4A4A46"}[self.state]
        self._dot.delete("all")
        self._dot.create_oval(0, 3, 7, 10, fill=color, outline="")
        self.val_lbl.configure(fg=C["text_1"] if self.state != "off"
                               else C["text_3"])
        # 已安装且非过期时才禁用「下载安装」；否则它是该行的主动作
        need_install = self.state in ("off", "warn")
        self.btn_install.set_state(need_install)
        if need_install:
            self.btn_install.configure_variant("primary", "下载安装")
        else:
            self.btn_install.configure_variant("ghost", "已安装")

    def set_state(self, state, version, path):
        """state ∈ ok / warn / off / err；version、path 为展示文本。"""
        self.state, self.version, self.path = state, version, path
        self.val_lbl.configure(text=version)
        self._fit_path(path)
        self._paint()

    def _fit_path(self, path):
        avail = max(60, self.path_lbl.winfo_width() or 460)
        while path and self.f_sub.measure(path) > avail:
            path = path[:-1]
        self.path_lbl.configure(text=path, cursor="hand2")
        self.path_lbl.bind("<Button-1>", self._copy)

    def _copy(self, _=None):
        try:
            self.clipboard_clear()
            self.clipboard_append(self.path)
        except tk.TclError:
            pass

    def _on_resize(self, _=None):
        self._fit_path(self.path)


class FlatButton(tk.Canvas):
    """扁平按钮，三档：primary / ghost / danger。含键盘焦点环。"""

    def __init__(self, master, text, variant="ghost", command=None, width=None):
        w = width or (14 + len(text) * 9)
        super().__init__(master, width=w, height=32, bg=C["bg_app"],
                         highlightthickness=0, cursor="hand2", takefocus=1)
        self.text, self.variant, self.command = text, variant, command
        self.enabled, self.hover, self.focused = True, False, False
        self.f = tkfont.Font(self, family=FONT_UI[0], size=FONT_UI_SM[1])
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Enter>", lambda e: self._hover_on())
        self.bind("<Leave>", lambda e: self._hover_off())
        self.bind("<Button-1>", lambda e: self._click())
        self.bind("<FocusIn>", lambda e: self._set_focus(True))
        self.bind("<FocusOut>", lambda e: self._set_focus(False))
        self.bind("<space>", lambda e: self._click())
        self.bind("<Return>", lambda e: self._click())
        self._draw()

    def _style(self):
        if not self.enabled:
            return dict(fill=C["bg_app"], outline=C["border"], text=C["text_3"])
        if self.variant == "primary":
            return dict(fill=C["ok"], outline=C["ok"], text="#FFFFFF")
        if self.variant == "danger":
            return dict(fill=C["bg_app"], outline=C["err"], text=C["err"])
        return dict(fill=C["bg_app"], outline=C["border"], text=C["text_1"])

    def _draw(self):
        self.delete("all")
        s = self._style()
        w, h = self.winfo_width(), self.winfo_height()
        outline = C["accent"] if self.focused else (
            C["text_3"] if self.hover else s["outline"])
        width = 2 if self.focused else 1
        self.create_rectangle(width, width, w - width, h - width, width=width,
                              fill=s["fill"], outline=outline)
        self.create_text(w / 2, h / 2, text=self.text, fill=s["text"], font=self.f)

    def set_state(self, enabled=True, text=None):
        self.enabled = enabled
        if text:
            self.text = text
            self.configure(width=14 + len(text) * 9)
        self.configure(cursor="hand2" if enabled else "arrow")
        self._draw()

    def set_text(self, text):
        self.set_state(self.enabled, text)

    def configure_variant(self, variant, text=None):
        """同时切换视觉变体与文案，并重算画布宽度。"""
        self.variant = variant
        if text:
            self.text = text
            self.configure(width=14 + len(text) * 9)
        self._draw()

    def _hover_on(self):
        self.hover = True; self._draw()

    def _hover_off(self):
        self.hover = False; self._draw()

    def _set_focus(self, on):
        self.focused = on; self._draw()

    def _click(self):
        if self.enabled and self.command:
            self.command()


class Terminal(tk.Frame):
    """日志终端：级别着色 / 5000 行上限 / 自动滚动可暂停。"""

    MAX = 5000

    def __init__(self, master):
        super().__init__(master, bg=C["bg_app"], highlightbackground=C["border"],
                         highlightthickness=1)
        head = tk.Frame(self, bg=C["bg_app"])
        head.pack(fill="x", padx=12, pady=8)
        tk.Label(head, text="运行日志", bg=C["bg_app"], fg=C["text_2"],
                 font=FONT_UI).pack(side="left")
        self.pause_btn = tk.Label(head, text="暂停滚动", bg=C["bg_app"],
                                  fg=C["text_3"], font=FONT_UI_SM, cursor="hand2")
        self.pause_btn.pack(side="left", padx=(16, 0))
        self.pause_btn.bind("<Button-1>", self.toggle_pause)
        tk.Label(head, text="自动滚动 · 保留 5000 行", bg=C["bg_app"],
                 fg=C["text_3"], font=FONT_UI_SM).pack(side="right")

        body = tk.Frame(self, bg=C["bg_app"])
        body.pack(fill="both", expand=True, padx=(1, 1), pady=(0, 1))
        self.text = tk.Text(body, bg=C["bg_app"], fg=C["text_1"],
                            font=FONT_MONO, wrap="word", height=14,
                            bd=0, highlightthickness=0, padx=12, pady=10,
                            spacing1=0, spacing3=2, insertwidth=0)
        sb = ttk.Scrollbar(body, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        self.text.tag_configure("time", foreground=C["text_3"])
        self.text.tag_configure("msg", foreground=C["text_1"])
        for lvl, col in LEVEL_COLOR.items():
            self.text.tag_configure(lvl, foreground=col)
        self.text.configure(state="disabled")
        self.auto = True
        self.show_empty()
        self.text.bind("<MouseWheel>", self._on_wheel)

    def _on_wheel(self, e):
        self.auto = False
        self.pause_btn.configure(text="继续滚动")
        self.text.yview_scroll(-1 * (e.delta // 120), "units")

    def toggle_pause(self, _=None):
        self.auto = not self.auto
        self.pause_btn.configure(text="暂停滚动" if self.auto else "继续滚动")
        if self.auto:
            self.text.see("end")

    def show_empty(self):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("end", "暂无日志，等待引擎输出…", ("msg",))
        self._center()
        self.text.configure(state="disabled")

    def _center(self):
        self.text.tag_add("center", "1.0", "end")
        self.text.tag_configure("center", justify="center",
                                foreground=C["text_3"])

    def append(self, time_str, level, message):
        if int(self.text.index("end-1c").split(".")[0]) <= 1:
            self.text.configure(state="normal")
            self.text.delete("1.0", "end")
        self.text.configure(state="normal")
        self.text.insert("end", time_str + "  ", ("time",))
        self.text.insert("end", f"[{level}]", (level.upper(),))
        self.text.insert("end", "  " + message + "\n", ("msg",))
        if int(self.text.index("end-1c").split(".")[0]) > self.MAX:
            self.text.delete("1.0", "100.0")
        self.text.configure(state="disabled")
        if self.auto:
            self.text.see("end")


class ConsoleUI(ttk.Frame):
    """整套控制台界面。宿主只需 set_stat / append_log / 绑定按钮。"""

    def __init__(self, master):
        super().__init__(master, style="Dark.TFrame", padding=24)
        self.master = master
        master.configure(bg=C["bg_app"])
        master.minsize(720, 520)
        self.pack(fill="both", expand=True)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Dark.TFrame", background=C["bg_app"])
        style.configure("Vertical.TScrollbar", background=C["bg_raised"],
                        troughcolor=C["bg_app"], bordercolor=C["bg_app"],
                        arrowcolor=C["text_2"])

        self._appbar()
        self._stats()
        self._client()
        self._toolbar()
        self._terminal()

    # 顶栏
    def _appbar(self):
        bar = tk.Frame(self, bg=C["bg_app"])
        bar.pack(fill="x", side="top")
        logo = tk.Label(bar, bg=C["bg_app"], bd=0)
        img = load_icon(26)
        if img:
            logo.configure(image=img)
        else:                                    # 无图标文件时回退到 Canvas 绘制
            logo.destroy()
            logo = tk.Canvas(bar, width=26, height=26, bg=C["bg_app"],
                             highlightthickness=0)
            logo.create_polygon(13, 4, 21, 8, 21, 17, 13, 21, 5, 17, 5, 8,
                                fill="#0A4638", outline=C["ok"], width=1)
        logo.pack(side="left", padx=(0, 10))
        txt = tk.Frame(bar, bg=C["bg_app"])
        txt.pack(side="left")
        tk.Label(txt, text="闲鱼卖家客服 · 控制台", bg=C["bg_app"],
                 fg=C["text_1"], font=("Microsoft YaHei UI", 10, "bold"),
                 anchor="w").pack(anchor="w")
        tk.Label(txt, text="自动回复 / 自动发货统一管理", bg=C["bg_app"],
                 fg=C["text_3"], font=FONT_UI_SM, anchor="w").pack(anchor="w")
        v = tk.Label(bar, text=" v2.0.1 ", bg=C["ok_bg"], fg=C["ok"],
                     font=FONT_UI_SM)
        v.pack(side="right")
        tk.Frame(self, bg=C["border"], height=1).pack(fill="x", pady=(12, 16))

    # 指标卡
    def _stats(self):
        holder = tk.Frame(self, bg=C["bg_app"])
        holder.pack(fill="x")
        self.cards = {}
        spec = [("php", "PHP", "检测中", "—"),
                ("mysql", "MySQL", "检测中", "—"),
                ("web", "网站服务", "检测中", "—"),
                ("engine", "引擎进程", "0", "未启动")]
        self.grid_ref = {}
        for i, (k, lbl, val, sub) in enumerate(spec):
            self.grid_ref[k] = i
            card = StatCard(holder, k, lbl, val, sub, "off")
            self.cards[k] = card
        holder.bind("<Configure>", self._relayout_cards)
        self._relayout_cards()

    def _relayout_cards(self, _=None):
        w = self.winfo_width() - 48
        cols = 4 if w >= 620 else (2 if w >= 340 else 1)
        for i, card in enumerate(self.cards.values()):
            card.grid(row=i // cols, column=i % cols, sticky="nsew",
                      padx=(0 if i % cols == 0 else 6, 0),
                      pady=6)
            self.grid_columnconfigure(i % cols, weight=1)
        for r in range((len(self.cards) - 1) // cols + 1):
            self.grid_rowconfigure(r, weight=1)

    def set_stat(self, key, value, sub="", state="ok"):
        if key in self.cards:
            self.cards[key].set(self.cards[key]._label, value, sub, state)

    def set_client_state(self, state, version, path):
        """更新闲鱼客服客户端卡。

        state='ok'    → 已安装，「下载安装」变为禁用的「已安装」
        state='warn'  → 版本过旧，「下载安装」高亮为主操作
        state='off'   → 未安装，「下载安装」高亮
        """
        self.client.set_state(state, version, path)

    # 客户端卡
    def _client(self):
        self.client = ClientCard(self)
        self.client.pack(fill="x", pady=(12, 2))
        self.client.path_lbl.bind("<Configure>", self.client._on_resize)

    # 工具栏
    def _toolbar(self):
        """工具栏：服务开关（单按钮切换）+ 四个 ghost 次操作。

        服务开关合并了「启动服务」与「关闭网站」：
        未运行时为 primary 绿「启动服务」，运行中变danger 红「关闭服务」。
        """
        bar = tk.Frame(self, bg=C["bg_app"])
        bar.pack(fill="x", pady=(10, 14))
        self.btns = {}
        self.service_running = False
        self._danger_armed = False

        specs = [("deps", "安装依赖", "ghost"), ("log", "查看日志", "ghost"),
                 ("db", "初始化数据库", "ghost"), ("export", "导出消息", "ghost"),
                 ("service", "启动服务", "primary")]
        for k, t, v in specs:
            b = FlatButton(bar, t, v)
            b.pack(side="left", padx=(0, 8))
            self.btns[k] = b

        self.btns["service"].command = self._toggle_service
        # 客户端卡内的两个动作统一挂到 btns，调用方不用区分位置
        self.btns["install"] = self.client.btn_install
        self.btns["recheck"] = self.client.btn_recheck

    def _toggle_service(self):
        """服务开关：启动需二次确认，关闭立即执行。"""
        if not self.service_running:
            self._danger_armed = False
            if self.on_start:
                self.on_start()
            return
        if self._danger_armed:
            self._disarm()
            if self.on_stop:
                self.on_stop()
            return
        self._danger_armed = True
        self._sync_service_btn(armed=True)
        self.after(3000, self._disarm)

    def _disarm(self):
        self._danger_armed = False
        if self.service_running:
            self._sync_service_btn()

    def _sync_service_btn(self, armed=False):
        b = self.btns["service"]
        if not self.service_running:
            b.configure_variant("primary", "启动服务")
        elif armed:
            b.configure_variant("danger", "确认关闭？")
        else:
            b.configure_variant("danger", "关闭服务")

    def set_service_state(self, running, busy=False):
        """由后端回报服务真实状态后调用，同步按钮文案与变体。

        running=True  → 红「关闭服务」；running=False → 绿「启动服务」
        busy=True     → 禁用并显示「启动中… / 关闭中…」
        """
        self.service_running = running
        self._danger_armed = False
        b = self.btns["service"]
        if busy:
            b.text = "启动中…" if not running else "关闭中…"
            b.enabled = False
            b.configure(width=14 + len(b.text) * 9,
                        cursor="arrow")
        else:
            b.configure_variant("danger" if running else "primary",
                                "关闭服务" if running else "启动服务")
        b._draw()

    def set_busy(self, busy, start_text="启动服务"):
        self.set_service_state(self.service_running, busy=busy)

    # 日志
    def _terminal(self):
        self.term = Terminal(self)
        self.term.pack(fill="both", expand=True)

    def append_log(self, time_str, level, message):
        self.term.append(time_str, level.upper(), message)

    def clear_log(self):
        self.term.show_empty()


# 宿主需实现的回调
ConsoleUI.on_start = None
ConsoleUI.on_stop = None


if __name__ == "__main__":
    import time

    root = tk.Tk()
    root.title("闲鱼卖家客服 · 控制台启动器")
    root.geometry("880x620")
    apply_window_icon(root)
    ui = ConsoleUI(root)
    ui.set_stat("php", "正常", "响应 24 ms", "ok")
    ui.set_stat("mysql", "已连接", "6 个连接池", "ok")
    ui.set_stat("web", "已停止", "127.0.0.1:8082", "off")
    ui.set_stat("engine", "0", "未启动", "off")
    ui.set_service_state(False)
    ui.set_client_state(
        "ok", "已安装",
        r"<用户名>\AppData\Local\Programs\xianyu-seller-im\闲鱼卖家客服.exe")
    for t, l, m in [
        ("19:53:55", "INFO", "网页已启动 http://127.0.0.1:8082/ (php.exe)"),
        ("19:53:55", "INFO", "自动回复引擎已拉起 (PID 18440)"),
        ("19:54:28", "INFO", "引擎已开启，等待引擎开启…"),
        ("19:54:43", "WARN", "端口 9222 被占用，已复用"),
        ("19:55:16", "INFO", "自动回复引擎已拉起 (PID 24616)"),
    ]:
        ui.append_log(t, l, m)

    def start():
        ui.set_service_state(False, busy=True)
        ui.append_log(time.strftime("%H:%M:%S"), "INFO", "正在启动服务…")

        def done():
            ui.set_service_state(True)
            ui.set_stat("web", "运行中", "127.0.0.1:8082", "ok")
            ui.set_stat("engine", "2", "PID 18440 / 9222", "warn")
            ui.append_log(time.strftime("%H:%M:%S"), "INFO", "服务已启动")
        root.after(1400, done)

    def stop():
        ui.set_service_state(True, busy=True)
        ui.append_log(time.strftime("%H:%M:%S"), "INFO", "正在关闭服务…")

        def done():
            ui.set_service_state(False)
            ui.set_stat("web", "已停止", "127.0.0.1:8082", "off")
            ui.set_stat("engine", "0", "未启动", "off")
            ui.append_log(time.strftime("%H:%M:%S"), "INFO", "服务已关闭")
        root.after(1000, done)

    ui.on_start = start
    ui.on_stop = stop
    ui.btns["deps"].command = lambda: ui.append_log(
        time.strftime("%H:%M:%S"), "INFO", "开始安装依赖…")
    ui.btns["install"].command = lambda: ui.append_log(
        time.strftime("%H:%M:%S"), "INFO", "开始下载闲鱼客服客户端…")
    ui.btns["recheck"].command = lambda: (
        ui.set_client_state("off", "检测中", "—"),
        ui.append_log(time.strftime("%H:%M:%S"), "INFO", "重新检测客户端…"))
    ui.btns["log"].command = lambda: None
    ui.btns["db"].command = lambda: None
    ui.btns["export"].command = lambda: None
    root.mainloop()