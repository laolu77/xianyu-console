"""
installer_ui.py — 闲鱼客服控制台 · 安装向导 Tkinter 组件库

三步向导：环境检测 → 数据库配置 → 安装完成
与 console_ui.py 共用同一套设计令牌（C），视觉完全一致。

用法：
    from installer_ui import InstallerWizard
    wiz = InstallerWizard(root, ["环境检测", "数据库配置", "安装完成"])
    wiz.goto(1)                                  # 跳到第 2 步
    wiz.steps[1].fill(host="127.0.0.1", port="3306", ...)
    wiz.steps[1].set_status("ok", "连接成功", "MySQL 5.7.26")
    wiz.set_next_enabled(True)
    wiz.on_next = lambda step: ...               # 校验通过后调用
"""

import os
import tkinter as tk
from tkinter import font as tkfont

from console_ui_installer import C, FONT_UI, FONT_UI_SM, apply_window_icon, load_icon

FONT_H1 = ("Microsoft YaHei UI", 11, "bold")
FONT_H2 = ("Microsoft YaHei UI", 10, "bold")


# ── 基础控件 ──────────────────────────────────────────────────

class RoundButton(tk.Canvas):
    """胶囊按钮，variant: primary / ghost / danger。"""

    def __init__(self, master, text, variant="ghost", command=None, width=None):
        w = width or (16 + len(text) * 10)
        super().__init__(master, width=w, height=32, bg=C["bg_app"],
                         highlightthickness=0, cursor="hand2", takefocus=1)
        self.text, self.variant, self.command = text, variant, command
        self.enabled, self.hover, self.focused = True, False, False
        self.f = tkfont.Font(self, family=FONT_UI[0], size=FONT_UI_SM[1])
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
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
            self.configure(width=16 + len(text) * 10)
        self.configure(cursor="hand2" if enabled else "arrow")
        self._draw()

    def set_text(self, text):
        self.set_state(self.enabled, text)

    def configure_variant(self, variant, text=None):
        self.variant = variant
        if text:
            self.text = text
            self.configure(width=16 + len(text) * 10)
        self._draw()

    def _set_hover(self, on):
        self.hover = on
        self._draw()

    def _set_focus(self, on):
        self.focused = on
        self._draw()

    def _click(self):
        if self.enabled and self.command:
            self.command()


class Field(tk.Frame):
    """带标签的输入框：标签在上、输入框在下，聚焦时描边转绿。"""

    def __init__(self, master, label, value="", hint="", secret=False, width=28):
        super().__init__(master, bg=C["bg_app"])
        tk.Label(self, text=label, bg=C["bg_app"], fg=C["text_3"],
                 font=FONT_UI_SM, anchor="w").pack(anchor="w", pady=(0, 4))
        self.var = tk.StringVar(value=value)
        self.entry = tk.Entry(self, textvariable=self.var, width=width,
                              bg=C["bg_raised"], fg=C["text_1"],
                              insertbackground=C["text_1"],
                              relief="flat", bd=0, highlightthickness=1,
                              highlightbackground=C["border"],
                              highlightcolor=C["ok"],
                              font=FONT_UI)
        self.entry.pack(fill="x", ipady=5, padx=1, pady=1)
        self.hint_lbl = tk.Label(self, text=hint, bg=C["bg_app"],
                                 fg=C["text_3"], font=FONT_UI_SM, anchor="w")
        if hint:
            self.hint_lbl.pack(anchor="w", pady=(4, 0))
        # 错误标签默认不占位，出现时才插入，避免空行撑高表单
        self.error_lbl = tk.Label(self, text="", bg=C["bg_app"], fg=C["err"],
                                  font=FONT_UI_SM, anchor="w")
        if secret:
            self.entry.configure(show="●")

    def get(self):
        return self.var.get()

    def set(self, v):
        self.var.set(v)

    def set_error(self, msg):
        self.error_lbl.configure(text=msg)
        self.entry.configure(highlightbackground=C["err"] if msg else C["border"])
        if msg:
            if not self.error_lbl.winfo_ismapped():
                self.error_lbl.pack(anchor="w", pady=(3, 0))
        else:
            self.error_lbl.pack_forget()

    def clear_error(self):
        self.set_error("")


class PathField(tk.Frame):
    """路径输入：标签 + 输入框 + 「浏览…」按钮，同一行内。"""

    def __init__(self, master, label, value="", hint="", on_browse=None,
                 width=52):
        super().__init__(master, bg=C["bg_app"])
        tk.Label(self, text=label, bg=C["bg_app"], fg=C["text_3"],
                 font=FONT_UI_SM, anchor="w").pack(anchor="w", pady=(0, 4))
        row = tk.Frame(self, bg=C["bg_app"])
        row.pack(fill="x")
        self.var = tk.StringVar(value=value)
        self.entry = tk.Entry(row, textvariable=self.var, width=width,
                              bg=C["bg_raised"], fg=C["text_1"],
                              insertbackground=C["text_1"],
                              relief="flat", bd=0, highlightthickness=1,
                              highlightbackground=C["border"],
                              highlightcolor=C["ok"], font=FONT_UI)
        self.entry.pack(side="left", fill="x", expand=True, ipady=5,
                        padx=1, pady=1)
        self.btn = RoundButton(row, "浏览…", "ghost", on_browse)
        self.btn.pack(side="left", padx=(8, 0))
        self.hint_lbl = tk.Label(self, text=hint, bg=C["bg_app"],
                                 fg=C["text_3"], font=FONT_UI_SM, anchor="w")
        if hint:
            self.hint_lbl.pack(anchor="w", pady=(5, 0))
        self.error_lbl = tk.Label(self, text="", bg=C["bg_app"], fg=C["err"],
                                  font=FONT_UI_SM, anchor="w")

    def get(self):
        return self.var.get()

    def set(self, v):
        self.var.set(v)

    def set_error(self, msg):
        self.error_lbl.configure(text=msg)
        self.entry.configure(highlightbackground=C["err"] if msg else C["border"])
        if msg:
            if not self.error_lbl.winfo_ismapped():
                self.error_lbl.pack(anchor="w", pady=(3, 0))
        else:
            self.error_lbl.pack_forget()

    def clear_error(self):
        self.set_error("")


class RadioCard(tk.Frame):
    """卡片式单选项，选中时描边转绿并显示实心点。"""

    def __init__(self, master, text, value, group, on_change=None):
        super().__init__(master, bg=C["bg_surface"], cursor="hand2")
        self.value, self.group, self.on_change = value, group, on_change
        self.selected = False
        self.f = tkfont.Font(self, family=FONT_UI[0], size=FONT_UI[1])
        self.dot = tk.Canvas(self, width=14, height=14, bg=C["bg_surface"],
                             highlightthickness=0)
        self.dot.pack(side="left", padx=(10, 0), pady=9)
        self.lbl = tk.Label(self, text=text, bg=C["bg_surface"],
                            fg=C["text_2"], font=self.f, anchor="w")
        self.lbl.pack(side="left", padx=(8, 12), pady=9, fill="x", expand=True)
        for w in (self, self.dot, self.lbl):
            w.bind("<Button-1>", self._pick)
        self._draw()

    def _draw(self):
        self.configure(highlightbackground=C["ok"] if self.selected else C["border"],
                       highlightthickness=1)
        self.lbl.configure(fg=C["text_1"] if self.selected else C["text_2"])
        self.dot.delete("all")
        if self.selected:
            self.dot.create_oval(1, 1, 13, 13, outline=C["ok"], width=2)
            self.dot.create_oval(4, 4, 10, 10, fill=C["ok"], outline="")
        else:
            self.dot.create_oval(1, 1, 13, 13, outline="#4A4A46", width=2)

    def select(self, notify=True):
        self.selected = True
        self._draw()
        if notify and self.on_change:
            self.on_change(self.value)

    def _pick(self, _=None):
        if not self.selected:
            self.group.select(self)
            if self.on_change:
                self.on_change(self.value)


class RadioGroup(tk.Frame):
    """管理一组 RadioCard，选中项互斥。"""

    def __init__(self, master, on_change=None):
        super().__init__(master, bg=C["bg_app"])
        self.items = []
        self.value = None
        self.on_change = on_change

    def add(self, text, value):
        r = RadioCard(self, text, value, self,
                      on_change=lambda v: self._on_pick(v))
        r.pack(fill="x", pady=3)
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
                r.select()
                return
        if self.items:
            self.items[0].select()


class StatusBanner(tk.Frame):
    """状态反馈条：ok / warn / err / info 四种。"""

    COLORS = {
        "ok":   (C["ok_bg"],  C["ok"],  C["text_1"], "✓"),
        "warn": (C["warn_bg"], C["warn"], C["text_1"], "!"),
        "err":  (C["err_bg"], C["err"], C["text_1"], "×"),
        "info": (C["bg_raised"], C["text_3"], C["text_2"], "i"),
    }

    def __init__(self, master):
        super().__init__(master, bg=C["bg_app"])
        self.f_title = tkfont.Font(self, family=FONT_UI[0], size=FONT_UI[1])
        self.f_sub = tkfont.Font(self, family=FONT_UI_SM[0], size=FONT_UI_SM[1])
        self._build()

    def _build(self):
        for c in self.winfo_children():
            c.destroy()
        self.icon = tk.Label(self, text="", bg=C["bg_app"], font=self.f_title)
        self.icon.pack(side="left", padx=(11, 8), pady=9)
        txt = tk.Frame(self, bg=C["bg_app"])
        txt.pack(side="left", fill="x", expand=True)
        self.title_lbl = tk.Label(txt, text="", bg=C["bg_app"], fg=C["text_1"],
                                  font=self.f_title, anchor="w")
        self.title_lbl.pack(anchor="w")
        self.sub_lbl = tk.Label(txt, text="", bg=C["bg_app"], fg=C["text_3"],
                                font=self.f_sub, anchor="w")
        self.sub_lbl.pack(anchor="w")

    def set(self, state, title, sub=""):
        bg, accent, fg, glyph = self.COLORS.get(state, self.COLORS["info"])
        self.configure(bg=bg, highlightbackground=accent, highlightthickness=1)
        self.icon.configure(text=glyph, bg=bg, fg=accent)
        self.title_lbl.configure(text=title, bg=bg, fg=fg)
        self.sub_lbl.configure(text=sub, bg=bg)
        for w in self.winfo_children():
            w.configure(bg=bg)
        if not sub:
            self.sub_lbl.pack_forget()
        else:
            self.sub_lbl.pack(anchor="w")
        self.pack(fill="x", pady=(0, 0))

    def clear(self):
        self.pack_forget()


class Stepper(tk.Frame):
    """顶部步骤条：已完成 / 当前 / 未开始 三态 + 连接线。"""

    def __init__(self, master, titles):
        super().__init__(master, bg=C["bg_surface"])
        self.titles = titles
        self.current = 0
        self.dots = []
        self.lines = []
        self.f = tkfont.Font(self, family=FONT_UI_SM[0], size=FONT_UI_SM[1])
        for i, t in enumerate(titles):
            if i > 0:
                ln = tk.Frame(self, bg=C["border"], height=1, width=44)
                ln.pack(side="left", padx=(6, 6), pady=(0, 16))
                self.lines.append(ln)
            box = tk.Frame(self, bg=C["bg_surface"])
            box.pack(side="left")
            d = tk.Canvas(box, width=20, height=20, bg=C["bg_surface"],
                          highlightthickness=0)
            d.pack()
            tk.Label(box, text=t, bg=C["bg_surface"], fg=C["text_3"],
                     font=self.f).pack(pady=(4, 0))
            self.dots.append((d, box))
        self.set(0)

    def set(self, idx):
        self.current = idx
        for i, (d, box) in enumerate(self.dots):
            d.delete("all")
            lbl = box.winfo_children()[1]
            if i < idx:
                d.create_oval(0, 0, 20, 20, fill=C["ok_bg"], outline=C["ok"], width=1)
                d.create_line(5, 10, 9, 14, fill=C["ok"], width=2)
                d.create_line(9, 14, 15, 6, fill=C["ok"], width=2)
                lbl.configure(fg=C["ok"])
            elif i == idx:
                d.create_oval(0, 0, 20, 20, fill=C["ok"], outline=C["ok"], width=1)
                d.create_text(10, 10, text=str(i + 1), fill="#FFFFFF",
                              font=(FONT_UI[0], FONT_UI_SM[1]))
                lbl.configure(fg=C["text_1"])
            else:
                d.create_oval(0, 0, 20, 20, fill=C["bg_raised"],
                              outline=C["border"], width=1)
                d.create_text(10, 10, text=str(i + 1), fill=C["text_3"],
                              font=(FONT_UI[0], FONT_UI_SM[1]))
                lbl.configure(fg=C["text_3"])
        for i, ln in enumerate(self.lines):
            ln.configure(bg=C["ok"] if i < idx else C["border"])


# ── 步骤内容页 ────────────────────────────────────────────────

class StepPage(tk.Frame):
    """步骤页基类：标题 + 描述 + 内容区。"""

    def __init__(self, master, title, desc=""):
        super().__init__(master, bg=C["bg_app"])
        self.title_lbl = tk.Label(self, text=title, bg=C["bg_app"],
                                  fg=C["text_1"], font=FONT_H1, anchor="w")
        self.title_lbl.pack(anchor="w")
        self.desc_lbl = tk.Label(self, text=desc, bg=C["bg_app"],
                                 fg=C["text_2"], font=FONT_UI,
                                 anchor="w", justify="left", wraplength=560)
        self.desc_lbl.pack(anchor="w", pady=(6, 16))
        self.body = tk.Frame(self, bg=C["bg_app"])
        self.body.pack(fill="both", expand=True)

    def set_desc(self, text):
        self.desc_lbl.configure(text=text)


class WelcomeStep(StepPage):
    """第 1 步：欢迎使用 + 安装位置。"""

    def __init__(self, master, default_dir="", on_browse=None):
        super().__init__(master, "欢迎使用",
                         "这个程序会自动回复买家消息、付款后自动发卡密。\n"
                         "接下来需要填一些配置，全部在这个窗口里完成，不需要开网页。")
        self.f_dir = PathField(self.body, "安装位置", default_dir,
                               hint="占用空间约 220MB，装在当前用户目录，不需要管理员权限",
                               on_browse=on_browse)
        self.f_dir.pack(fill="x")

    def values(self):
        return dict(dir=self.f_dir.get())

    def validate(self):
        self.f_dir.clear_error()
        if not self.f_dir.get().strip():
            self.f_dir.set_error("请选择安装位置")
            return False, "请选择安装位置"
        return True, ""


class ClientStep(StepPage):
    """第 3 步：闲鱼客户端路径 + 控制台登录密码。"""

    def __init__(self, master, default_client="", on_browse=None):
        super().__init__(master, "闲鱼客户端与登录密码",
                         "本软件是操作闲鱼官方卖家客户端来工作的，所以要知道它装在哪。\n"
                         "还没装的请先装好（闲鱼官网下载），装完把路径填到下面。")
        self.f_client = PathField(
            self.body, "闲鱼客户端安装目录", default_client,
            hint="没自动找到。可以先留空，装完在控制台「设置」里再填。",
            on_browse=on_browse)
        self.f_client.pack(fill="x", pady=(0, 20))

        tk.Label(self.body, text="控制台登录密码（账号固定为 admin）",
                 bg=C["bg_app"], fg=C["text_3"], font=FONT_UI_SM,
                 anchor="w").pack(anchor="w", pady=(0, 4))
        row = tk.Frame(self.body, bg=C["bg_app"])
        row.pack(fill="x")
        self.f_pwd = Field(row, "密码", secret=True)
        self.f_pwd.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.f_pwd2 = Field(row, "确认", secret=True)
        self.f_pwd2.pack(side="left", fill="x", expand=True)
        # 提示文字放到整行下方，避免两个输入框因提示行错位而高矮不一
        self.f_pwd.hint_lbl.pack_forget()
        self.f_pwd2.hint_lbl.pack_forget()
        self.f_pwd2.error_lbl.pack_forget()
        tk.Label(self.body, text="至少 6 位，以后打开控制台要输",
                 bg=C["bg_app"], fg=C["text_3"], font=FONT_UI_SM,
                 anchor="w").pack(anchor="w", pady=(5, 0))

    def values(self):
        return dict(client=self.f_client.get(), password=self.f_pwd.get())

    def validate(self):
        self.f_client.clear_error()
        self.f_pwd.clear_error()
        self.f_pwd2.clear_error()
        pwd = self.f_pwd.get()
        if len(pwd) < 6:
            self.f_pwd.set_error("至少 6 位")
            return False, "控制台密码至少 6 位"
        if pwd != self.f_pwd2.get():
            self.f_pwd2.set_error("两次输入不一致")
            return False, "两次输入的密码不一致"
        return True, ""


class DbStep(StepPage):
    """第 2 步：数据库配置。"""

    def __init__(self, master, on_mode_change=None):
        super().__init__(master, "数据库配置",
                         "卡密、商品、规则、订单都存在 MySQL 里。没有 MySQL 请先装 "
                         "phpStudy 或 MySQL 官方版，装好后填这里。")
        self.on_mode_change = on_mode_change
        self.mode = "new"

        self.group = RadioGroup(self.body, on_change=self._on_mode)
        self.opt_new = self.group.add("新建数据库（自动建库建表）", "new")
        self.opt_exist = self.group.add("使用已有数据库", "exist")
        self.group.pack(fill="x", pady=(0, 16))

        grid = tk.Frame(self.body, bg=C["bg_app"])
        grid.pack(fill="x")
        for i in range(2):
            grid.columnconfigure(i, weight=1, uniform="db")

        self.f_host = Field(grid, "主机", "127.0.0.1")
        self.f_host.grid(row=0, column=0, sticky="ew", padx=(0, 8), pady=(0, 8))
        self.f_port = Field(grid, "端口", "3306")
        self.f_port.grid(row=0, column=1, sticky="ew", padx=(8, 0), pady=(0, 8))
        self.f_user = Field(grid, "账号", "root")
        self.f_user.grid(row=1, column=0, sticky="ew", padx=(0, 8), pady=(0, 8))
        self.f_pass = Field(grid, "密码", secret=True)
        self.f_pass.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(0, 8))
        self.f_db = Field(grid, "库名", "xianyu_autoreply")
        self.f_db.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 0))

        self.status = StatusBanner(self.body)
        row = tk.Frame(self.body, bg=C["bg_app"])
        row.pack(fill="x", pady=(12, 0))
        self.btn_test = RoundButton(row, "测试连接", "ghost", self._on_test)
        self.btn_test.pack(side="left")
        tk.Label(row, text="先测通再下一步，避免装完才发现连不上。",
                 bg=C["bg_app"], fg=C["text_3"], font=FONT_UI_SM,
                 anchor="w").pack(side="left", padx=(12, 0))
        self.group.select_value("new")

    def _on_mode(self, v):
        self.mode = v
        if self.on_mode_change:
            self.on_mode_change(v)

    def _on_test(self):
        if self.on_test_cb:
            self.on_test_cb()

    on_test_cb = None

    def values(self):
        return dict(host=self.f_host.get(), port=self.f_port.get(),
                    user=self.f_user.get(), password=self.f_pass.get(),
                    db=self.f_db.get(), mode=self.mode)

    def fill(self, **kw):
        for k, v in kw.items():
            f = getattr(self, "f_" + k, None)
            if f:
                f.set(v)

    def validate(self):
        """返回 (ok, msg)。"""
        v = self.values()
        for key, f in [("host", self.f_host), ("port", self.f_port),
                       ("user", self.f_user), ("db", self.f_db)]:
            f.clear_error()
            if not v[key].strip():
                f.set_error("不能为空")
                return False, "请填写" + f.winfo_children()[0].cget("text")
        if not v["port"].isdigit() or not (1 <= int(v["port"]) <= 65535):
            self.f_port.set_error("端口不合法")
            return False, "端口需为 1-65535 的数字"
        if self.mode == "new" and not v["db"].strip():
            self.f_db.set_error("库名不能为空")
            return False, "请填写库名"
        return True, ""


class DoneStep(StepPage):
    """第 3 步：安装完成。"""

    def __init__(self, master):
        super().__init__(master, "安装完成", "全部就绪，可以启动控制台了。")
        self.banner = StatusBanner(self.body)
        self.rows = {}

    def add_row(self, key, label, value=""):
        row = tk.Frame(self.body, bg=C["bg_app"])
        row.pack(fill="x", pady=5)
        tk.Label(row, text=label, bg=C["bg_app"], fg=C["text_3"],
                 font=FONT_UI, anchor="w", width=12).pack(side="left")
        val = tk.Label(row, text=value, bg=C["bg_app"], fg=C["text_1"],
                       font=FONT_UI, anchor="w")
        val.pack(side="left")
        self.rows[key] = val
        return val

    def set_value(self, key, text):
        if key in self.rows:
            self.rows[key].configure(text=text)


# ── 向导外壳 ──────────────────────────────────────────────────

class InstallerWizard(tk.Frame):
    """三步安装向导外壳：头部步骤条 + 内容区 + 底部导航。"""

    def __init__(self, master, titles=("欢迎使用", "数据库配置", "客户端与密码")):
        super().__init__(master, bg=C["bg_app"])
        self.master = master
        self.titles = list(titles)
        self.index = 0
        self.on_next = None
        self.on_back = None
        self.on_finish = None
        self.pages = []
        self._build()

    def _build(self):
        master = self.master
        master.configure(bg=C["bg_app"])
        master.minsize(660, 560)
        self.pack(fill="both", expand=True)

        # 头部
        head = tk.Frame(self, bg=C["bg_surface"])
        head.pack(fill="x")
        inner = tk.Frame(head, bg=C["bg_surface"])
        inner.pack(fill="x", padx=24, pady=14)

        logo = tk.Canvas(inner, width=34, height=34, bg=C["bg_surface"],
                         highlightthickness=0)
        logo.create_rectangle(0, 0, 34, 34, fill="#0A4638",
                              outline=C["ok"], width=1)
        logo.create_polygon(11, 14, 17, 19, 23, 14, fill="",
                            outline="#9FE1CB", width=2)
        logo.pack(side="left", padx=(0, 12))
        txt = tk.Frame(inner, bg=C["bg_surface"])
        txt.pack(side="left")
        tk.Label(txt, text="安装向导", bg=C["bg_surface"], fg=C["text_1"],
                 font=("Microsoft YaHei UI", 10, "bold"),
                 anchor="w").pack(anchor="w")
        tk.Label(txt, text="闲鱼客服控制台 v1.0.0", bg=C["bg_surface"],
                 fg=C["text_3"], font=FONT_UI_SM, anchor="w").pack(anchor="w")

        self.stepper = Stepper(inner, self.titles)
        self.stepper.pack(side="right")

        tk.Frame(self, bg=C["border"], height=1).pack(fill="x")

        # 内容区（可滚动，防止内容过高把底部导航挤出窗口）
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
        self.vbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=C["bg_app"])
        self._win = self.canvas.create_window((0, 0), window=self.inner,
                                              anchor="nw")
        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)
        for w in (self.canvas, self.inner):
            w.bind("<MouseWheel>", self._on_wheel)
        for i in range(3):
            page = tk.Frame(self.inner, bg=C["bg_app"])
            page.pack_forget()
            self.pages.append(page)

        # 底部
        tk.Frame(self, bg=C["border"], height=1).pack(fill="x")
        foot = tk.Frame(self, bg=C["bg_surface"])
        foot.pack(fill="x")
        fin = tk.Frame(foot, bg=C["bg_surface"])
        fin.pack(fill="x", padx=24, pady=12)
        self.counter = tk.Label(fin, text="", bg=C["bg_surface"],
                                fg=C["text_3"], font=FONT_UI_SM, anchor="w")
        self.counter.pack(side="left")
        # side="right" 逆序打包：先 pack 的在更右侧。
        # 约定：主操作「下一步/完成」在最右，「上一步」在其左侧。
        self.btn_next = RoundButton(fin, "下一步", "primary", self.next_step)
        self.btn_next.pack(side="right")
        self.btn_back = RoundButton(fin, "上一步", "ghost", self.back)
        self.btn_back.pack(side="right", padx=(0, 8))
        self.btn_exit = RoundButton(fin, "退出", "ghost", self.exit_wizard)
        self.btn_exit.pack(side="right", padx=(0, 8))

    def _on_inner(self, _=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self._sync_scrollbar()

    def _on_canvas(self, e):
        self.canvas.itemconfigure(self._win, width=e.width)
        self._sync_scrollbar()

    def _sync_scrollbar(self):
        bbox = self.canvas.bbox("all")
        need = bbox and bbox[3] > self.canvas.winfo_height() + 2
        if need and not self.vbar.winfo_ismapped():
            self.vbar.pack(side="right", fill="y", before=self.canvas)
        elif not need and self.vbar.winfo_ismapped():
            self.vbar.pack_forget()
        self.canvas.yview_moveto(0)

    def _on_wheel(self, e):
        self.canvas.yview_scroll(-1 * (e.delta // 120), "units")

    # 步骤管理
    def add_page(self, page, index=None):
        """把 StepPage 挂到某一页容器上，返回该页对象。"""
        if index is None:
            index = len(self.pages)
        holder = self.pages[index]
        page.pack(fill="both", expand=True, padx=24, pady=20)
        return page

    def goto(self, index):
        if not (0 <= index < len(self.pages)):
            return
        for i, p in enumerate(self.pages):
            if i == index:
                p.pack(fill="both", expand=True)
            else:
                p.pack_forget()
        self.index = index
        self.stepper.set(index)
        self.counter.configure(text="第 %d 步，共 %d 步" % (index + 1, len(self.pages)))
        self.btn_back.set_state(index > 0)
        # 「退出」只在第一步出现（Windows 安装向导惯例）
        if index == 0:
            self.btn_exit.pack(side="right", padx=(0, 8), before=self.btn_back)
        else:
            self.btn_exit.pack_forget()
        self.btn_next.set_state(True)
        if index == len(self.pages) - 1:
            self.btn_next.configure_variant("primary", "开始安装")
        else:
            self.btn_next.configure_variant("primary", "下一步")

    def exit_wizard(self):
        if self.on_exit:
            self.on_exit()
        else:
            self.master.destroy()

    def set_next_enabled(self, enabled):
        self.btn_next.set_state(enabled)

    def set_next_text(self, text):
        self.btn_next.set_text(text)

    # 导航
    def next_step(self):
        if self.index == len(self.pages) - 1:
            if self.on_finish:
                self.on_finish()
            return
        if self.on_next:
            if self.on_next(self.index) is False:
                return
        self.goto(self.index + 1)

    def back(self):
        if self.index == 0:
            return
        if self.on_back:
            self.on_back(self.index)
        self.goto(self.index - 1)


if __name__ == "__main__":
    root = tk.Tk()
    root.title("闲鱼客服控制台 安装向导")
    root.geometry("720x640")
    apply_window_icon(root)

    wiz = InstallerWizard(root)

    env = EnvStep(wiz.pages[0])
    wiz.add_page(env, 0)

    db = DbStep(wiz.pages[1])
    wiz.add_page(db, 1)

    done = DoneStep(wiz.pages[2])
    wiz.add_page(done, 2)
    done.add_row("dir", "安装目录", r"D:\xianyu-seller-im")
    done.add_row("db", "数据库", "xianyu_autoreply")
    done.add_row("web", "网站地址", "http://127.0.0.1:8082")

    env.set_result("php", True, "PHP 8.2.12 · 已就绪")
    env.set_result("mysql", True, "MySQL 5.7.26 · 127.0.0.1:3306")
    env.set_result("port", True, "8082 未被占用")
    env.set_result("dir", True, r"D:\xianyu-seller-im · 可写")

    def on_test():
        db.status.set("ok", "连接成功", "MySQL 5.7.26 · 库 xianyu_autoreply 已存在（15 张表）")
    db.on_test_cb = on_test

    def on_next(step):
        if step == 1:
            ok, msg = db.validate()
            if not ok:
                db.status.set("err", "配置有误", msg)
                return False
            db.status.set("ok", "连接成功", "MySQL 5.7.26")
        return True
    wiz.on_next = on_next

    wiz.goto(1)
    root.mainloop()
