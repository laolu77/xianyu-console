r"""uninstaller_ui.py — 闲鱼客服控制台 · 卸载界面组件库

与 console_ui.py（控制台）共用同一套设计令牌（C），视觉完全一致。

设计取舍（对应 UNINSTALL_SPEC.md）：
  * 语义色铁律：绿色只表示"安全/成功/进度"，红色只表示"危险/将被删除"。
    旧版「会删除」用绿色是错的 —— 用户会误以为那是安全选项。
  * 不用「会删除 / 不会删除」这种双重否定，改成「完整卸载 / 仅删快捷方式」
    两张单选卡，用户判断的是"删到什么程度"。
  * 「同时删除本机配置」独立成组（这是另一个维度的决策），勾选时
    上方警告条文案同步升级 —— 让用户实时看到自己将失去什么。
  * 「开始卸载」用实心红（danger_solid），这是危险主操作，必须最醒目。

为什么 RoundButton / StatusBanner 写在本地：
    原设计稿放在 installer_ui.py，但本项目只有 console_ui.py，
    引进来会多一个必须存在的文件（打包/复制漏了就直接 ImportError）。
    这两个组件各自只有几十行，内置更稳。

用法：
    from uninstaller_ui import Uninstaller
    un = Uninstaller(root, install_dir=r"D:\\XianyuConsole")
    un.on_confirm = lambda opts: do_uninstall(opts)
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont

from console_ui import C, FONT_UI, FONT_UI_SM

FONT_H1 = ("Microsoft YaHei UI", 11, "bold")


def _font(size_delta: int = 0, bold: bool = False):
    return (FONT_UI[0], FONT_UI[1] + size_delta,
            "bold") if bold else (FONT_UI[0], FONT_UI[1] + size_delta)


class RoundButton(tk.Canvas):
    """扁平按钮，变体：primary / ghost / danger / danger_solid。

    danger_solid = 红底白字，仅用于"危险主操作"（开始卸载）。
    danger       = 描边红，留给次级危险场景。
    """

    def __init__(self, master, text, variant="ghost", command=None,
                 bg=None, width=None):
        bg = bg or C["bg_surface"]
        super().__init__(master, height=32, bg=bg, highlightthickness=0,
                         cursor="hand2", takefocus=1)
        self._bg = bg
        self.f = tkfont.Font(self, family=FONT_UI_SM[0], size=FONT_UI_SM[1])
        self.configure(width=width or int(self.f.measure(text)) + 30)
        self.text, self.variant, self.command = text, variant, command
        self.enabled, self.hover, self.focused = True, False, False
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Enter>", lambda e: self._hover(True))
        self.bind("<Leave>", lambda e: self._hover(False))
        self.bind("<Button-1>", lambda e: self._click())
        self.bind("<FocusIn>", lambda e: self._focus(True))
        self.bind("<FocusOut>", lambda e: self._focus(False))
        self.bind("<space>", lambda e: self._click())
        self.bind("<Return>", lambda e: self._click())
        self._draw()

    def _style(self):
        if not self.enabled:
            return dict(fill=self._bg, outline=C["border"], text=C["text_3"])
        if self.variant == "primary":
            return dict(fill=C["ok"], outline=C["ok"], text="#FFFFFF")
        if self.variant == "danger_solid":
            return dict(fill=C["err"], outline=C["err"], text="#FFFFFF")
        if self.variant == "danger":
            return dict(fill=self._bg, outline=C["err"], text=C["err"])
        return dict(fill=self._bg, outline=C["border"], text=C["text_1"])

    def _draw(self):
        self.delete("all")
        s = self._style()
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1 or h <= 1:
            return
        outline = C["accent"] if self.focused else (
            C["text_3"] if self.hover and self.enabled else s["outline"])
        lw = 2 if self.focused else 1
        self.create_rectangle(lw, lw, w - lw, h - lw, width=lw,
                              fill=s["fill"], outline=outline)
        self.create_text(w / 2, h / 2, text=self.text, fill=s["text"],
                         font=self.f)

    def set_state(self, enabled=True, text=None):
        self.enabled = enabled
        if text:
            self.text = text
            self.configure(width=int(self.f.measure(text)) + 30)
        self.configure(cursor="hand2" if enabled else "arrow")
        self._draw()

    def _hover(self, on):
        self.hover = on
        self._draw()

    def _focus(self, on):
        self.focused = on
        self._draw()

    def _click(self):
        if self.enabled and self.command:
            self.command()


class StatusBanner(tk.Frame):
    """四态提示条：ok / warn / err / info。左侧竖条着色 + 标题 + 说明。"""

    TONES = {
        "ok":   ("ok", "ok_bg"),
        "warn": ("warn", "warn_bg"),
        "err":  ("err", "err_bg"),
        "info": ("accent", "bg_raised"),
    }

    def __init__(self, master, bg=None):
        bg = bg or C["bg_app"]
        super().__init__(master, bg=bg, highlightthickness=1,
                         highlightbackground=C["border"])
        self._bg = bg
        self.bar = tk.Frame(self, width=3)
        self.bar.pack(side="left", fill="y")
        box = tk.Frame(self, bg=bg)
        box.pack(side="left", fill="both", expand=True, padx=(10, 12), pady=9)
        self.title = tk.Label(box, bg=bg, fg=C["text_1"],
                              font=(FONT_UI[0], FONT_UI[1], "bold"),
                              anchor="w", justify="left")
        self.title.pack(fill="x")
        self.desc = tk.Label(box, bg=bg, fg=C["text_2"], font=FONT_UI_SM,
                             anchor="w", justify="left")
        self.desc.pack(fill="x", pady=(2, 0))

    def set(self, tone: str, title: str, desc: str = ""):
        fg, bgkey = self.TONES.get(tone, self.TONES["info"])
        self.configure(bg=C[bgkey], highlightbackground=C[bgkey])
        self.bar.configure(bg=C[fg])
        for w in (self.title, self.desc):
            w.configure(bg=C[bgkey])
        self.title.configure(text=title, fg=C["text_1"])
        self.desc.configure(text=desc) if desc else self.desc.configure(text="")
        return self


class CheckBox(tk.Canvas):
    """自绘复选框：14px 方框，勾选时琥珀填充 + 深色对勾。

    琥珀色 = "可撤销的破坏"，与红色主操作（不可撤销）做视觉区分。
    """

    def __init__(self, master, text, sub="", on_change=None, checked=False,
                 bg=None):
        bg = bg or C["bg_app"]
        super().__init__(master, height=52, bg=bg, highlightthickness=0,
                         cursor="hand2")
        self._bg = bg
        self.text, self.sub, self.on_change = text, sub, on_change
        self.checked = checked
        self.f = tkfont.Font(self, family=FONT_UI[0], size=FONT_UI[1])
        self.f_sub = tkfont.Font(self, family=FONT_UI_SM[0], size=FONT_UI_SM[1])
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Button-1>", lambda e: self.toggle())
        self.bind("<space>", lambda e: self.toggle())
        self._draw()

    def _draw(self):
        self.delete("all")
        if self.checked:
            self.create_rectangle(1, 14, 15, 28, fill=C["warn"],
                                  outline=C["warn"])
            self.create_line(4, 21, 7, 25, fill=C["bg_app"], width=2)
            self.create_line(7, 25, 12, 17, fill=C["bg_app"], width=2)
        else:
            self.create_rectangle(1, 14, 15, 28, fill=C["bg_raised"],
                                  outline="#4A4A46", width=1)
        self.create_text(23, 12, text=self.text, anchor="w",
                         fill=C["text_1"] if self.checked else C["text_2"],
                         font=self.f)
        if self.sub:
            self.create_text(23, 32, text=self.sub, anchor="w",
                             fill=C["text_3"], font=self.f_sub)

    def toggle(self):
        self.set(not self.checked)

    def set(self, checked, notify=True):
        self.checked = bool(checked)
        self._draw()
        if notify and self.on_change:
            self.on_change(self.checked)

    def get(self) -> bool:
        return self.checked


class OptionCard(tk.Frame):
    """单选卡片：整行可点，选中时整卡描边转红 + 实心红圆点 + 标题提亮。"""

    def __init__(self, master, text, value, group, accent, badge=None,
                 desc=""):
        super().__init__(master, bg=C["bg_surface"], cursor="hand2",
                         highlightthickness=1,
                         highlightbackground=C["border"])
        self.value, self.group, self.accent = value, group, accent
        self.selected = False
        self.badge = badge
        self.on_change = None
        self.f = tkfont.Font(self, family=FONT_UI[0], size=FONT_UI[1])
        self.f_sub = tkfont.Font(self, family=FONT_UI_SM[0], size=FONT_UI_SM[1])

        self.dot = tk.Canvas(self, width=14, height=14, bg=C["bg_surface"],
                             highlightthickness=0)
        self.dot.pack(side="left", padx=(12, 0), pady=12)
        self.lbl = tk.Label(self, text=text, bg=C["bg_surface"],
                            fg=C["text_2"], font=self.f, anchor="w")
        self.lbl.pack(side="left", padx=(9, 8), pady=12)
        self.badge_lbl = None
        if badge:
            self.badge_lbl = tk.Label(self, text=" %s " % badge,
                                      bg=C["err_bg"], fg=C["err"],
                                      font=self.f_sub)
            self.badge_lbl.pack(side="left", pady=12)
        self.desc_lbl = tk.Label(self, text=desc, bg=C["bg_surface"],
                                 fg=C["text_3"], font=self.f_sub, anchor="w",
                                 justify="left")
        self.desc_lbl.pack(side="left", padx=(8, 12), pady=12)
        for w in (self, self.dot, self.lbl, self.desc_lbl):
            w.bind("<Button-1>", self._pick)
        self._draw()

    def _draw(self):
        self.configure(highlightbackground=self.accent if self.selected
                       else C["border"],
                       highlightthickness=1)
        self.lbl.configure(fg=C["text_1"] if self.selected else C["text_2"])
        self.desc_lbl.configure(fg=C["text_2"] if self.selected else C["text_3"])
        self.dot.delete("all")
        if self.selected:
            self.dot.create_oval(0, 0, 14, 14, outline=self.accent, width=2)
            self.dot.create_oval(3.5, 3.5, 10.5, 10.5, fill=self.accent,
                                 outline="")
        else:
            self.dot.create_oval(1, 1, 13, 13, outline="#4A4A46", width=2)

    def _pick(self, _=None):
        if not self.selected:
            self.group.select(self)
            if self.on_change:
                self.on_change(self.value)


class OptionGroup(tk.Frame):
    def __init__(self, master, accent=None, bg=None):
        bg = bg or C["bg_app"]
        super().__init__(master, bg=bg)
        self.items = []
        self.value = None
        self.accent = accent or C["err"]
        self.on_change = None

    def add(self, text, value, desc="", badge=None):
        r = OptionCard(self, text, value, self, self.accent, badge, desc)
        r.pack(fill="x", pady=(0, 8))
        r.on_change = lambda v: self._on_pick(v)
        self.items.append(r)
        return r

    def _on_pick(self, v):
        self.value = v
        if self.on_change:
            self.on_change(v)

    def select(self, item):
        for r in self.items:
            r.selected = (r is item)
            r._draw()
        self.value = item.value

    def select_value(self, v):
        for r in self.items:
            if r.value == v:
                self.select(r)
                return
        if self.items:
            self.select(self.items[0])


class ProgressBar(tk.Frame):
    """4px 细进度条：灰色轨道常驻可见，0% 时也能看出这里有个进度轨道。"""

    def __init__(self, master, bg=None):
        bg = bg or C["bg_app"]
        super().__init__(master, bg=bg)
        self.track = tk.Frame(self, height=4, bg=C["bg_raised"])
        self.track.pack(fill="x")
        self.fill = tk.Frame(self.track, height=4, bg=C["ok"], width=0)
        self.fill.place(x=0, y=0, relheight=1)
        self.value = 0.0
        self.color = C["ok"]
        self.track.bind("<Configure>", lambda e: self._draw())

    def _draw(self):
        w = self.track.winfo_width()
        self.fill.configure(width=max(0, int(w * self.value)), bg=self.color)

    def set(self, value, color=None):
        self.value = max(0.0, min(1.0, value))
        if color:
            self.color = color
        self._draw()


class Uninstaller(tk.Frame):
    """卸载界面外壳：选范围 → 确认 → 执行（带进度）→ 结束。"""

    def __init__(self, master, install_dir="", version="v1.0.0"):
        super().__init__(master, bg=C["bg_app"])
        self.master = master
        master.configure(bg=C["bg_app"])
        master.minsize(640, 660)
        self.pack(fill="both", expand=True)

        self.on_confirm = None
        self.on_cancel = None
        self.install_dir = install_dir
        self.running = False
        self._synced_once = False

        self._header(version)
        self._body()
        self._footer()

    # ---------- 头部 ----------
    def _header(self, version):
        head = tk.Frame(self, bg=C["bg_surface"])
        head.pack(fill="x")
        inner = tk.Frame(head, bg=C["bg_surface"])
        inner.pack(fill="x", padx=22, pady=13)
        logo = tk.Canvas(inner, width=34, height=34, bg=C["bg_surface"],
                         highlightthickness=0)
        logo.create_rectangle(0, 0, 34, 34, fill=C["err_bg"],
                              outline=C["err"], width=1)
        # 垃圾桶：盖 + 身 + 竖纹
        logo.create_line(8, 12, 26, 12, fill="#F09595", width=2)
        logo.create_line(14, 9, 20, 9, fill="#F09595", width=2)
        logo.create_line(14, 12, 14, 9, fill="#F09595", width=2)
        logo.create_line(20, 12, 20, 9, fill="#F09595", width=2)
        logo.create_line(11, 15, 12, 25, fill="#F09595", width=2)
        logo.create_line(17, 15, 17, 25, fill="#F09595", width=2)
        logo.create_line(23, 15, 22, 25, fill="#F09595", width=2)
        logo.create_line(10, 25, 24, 25, fill="#F09595", width=2)
        logo.pack(side="left", padx=(0, 12))

        txt = tk.Frame(inner, bg=C["bg_surface"])
        txt.pack(side="left")
        tk.Label(txt, text="卸载 闲鱼客服控制台", bg=C["bg_surface"],
                 fg=C["text_1"], font=(FONT_UI[0], 10, "bold"),
                 anchor="w").pack(anchor="w")
        tk.Label(txt, text=version, bg=C["bg_surface"], fg=C["text_3"],
                 font=FONT_UI_SM, anchor="w").pack(anchor="w")
        tk.Label(inner, text=" 危险操作 ", bg=C["err_bg"], fg=C["err"],
                 font=FONT_UI_SM).pack(side="right")

        tk.Frame(self, bg=C["border"], height=1).pack(fill="x")

    # ---------- 内容 ----------
    def _body(self):
        self.content = tk.Frame(self, bg=C["bg_app"])
        self.content.pack(fill="both", expand=True)

        self.canvas = tk.Canvas(self.content, bg=C["bg_app"],
                                highlightthickness=0, bd=0)
        self.vbar = tk.Scrollbar(self.content, orient="vertical",
                                 command=self.canvas.yview,
                                 bg=C["bg_raised"], troughcolor=C["bg_app"],
                                 activebackground=C["text_3"],
                                 bd=0, highlightthickness=0, width=10)
        self.canvas.configure(yscrollcommand=self.vbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)

        self.inner = tk.Frame(self.canvas, bg=C["bg_app"])
        self._win = self.canvas.create_window((0, 0), window=self.inner,
                                              anchor="nw")
        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.inner.bind("<MouseWheel>", self._on_wheel)

        pad = tk.Frame(self.inner, bg=C["bg_app"])
        pad.pack(fill="both", expand=True, padx=22, pady=14)

        tk.Label(pad, text="即将卸载", bg=C["bg_app"], fg=C["text_1"],
                 font=FONT_H1, anchor="w").pack(anchor="w")
        tk.Label(pad,
                 text="程序文件与快捷方式会被移除。下面选择要删到什么程度。",
                 bg=C["bg_app"], fg=C["text_2"], font=FONT_UI,
                 anchor="w", justify="left").pack(anchor="w", pady=(4, 12))

        # 安装位置
        tk.Label(pad, text="安装位置", bg=C["bg_app"], fg=C["text_3"],
                 font=FONT_UI_SM, anchor="w").pack(anchor="w", pady=(0, 4))
        self.dir_lbl = tk.Label(pad, text=self.install_dir or "—",
                                bg=C["bg_surface"], fg=C["text_1"],
                                font=FONT_UI_SM, anchor="w", justify="left",
                                padx=10, pady=8, wraplength=560)
        self.dir_lbl.pack(fill="x", pady=(0, 12))

        # 卸载范围
        tk.Label(pad, text="卸载范围", bg=C["bg_app"], fg=C["text_3"],
                 font=FONT_UI_SM, anchor="w").pack(anchor="w", pady=(0, 6))
        self.group = OptionGroup(pad, accent=C["err"])
        self.group.pack(fill="x")
        self.opt_full = self.group.add(
            "完整卸载", "full", "删除程序文件、桌面与开始菜单快捷方式", "推荐")
        self.opt_shortcut = self.group.add(
            "仅删除快捷方式", "shortcut", "保留程序文件，只移除桌面与开始菜单入口")
        self.group.select_value("full")

        # 本机数据（独立分组：数据删除是另一个维度的决策）
        tk.Label(pad, text="本机数据", bg=C["bg_app"], fg=C["text_3"],
                 font=FONT_UI_SM, anchor="w").pack(anchor="w", pady=(6, 4))
        self.chk_data = CheckBox(pad, "同时删除本机配置",
                                 "数据库连接、登录密码、日志",
                                 on_change=self._on_data_toggle,
                                 checked=False)
        self.chk_data.pack(fill="x")
        tk.Label(pad, text="取消勾选则保留 data/，重装后不用重新填数据库连接。",
                 bg=C["bg_app"], fg=C["text_3"], font=FONT_UI_SM,
                 anchor="w").pack(anchor="w", padx=(23, 0), pady=(0, 12))

        # 后果预告（随勾选状态升级）
        self.notice = StatusBanner(pad)
        self.notice.pack(fill="x")
        self.notice.set("warn", "数据库里的业务数据不会被删除",
                        "卡密、商品、规则、订单都还在。")

        # 进度
        prog = tk.Frame(pad, bg=C["bg_app"])
        prog.pack(fill="x", pady=(14, 0))
        phead = tk.Frame(prog, bg=C["bg_app"])
        phead.pack(fill="x", pady=(0, 7))
        tk.Label(phead, text="执行进度", bg=C["bg_app"], fg=C["text_2"],
                 font=FONT_UI, anchor="w").pack(side="left")
        self.prog_text = tk.Label(phead, text="就绪", bg=C["bg_app"],
                                  fg=C["text_3"], font=FONT_UI_SM, anchor="w")
        self.prog_text.pack(side="right")
        self.progress = ProgressBar(prog)
        self.progress.pack(fill="x")
        self.prog_hint = tk.Label(
            prog, text="点「开始卸载」执行。删除过程中请勿关闭程序。",
            bg=C["bg_app"], fg=C["text_3"], font=FONT_UI_SM, anchor="w")
        self.prog_hint.pack(anchor="w", pady=(7, 0))

    def _on_inner(self, _=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self._sync_scrollbar()

    def _on_canvas(self, e):
        self.canvas.itemconfigure(self._win, width=e.width)
        self._sync_scrollbar()

    def _sync_scrollbar(self):
        bbox = self.canvas.bbox("all")
        need = bool(bbox and bbox[3] > self.canvas.winfo_height() + 2)
        if need and not self.vbar.winfo_ismapped():
            self.vbar.pack(side="right", fill="y")
            self.canvas.pack_forget()
            self.canvas.pack(side="left", fill="both", expand=True)
        elif not need and self.vbar.winfo_ismapped():
            self.vbar.pack_forget()
        if not self._synced_once:
            self.canvas.yview_moveto(0)
            self._synced_once = True

    def _on_wheel(self, e):
        if self.vbar.winfo_ismapped():
            self.canvas.yview_scroll(-1 * (e.delta // 120), "units")

    def _on_data_toggle(self, checked):
        if checked:
            self.notice.set("err", "本机配置会被一并删除",
                            "data/ 里的数据库连接、登录密码、日志都会没。")
        else:
            self.notice.set("warn", "数据库里的业务数据不会被删除",
                            "卡密、商品、规则、订单都还在。")

    # ---------- 底部 ----------
    def _footer(self):
        tk.Frame(self, bg=C["border"], height=1).pack(fill="x")
        foot = tk.Frame(self, bg=C["bg_surface"])
        foot.pack(fill="x")
        fin = tk.Frame(foot, bg=C["bg_surface"])
        fin.pack(fill="x", padx=22, pady=11)
        self.hint = tk.Label(fin, text="卸载不可撤销", bg=C["bg_surface"],
                             fg=C["text_3"], font=FONT_UI_SM, anchor="w")
        self.hint.pack(side="left")
        self.btn_start = RoundButton(fin, "开始卸载", "danger_solid",
                                     self.start, bg=C["bg_surface"])
        self.btn_start.pack(side="right")
        self.btn_cancel = RoundButton(fin, "取消", "ghost", self.cancel,
                                      bg=C["bg_surface"])
        self.btn_cancel.pack(side="right", padx=(0, 8))

    # ---------- 交互 ----------
    def set_install_dir(self, path):
        self.install_dir = str(path or "")
        self.dir_lbl.configure(text=self.install_dir or "—")

    def options(self) -> dict:
        return dict(scope=self.group.value or "full",
                    delete_data=self.chk_data.get(),
                    install_dir=self.install_dir)

    def start(self):
        if self.running:
            return
        self.running = True
        self.btn_start.set_state(False)
        self.btn_cancel.set_state(False)
        self.progress.set(0.0)
        self.prog_text.configure(text="准备中…", fg=C["text_3"])
        if self.on_confirm:
            self.on_confirm(self.options())

    def cancel(self):
        if self.running:
            return
        if self.on_cancel:
            self.on_cancel()
        else:
            self.master.destroy()

    def set_progress(self, ratio, text=None, color=None):
        self.progress.set(ratio, color)
        if text:
            self.prog_text.configure(text=text, fg=color or C["text_2"])

    def finish(self, ok=True, text="卸载完成"):
        self.running = False
        self.btn_start.set_state(True)
        self.btn_cancel.set_state(True)
        if ok:
            self.set_progress(1.0, text, C["ok"])
            self.hint.configure(text="卸载已完成，可以关闭窗口")
            self.prog_hint.configure(text="残留文件会在程序退出后自动清理。")
        else:
            self.set_progress(self.progress.value, text, C["err"])
            self.hint.configure(text="卸载未完全成功，请查看上方提示")
