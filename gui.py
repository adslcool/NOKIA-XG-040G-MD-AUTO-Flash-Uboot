#!/usr/bin/env python3
"""XG-040G-MD 光猫工具 · 图形界面

只用 Python 标准库 tkinter，不需要额外安装依赖（Windows 版 exe 也是这套）。

界面分区：
  1. 账号密码设置（存 config.ini，可保存/重载）
  2. 功能按钮：开 Telnet / 开 FTP / 备份 / 重启
  3. 刷机（默认锁定，需先勾选"启用刷机功能"解锁；点击后弹出多重确认）
  4. 运行日志
"""
import os
import queue
import sys
import threading
import tkinter as tk
import webbrowser
from tkinter import ttk, messagebox, scrolledtext

import config as cfgmod
import models as models_mod
import serial_shell

try:
    from version import VERSION
except Exception:  # 打包漏了 version.py 时不能让 GUI 起不来
    VERSION = "0.0.0"

APP_TITLE = "XG-040G-MD 光猫工具"
APP_TITLE_FULL = f"{APP_TITLE} v{VERSION}"


def _resource_dir():
    """可写的工作目录：exe 所在目录（或源码目录）。

    config.ini、backup_* 都放这里，因为打包后 _MEIPASS 是只读临时目录。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _bundle_dir():
    """PyInstaller 解包出的只读资源目录；非打包时即源码目录。"""
    return getattr(sys, "_MEIPASS", _resource_dir())


def find_resource(name):
    """按「工作目录 → 打包内资源」的顺序找文件。

    先看 exe 同目录，这样用户可以直接替换 flash.py 或换一个 tcboot.bin，
    不用重新打包；找不到再退回 exe 内自带的副本。
    """
    for d in (_resource_dir(), _bundle_dir()):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    return None


class LogRedirector:
    """把 print 输出接进 GUI 日志框。

    flash.py 全程用 print 汇报进度，GUI 不去改动它，只在运行时把 stdout
    换成本对象，这样命令行和 GUI 共用同一份实现。
    """

    def __init__(self, q):
        self.q = q

    def write(self, text):
        if text:
            self.q.put(text)

    def flush(self):
        pass


def _set_window_icon(root):
    """给窗口设置标题栏/任务栏图标。

    优先用 icon.png（tkinter 的 iconphoto 支持 PNG，跨平台且不需要 Pillow）；
    Windows 下再补一次 iconbitmap(icon.ico)，因为部分环境下任务栏只认 .ico。
    两者都找不到就静默跳过 —— 图标只是外观，不能因此让程序起不来。
    """
    png = find_resource("icon.png")
    if png:
        try:
            # 用 PhotoImage 保持引用：不保存会被 GC 掉，图标随即消失
            root._icon_img = tk.PhotoImage(file=png)
            root.iconphoto(True, root._icon_img)
        except Exception:
            pass
    ico = find_resource("icon.ico")
    if ico:
        try:
            root.iconbitmap(ico)
        except Exception:
            pass


class App:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_TITLE_FULL)
        self.root.geometry("1040x900")
        self.root.minsize(960, 640)
        _set_window_icon(self.root)

        self.log_queue = queue.Queue()
        self.worker = None
        self.flash = None          # 延迟导入 flash，避免启动时就做网络探测

        self.cfg = cfgmod.load(os.path.join(_resource_dir(), cfgmod.CONFIG_FILE))
        self.vars = {}
        self.hint_labels = {}
        self.model = models_mod.get(self.cfg.get("device", "model"))
        self._build_ui()
        self._load_into_ui()
        self._pump_log()

    # ---------- 界面 ----------
    def _build_ui(self):
        # 内容比屏幕高（加了 TTL 区之后约 1170px），所以整块放进可滚动的
        # Canvas：小屏笔记本（1366x768）也能滚到最底下的日志框。
        # 日志框本身也是可滚动的，滚轮事件要让它优先，否则会一起滚。
        container = ttk.Frame(self.root)
        container.pack(fill="both", expand=True)
        canvas = tk.Canvas(container, highlightthickness=0, borderwidth=0)
        vbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vbar.set)
        vbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        outer = ttk.Frame(canvas, padding=10)
        _win = canvas.create_window((0, 0), window=outer, anchor="nw")

        def _on_inner_configure(_e=None):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _on_canvas_configure(e):
            # 让内容宽度跟着窗口走，否则文字换行按最初宽度算，拉大窗口不重排
            canvas.itemconfigure(_win, width=e.width)

        outer.bind("<Configure>", _on_inner_configure)
        canvas.bind("<Configure>", _on_canvas_configure)

        def _on_wheel(e):
            # 鼠标停在日志框上时交给日志框自己滚，不然两个一起动很难用
            if isinstance(e.widget, (tk.Text, tk.Listbox)):
                return
            if getattr(e, "num", None) == 4:
                step = -1
            elif getattr(e, "num", None) == 5:
                step = 1
            else:
                step = -1 if getattr(e, "delta", 0) > 0 else 1
            canvas.yview_scroll(step, "units")

        # Windows/macOS 用 <MouseWheel>，X11 用 Button-4/5，三个都绑
        for _seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            canvas.bind_all(_seq, _on_wheel)

        self._canvas = canvas

        # ===== 机型选择 =====
        # 放在最上面：机型决定了地址、端口、默认账号密码、可用功能，
        # 后面的表单和按钮都跟着它变，所以必须先选。
        modelbox = ttk.LabelFrame(outer, text="机型", padding=8)
        modelbox.pack(fill="x", pady=(0, 8))

        self.model_var = tk.StringVar()
        mrow = ttk.Frame(modelbox)
        mrow.pack(fill="x")
        ttk.Label(mrow, text="选择机型：").pack(side="left")
        self.model_combo = ttk.Combobox(
            mrow, textvariable=self.model_var, state="readonly", width=38,
            values=[m.name for m in models_mod.all_models()])
        self.model_combo.pack(side="left")
        self.model_combo.bind("<<ComboboxSelected>>", self._on_model_change)
        ttk.Button(mrow, text="按此机型填默认值", command=self.on_fill_model_defaults).pack(
            side="left", padx=(10, 0))
        self.model_note = ttk.Label(modelbox, text="", foreground="#666",
                                    wraplength=900, justify="left")
        self.model_note.pack(anchor="w", pady=(6, 0))

        # ===== 顶部提示：恢复出厂设置 =====
        # 这是使用前提的说明，不跟刷机按钮绑定（用户明确要求）。
        # 文案随机型变化：两个厂商的超级密码不一样。
        tip = tk.Frame(outer, bg="#fff8e1", highlightbackground="#e0c060",
                       highlightthickness=1)
        tip.pack(fill="x", pady=(0, 8))
        self.tip_label = tk.Label(
            tip, bg="#fff8e1", fg="#8a6d00", justify="left", anchor="w",
            wraplength=820, text="")
        self.tip_label.pack(fill="x", padx=8, pady=6)

        # ===== 账号密码 =====
        acct = ttk.LabelFrame(outer, text="账号密码（保存在 config.ini）", padding=8)
        acct.pack(fill="x")

        rows = [
            ("device", "router_ip", "光猫地址", False,
             "光猫的管理地址，默认 192.168.1.1"),
            ("device", "mac", "光猫 MAC", False,
             "烽火机型必填：telnet / su 密码由 MAC 后 6 位推导（见机型说明）"),
            ("telnet", "username", "铭牌用户账号", False,
             "光猫底部铭牌上的用户账号（不同型号不同）"),
            ("telnet", "password", "铭牌用户密码", False,
             "光猫底部铭牌上的用户密码；telnet 登录用它"),
            ("telnet", "su_user", "提权账号（su）", False,
             "自动填成「铭牌用户账号_ftp」，即拿到 root 的账号"),
            ("telnet", "root_password", "提权密码（留空=同上）", False,
             "留空表示与「铭牌用户密码」相同"),
            ("super", "username", "网页超级账号", False,
             "登录管理网页用的超级账号"),
            ("super", "password", "网页超级密码", False,
             "恢复出厂后的默认超级密码，用于自动开启 Telnet / FTP"),
        ]
        for r, (section, key, label, secret, hint) in enumerate(rows):
            ttk.Label(acct, text=label + "：").grid(
                row=r, column=0, sticky="e", padx=(0, 6), pady=3)
            var = tk.StringVar()
            self.vars[(section, key)] = var
            ent = ttk.Entry(acct, textvariable=var, width=38,
                            show="*" if secret else "")
            ent.grid(row=r, column=1, sticky="we", pady=3)
            hint_lbl = ttk.Label(acct, text=hint, foreground="#777")
            hint_lbl.grid(row=r, column=2, sticky="w", padx=(8, 0), pady=3)
            self.hint_labels[(section, key)] = hint_lbl
        acct.columnconfigure(1, weight=1)

        btns = ttk.Frame(acct)
        btns.grid(row=len(rows), column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Button(btns, text="保存配置", command=self.on_save).pack(side="left")
        ttk.Button(btns, text="重新载入", command=self.on_reload).pack(
            side="left", padx=6)
        ttk.Button(btns, text="恢复默认", command=self.on_reset).pack(side="left")

        # 铭牌用户账号一变，提权账号自动跟着变成「账号_ftp」。
        # 只在用户没手工改过提权账号时才覆盖（_su_auto 记录上次自动填的值），
        # 免得把用户特意填的账号冲掉。
        self._su_auto = None
        self.vars[("telnet", "username")].trace_add("write", self._on_username_change)
        self.vars[("telnet", "su_user")].trace_add("write", self._on_su_user_change)

        # ===== 功能 =====
        ops = ttk.LabelFrame(outer, text="操作（默认只读，不写入任何分区）", padding=8)
        ops.pack(fill="x", pady=(8, 0))

        row1 = ttk.Frame(ops)
        row1.pack(fill="x")
        self.btn_telnet = ttk.Button(
            row1, text="① 开启 Telnet", width=16,
            command=lambda: self.run_mode("telnet"))
        self.btn_telnet.pack(side="left")
        # telnet 和 FTP 是两个独立开关，所以给两个按钮：
        # 用户自己开过 telnet、没开 FTP 时，只点这个即可。
        self.btn_ftp = ttk.Button(
            row1, text="② 开启 FTP", width=14,
            command=lambda: self.run_mode("ftp"))
        self.btn_ftp.pack(side="left", padx=6)
        self.btn_backup = ttk.Button(
            row1, text="③ 备份全部分区", width=16,
            command=lambda: self.run_mode("backup"))
        self.btn_backup.pack(side="left")
        self.btn_reboot = ttk.Button(
            row1, text="重启光猫", width=12, command=self.on_reboot)
        self.btn_reboot.pack(side="left", padx=6)

        # 第二行：进阶功能（换 U-Boot / 安装第三方固件）
        row2 = ttk.Frame(ops)
        row2.pack(fill="x", pady=(8, 0))
        self.btn_stock2ubi = ttk.Button(
            row2, text="④ 运行 stock2ubi 刷 U-Boot", width=26,
            command=self.on_stock2ubi)
        self.btn_stock2ubi.pack(side="left")
        ttk.Label(row2, text="文件：").pack(side="left", padx=(10, 0))
        self.stock2ubi_var = tk.StringVar(value="an758x-stock2ubi")
        ttk.Entry(row2, textvariable=self.stock2ubi_var, width=22).pack(side="left")
        ttk.Label(row2, text="（放在程序同目录）", foreground="#666").pack(
            side="left", padx=(4, 0))

        ops_hint = ttk.Label(ops, text="", foreground="#666")
        ops_hint.pack(anchor="w", pady=(6, 0))
        self.hint_labels["ops_hint"] = ops_hint

        # 相关项目链接（只放 an758x-stock2ubi —— U-Boot 本体与 webuboot 的
        # 链接按用户要求只加在 README 和恩山帖里，不加在 GUI 上）
        linkrow = ttk.Frame(ops)
        linkrow.pack(anchor="w", pady=(4, 0))
        ttk.Label(linkrow, text="相关项目：", foreground="#666").pack(side="left")
        self._link_label(
            linkrow,
            "an758x-stock2ubi（在光猫上刷 U-Boot 的网页工具）",
            "https://github.com/pbs05/an758x-stock2ubi"
        ).pack(side="left")

        # ===== TTL 串口（拆机接串口线，不走 telnet）=====
        ttlbox = ttk.LabelFrame(
            outer, text="TTL 串口（拆机接串口线，免 Telnet / 免超级密码）", padding=8)
        ttlbox.pack(fill="x", pady=(8, 0))

        trow = ttk.Frame(ttlbox)
        trow.pack(fill="x")
        self.serial_on = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            trow, text="走 TTL 串口", variable=self.serial_on,
            command=self._on_serial_toggle).pack(side="left")
        ttk.Label(trow, text="端口：").pack(side="left", padx=(10, 0))
        self.serial_port = tk.StringVar(value="")
        self.serial_combo = ttk.Combobox(
            trow, textvariable=self.serial_port, state="readonly", width=28)
        self.serial_combo.pack(side="left")
        ttk.Button(trow, text="刷新", width=6,
                   command=self.on_refresh_ports).pack(side="left", padx=(4, 0))
        ttk.Label(trow, text="波特率：").pack(side="left", padx=(10, 0))
        self.serial_baud = tk.StringVar(value="115200")
        ttk.Combobox(trow, textvariable=self.serial_baud, state="readonly",
                     width=8, values=list(serial_shell.BAUD_RATES)
                     ).pack(side="left")

        self.serial_hint = ttk.Label(ttlbox, text="", foreground="#666",
                                     wraplength=940, justify="left")
        self.serial_hint.pack(anchor="w", pady=(4, 0))

        # ===== 刷机 =====
        flashbox = ttk.LabelFrame(outer, text="刷机（危险）", padding=8)
        flashbox.pack(fill="x", pady=(8, 0))

        self.allow_flash = tk.BooleanVar(value=False)
        self.flash_check = ttk.Checkbutton(
            flashbox, text="我了解风险，启用刷机功能",
            variable=self.allow_flash, command=self.on_toggle_flash)
        self.flash_check.pack(anchor="w")

        fr = ttk.Frame(flashbox)
        fr.pack(fill="x", pady=(6, 0))
        ttk.Label(fr, text="U-Boot 文件：").pack(side="left")
        self.uboot_var = tk.StringVar(value="tcboot.bin")
        self.uboot_entry = ttk.Entry(fr, textvariable=self.uboot_var, width=30)
        self.uboot_entry.pack(side="left")
        ttk.Label(fr, text="（放在程序同目录）", foreground="#666").pack(
            side="left", padx=(4, 0))

        self.btn_flash = ttk.Button(
            flashbox, text="③ 刷写 U-Boot", width=18, command=self.on_flash,
            state="disabled")
        self.btn_flash.pack(anchor="w", pady=(8, 0))

        # telnet 登录失败后自动重启：认证失败有阈值（贝尔实测 3 次锁 300 秒），
        # 触发后只能等或重启。重启 1~3 分钟，比干等 5 分钟更可控，所以默认开。
        self.auto_reboot = tk.BooleanVar(value=True)
        self.auto_reboot_check = ttk.Checkbutton(
            flashbox,
            text="telnet 登录失败时自动重启光猫解除锁定（推荐）",
            variable=self.auto_reboot, command=self.on_toggle_auto_reboot)
        self.auto_reboot_check.pack(anchor="w", pady=(8, 0))

        self.flash_note = ttk.Label(flashbox, text="", foreground="#a00")
        self.flash_note.pack(anchor="w", pady=(6, 0))


        # ===== 日志 =====
        logbox = ttk.LabelFrame(outer, text="运行日志", padding=6)
        logbox.pack(fill="both", expand=True, pady=(8, 0))
        self.log = scrolledtext.ScrolledText(
            logbox, height=9, wrap="word", state="disabled",
            font=("Consolas", 9))
        self.log.pack(fill="both", expand=True)

    # ---------- 可点击链接 ----------
    def _link_label(self, parent, text, url):
        """做一个蓝色下划线、可点击打开浏览器的链接标签。

        tkinter 没有原生超链接控件，用 Label + 手型光标 + 点击回调模拟。
        """
        lbl = tk.Label(parent, text=text, fg="#0a66c2", cursor="hand2",
                       font=("", 9, "underline"), anchor="w", justify="left")
        lbl.bind("<Button-1>", lambda _e, u=url: self._open_url(u))
        return lbl

    def _open_url(self, url):
        try:
            webbrowser.open(url)
        except Exception as e:  # noqa: BLE001
            messagebox.showinfo(APP_TITLE, f"请手动打开：\n{url}\n\n（{e}）")

    # ---------- 机型 ----------
    def _on_model_change(self, *_):
        """下拉框选中机型后：更新提示文案、字段提示、按钮可用性。

        注意这里**不**动用户已经填好的账号密码 —— 用户可能手工改过。
        要填回机型默认值，点「按此机型填默认值」那个按钮，走显式确认。
        例外是提权账号：它是自动推导字段（贝尔「账号_ftp」/ 烽火固定 root），
        留着上一个机型的目标只会 su 到不存在的账号，所以按新机型重置。
        """
        name = self.model_var.get()
        old = self.model
        for m in models_mod.all_models():
            if m.name == name:
                self.model = m
                break
        if old.key != self.model.key:
            self._adopt_model_defaults(old)
        self.cfg.set("device", "model", self.model.key)
        self._apply_model_to_ui()

    def _model_field_values(self, m):
        """某机型下各「机型身份字段」应有的值。

        这些字段不是用户自由填写的，而是机型决定的（账号名、默认密码、
        管理地址、提权目标）。切换机型时必须跟着换，否则会拿贝尔的
        CMCCAdmin 去登烽火、或者 su 到一个不存在的账号。
        """
        mac = self._model_mac()
        return {
            ("device", "router_ip"): m.router_ip,
            ("telnet", "username"): m.telnet_user,
            ("telnet", "password"):
                models_mod.derive_telnet_password(m, mac) or m.telnet_password,
            ("telnet", "su_user"):
                (f"{m.telnet_user}_ftp" if m.su_user_follows_telnet
                 else m.su_user),
            ("telnet", "root_password"): models_mod.derive_su_password(m, mac),
            ("super", "username"): m.super_user,
            ("super", "password"): m.super_password,
        }

    def _adopt_model_defaults(self, old):
        """切换机型：只替换「还是上一个机型默认值」的字段。

        判据是「当前值 == 旧机型该字段的值」或「为空」。用户手工改过的
        字段（值不等于旧默认值）一律保持不动，不会被切换冲掉。
        这正是 su 自动跟随那套逻辑的推广版。
        """
        old_vals = self._model_field_values(old)
        new_vals = self._model_field_values(self.model)
        for key, old_val in old_vals.items():
            var = self.vars.get(key)
            if var is None:
                continue
            cur = var.get()
            if cur == old_val or cur == "":
                new_val = new_vals.get(key, "")
                if new_val or cur == old_val:
                    var.set(new_val)
        # su 自动跟随状态重新同步（_adopt 已经把值改好了）
        uname = self.vars[("telnet", "username")].get().strip()
        su = self.vars[("telnet", "su_user")].get().strip()
        self._su_auto = f"{uname}_ftp" if su == f"{uname}_ftp" else None

    def _apply_model_to_ui(self):
        """把当前机型的特征反映到界面上（提示、字段提示、功能开关）。"""
        m = self.model

        # 顶部出厂提示：两个厂商的超级密码不同，必须跟着换
        if m.super_user and m.super_password:
            # 只有支持网页开 telnet 的机型才说「程序会自动开启」
            if m.supports_web_telnet:
                tail = ("程序才能自动登录网页开启 Telnet"
                        + ("与 FTP。" if m.need_ftp_for_su else "。"))
            else:
                tail = ("程序才能自动登录网页读取设备信息、执行备份；"
                        "本机型需你自行开启 Telnet。")
            self.tip_label.configure(
                text="提示：请先恢复光猫出厂设置\n"
                     f"按住光猫 reset 键 20 秒以上，让超级管理员密码回到默认值"
                     f"（{m.super_user} / {m.super_password}），{tail}")
        else:
            self.tip_label.configure(text="提示：请先恢复光猫出厂设置。")

        # 操作区提示也跟着机型变：别说「会自动开 Telnet 与 FTP」然后其实没开
        if "ops_hint" in self.hint_labels:
            if m.supports_web_telnet and m.need_ftp_for_su:
                t = ("提示：备份需要 root，程序会自动开启 Telnet 与 FTP；"
                     "若你已手动开过其中一个，点对应的单个按钮即可。")
            elif m.supports_web_telnet:
                t = "提示：备份需要 root，程序会自动开启 Telnet。"
            elif m.need_ftp_for_su:
                t = "提示：备份需要 root，程序会自动开启 FTP。"
            else:
                t = ("提示：本机型需要你先手动开启 Telnet，"
                     "程序才能登录并提权做备份（提权用 su root）。")
            self.hint_labels["ops_hint"].configure(text=t)

        # 机型说明（note）
        extra = ""
        if models_mod.mac_required(m):
            extra = ("\n⚠ 本机型需要填「光猫 MAC」：telnet 密码与提权密码都由 "
                     "MAC 后 6 位推导，不填无法登录。")
        self.model_note.configure(text=(m.note or "") + extra)

        # 字段提示：su 账号是否跟随 telnet 账号、MAC 是否必填
        if ("telnet", "su_user") in self.hint_labels:
            self.hint_labels[("telnet", "su_user")].configure(
                text=("自动填成「铭牌用户账号_ftp」，即拿到 root 的账号"
                      if m.su_user_follows_telnet
                      else f"本机型固定为 {m.su_user}（不跟随铭牌账号）"))
        if ("device", "mac") in self.hint_labels:
            self.hint_labels[("device", "mac")].configure(
                text=("本机型必填：telnet / su 密码由 MAC 后 6 位推导"
                      if models_mod.mac_required(m)
                      else "可选；本机型不需要"))
        if ("super", "password") in self.hint_labels:
            if m.supports_web_telnet:
                t = ("恢复出厂后的默认超级密码，用于自动开启 Telnet"
                     + (" / FTP" if m.need_ftp_for_su else ""))
            else:
                t = "恢复出厂后的默认超级密码（本机型用于登录网页）"
            self.hint_labels[("super", "password")].configure(text=t)

        # 刷机区：本机型不支持刷 tcboot 时把控件灰掉，并说明该用哪条路
        supported = m.supports_uboot_flash
        if getattr(self, "uboot_entry", None) is not None:
            self.uboot_entry.configure(state="normal" if supported else "disabled")
        if getattr(self, "flash_check", None) is not None:
            self.flash_check.configure(state="normal" if supported else "disabled")
        if getattr(self, "flash_note", None) is not None:
            if supported:
                self.flash_note.configure(
                    text="刷写前请确认已按上方提示恢复出厂设置；点击后会弹出确认框。",
                    foreground="#a00")
            else:
                self.flash_note.configure(
                    text=f"本机型不走 tcboot：请用上面的「④ 运行 stock2ubi 刷 U-Boot」，"
                         f"在它自己的网页里刷。",
                    foreground="#666")

        # 功能开关：该机型不支持的功能直接禁用，别让用户点了才发现不行
        # 走 TTL 串口时，开 Telnet / 开 FTP 这两步本来就要跳过，也一并灰掉。
        on_serial = bool(getattr(self, "serial_on", None) and self.serial_on.get())
        self.btn_telnet.configure(
            state="normal" if (m.supports_web_telnet and not on_serial)
            else "disabled")
        self.btn_ftp.configure(
            state="normal" if (m.need_ftp_for_su and not on_serial)
            else "disabled")
        self.btn_stock2ubi.configure(
            state="normal" if m.supports_stock2ubi else "disabled")
        # 刷写 tcboot 只对贝尔有意义（烽火没有 tcboot 分区表那套）
        self._flash_supported = m.supports_uboot_flash
        self.on_toggle_flash()

    def on_fill_model_defaults(self):
        """按当前机型填默认账号密码（会覆盖表单里的值，先确认）。"""
        m = self.model
        if not messagebox.askyesno(
                APP_TITLE,
                f"用「{m.name}」的默认值覆盖当前表单？\n\n"
                "地址、账号、密码都会被替换（不影响已保存的 config.ini，"
                "要保存请点「保存配置」）。"):
            return
        d = cfgmod.model_defaults(m)
        for section, kv in d.items():
            for key, val in kv.items():
                var = self.vars.get((section, key))
                if var is not None:
                    var.set(val)
        # MAC 不能猜，但保留用户已填的值
        self.log_line(f"[*] 已填入机型默认值：{m.name}\n")
        self.log_line(f"    如需 MAC 推导密码，请填好「光猫 MAC」后点「保存配置」。\n")

    def _model_mac(self):
        return self.vars[("device", "mac")].get().strip()

    def _derive_passwords_if_needed(self):
        """烽火机型：按 MAC 推导 telnet 密码与 su 密码，写回表单。

        只在对应字段**为空**时才填，避免覆盖用户手填的值。
        返回 (ok, 错误说明)。
        """
        m = self.model
        # 走 TTL 串口时既不用 telnet 也不用超级密码，MAC 推导完全没必要，
        # 不能因为没填 MAC 就把用户拦在门外（串口本身已经是 root）。
        if getattr(self, "serial_on", None) is not None and self.serial_on.get():
            return True, ""
        if not models_mod.mac_required(m):
            return True, ""
        mac = self._model_mac()
        if not models_mod.normalize_mac(mac):
            return False, ("本机型需要「光猫 MAC」（背面标签上，"
                           "例如 70:D5:1E:04:6C:EC），否则推导不出 telnet 密码。")
        if m.telnet_password_from_mac:
            var = self.vars[("telnet", "password")]
            if not var.get().strip():
                var.set(models_mod.derive_telnet_password(m, mac))
        if m.su_password_from_mac:
            var = self.vars[("telnet", "root_password")]
            if not var.get().strip():
                var.set(models_mod.derive_su_password(m, mac))
        return True, ""

    # ---------- 提权账号自动跟随铭牌账号 ----------
    def _on_username_change(self, *_):
        """铭牌用户账号变化 → 提权账号自动设为「账号_ftp」。

        仅当提权账号当前值等于上次自动填入的值（或为空）时才覆盖，
        这样用户手工改过的提权账号不会被冲掉。
        烽火机型 su 目标固定是 root，不跟随（见 models.Model.su_user_follows_telnet）。
        """
        if not self.model.su_user_follows_telnet:
            return
        uname = self.vars[("telnet", "username")].get().strip()
        cur = self.vars[("telnet", "su_user")].get().strip()
        if cur in ("", self._su_auto):
            self._su_auto = f"{uname}_ftp" if uname else ""
            self.vars[("telnet", "su_user")].set(self._su_auto)

    def _on_su_user_change(self, *_):
        """用户手工改了提权账号 → 记住它，之后不再自动覆盖。"""
        cur = self.vars[("telnet", "su_user")].get().strip()
        if cur != self._su_auto:
            self._su_auto = None

    # ---------- 配置读写 ----------
    def _load_into_ui(self):
        for (section, key), var in self.vars.items():
            var.set(self.cfg.get(section, key))
        # 载入后同步自动推导状态：若配置里的提权账号正好等于
        # 「铭牌账号_ftp」，就认作自动值，之后账号一改它还会跟着走。
        uname = self.vars[("telnet", "username")].get().strip()
        su = self.vars[("telnet", "su_user")].get().strip()
        self._su_auto = f"{uname}_ftp" if su == f"{uname}_ftp" else None
        # 机型下拉框与提示跟着配置走
        self.model = models_mod.get(self.cfg.get("device", "model"))
        self.model_var.set(self.model.name)
        self.auto_reboot.set(
            cfgmod.get_bool(self.cfg, "safety", "auto_reboot_on_login_fail", True))
        # TTL 串口设置
        self.serial_on.set(cfgmod.get_bool(self.cfg, "serial", "enabled", False))
        self.serial_baud.set(self.cfg.get("serial", "baudrate") or "115200")
        # 先刷新端口列表（否则下拉框是空的），再把保存的端口填回去 ——
        # 刷新时若只有一个串口会自动选中，不能用它覆盖用户上次的选择。
        self.on_refresh_ports()
        saved_port = self.cfg.get("serial", "port").strip()
        if saved_port:
            self.serial_port.set(saved_port)
        self._apply_model_to_ui()

    def _save_from_ui(self):
        for (section, key), var in self.vars.items():
            self.cfg.set(section, key, var.get())
        self.cfg.set("safety", "auto_reboot_on_login_fail",
                     "true" if self.auto_reboot.get() else "false")
        self.cfg.set("serial", "enabled",
                     "true" if self.serial_on.get() else "false")
        self.cfg.set("serial", "port", self.serial_port.get().strip())
        self.cfg.set("serial", "baudrate", self.serial_baud.get() or "115200")
        cfgmod.save(self.cfg, os.path.join(_resource_dir(), cfgmod.CONFIG_FILE))

    def on_toggle_auto_reboot(self):
        """自动重启勾选框：立刻写进配置，避免用户忘了点「保存配置」。"""
        self.cfg.set("safety", "auto_reboot_on_login_fail",
                     "true" if self.auto_reboot.get() else "false")
        try:
            cfgmod.save(self.cfg, os.path.join(_resource_dir(), cfgmod.CONFIG_FILE))
        except Exception:
            pass
        if self.auto_reboot.get():
            self.log_line("[*] 已开启：telnet 登录失败时自动重启光猫解除锁定\n")
        else:
            self.log_line("[!] 已关闭自动重启：登录失败后程序会直接退出，"
                          "需要你手动重启光猫\n")

    def on_save(self):
        try:
            self._save_from_ui()
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"保存失败：{e}")
            return
        self.log_line(f"[✓] 配置已保存到 {cfgmod.CONFIG_FILE}\n")

    def on_reload(self):
        self.cfg = cfgmod.load(os.path.join(_resource_dir(), cfgmod.CONFIG_FILE))
        self._load_into_ui()
        self.log_line("[*] 已重新载入配置\n")

    def on_reset(self):
        if not messagebox.askyesno(APP_TITLE, "恢复为默认账号密码？当前修改会丢失。"):
            return
        self.cfg = cfgmod.load("__no_such_file__")  # 强制用默认值
        self._load_into_ui()
        self.log_line("[*] 已恢复默认配置（记得点保存）\n")

    # ---------- 日志 ----------
    def log_line(self, text):
        self.log_queue.put(text)

    def _pump_log(self):
        try:
            while True:
                text = self.log_queue.get_nowait()
                if text == "__DONE__":
                    continue
                self.log.configure(state="normal")
                self.log.insert("end", text)
                self.log.see("end")
                self.log.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(120, self._pump_log)

    # ---------- 线程与状态 ----------
    def _set_busy(self, busy):
        state = "disabled" if busy else "normal"
        # 操作按钮都要跟着忙闲切换（漏掉某个会让它在运行时仍可点）
        for b in (self.btn_telnet, self.btn_ftp, self.btn_backup,
                  self.btn_reboot, self.btn_stock2ubi):
            b.configure(state=state)
        # 刷机按钮还要看勾选框
        if busy:
            self.btn_flash.configure(state="disabled")
        else:
            self.on_toggle_flash()

    def _start_worker(self, fn, *a, **kw):
        if self.worker and self.worker.is_alive():
            messagebox.showwarning(APP_TITLE, "已有任务在运行，请等它结束。")
            return
        self._set_busy(True)
        self.worker = threading.Thread(target=self._worker_wrap, args=(fn, a, kw),
                                       daemon=True)
        self.worker.start()

    def _worker_wrap(self, fn, a, kw):
        old = sys.stdout
        sys.stdout = LogRedirector(self.log_queue)
        try:
            fn(*a, **kw)
        except SystemExit as e:
            # flash.py 出错时用 sys.exit(1)，这里不该把整个 GUI 带走
            if e.code not in (0, None):
                self.log_queue.put(f"\n[!] 任务中止（退出码 {e.code}）\n")
        except Exception as e:
            self.log_queue.put(f"\n[!] 出错：{type(e).__name__}: {e}\n")
        finally:
            sys.stdout = old
            self.log_queue.put("__DONE__")

    def _load_flash(self):
        if self.flash is None:
            import importlib.util
            # 用 find_resource 而不是写死路径：打包成 exe 后 flash.py 在解包
            # 目录里，同时优先用 exe 同目录的那份，方便用户直接替换。
            path = find_resource("flash.py")
            if not path:
                raise FileNotFoundError("找不到 flash.py（应与程序放在一起）")
            spec = importlib.util.spec_from_file_location("flash", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            # flash.py 内部用 __file__ 决定工作目录，这里已在 main() 里 chdir
            self.flash = mod
        # 先按机型打底（端口、su 方式、功能开关），再用用户配置覆盖
        cfgmod.apply_model(self.cfg, self.flash, self.model)
        cfgmod.apply_to_flash(self.cfg, self.flash)
        return self.flash

    def _prepare(self):
        """保存配置 + 校验机型必需的字段 + 加载 flash 模块。

        返回 flash 模块；失败时弹框并返回 None。
        烽火机型没填 MAC 就直接拦下 —— 否则会拿着空密码去连 telnet，
        白白消耗认证机会（连续 3 次失败要锁 300 秒）。
        """
        try:
            ok, msg = self._derive_passwords_if_needed()
            if not ok:
                messagebox.showerror(APP_TITLE, msg)
                return None
            self._save_from_ui()
            return self._load_flash()
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"初始化失败：{e}")
            return None

    # ---------- TTL 串口 ----------
    def on_refresh_ports(self):
        """重新枚举串口并填进下拉框（不自动选中任何一个）。"""
        ports = serial_shell.list_ports()
        values = [f"{dev}" for dev, _ in ports]
        self.serial_combo.configure(values=values)
        if not serial_shell.available():
            self.serial_hint.configure(
                text="没有安装 pyserial，TTL 功能不可用。安装：pip install pyserial",
                foreground="#a00")
            self.serial_port.set("")
            return
        if not ports:
            self.serial_hint.configure(
                text="没有发现串口设备。请插上 USB-TTL 线，再点「刷新」。",
                foreground="#a00")
            self.serial_port.set("")
            return
        desc = "；".join(f"{dev} = {d}" for dev, d in ports)
        self.serial_hint.configure(
            text=f"发现 {len(ports)} 个串口：{desc}\n"
                 "请自己选一个（程序不猜）。常见 USB-TTL 芯片是 CH340 / CP2102 / FT232。",
            foreground="#666")
        # 只有一个串口时替用户选上，省一步点击；多个时保持空，必须自己选。
        if len(ports) == 1:
            self.serial_port.set(values[0])
        elif self.serial_port.get() not in values:
            self.serial_port.set("")

    def _on_serial_toggle(self):
        """勾上「走 TTL 串口」后，联网相关的按钮就该灰掉。"""
        on = bool(self.serial_on.get())
        if on:
            self.on_refresh_ports()
        for b in (self.btn_telnet, self.btn_ftp):
            b.configure(state="disabled" if on else "normal")
        self.serial_combo.configure(state="readonly" if on else "readonly")
        self._apply_model_to_ui()

    def _serial_args(self):
        """把界面上的串口设置变成 run_flow 认的 args 片段。"""
        if not self.serial_on.get():
            return {}
        port = (self.serial_port.get() or "").strip()
        if not port:
            messagebox.showerror(
                APP_TITLE,
                "已勾选「走 TTL 串口」，但没有选择串口。\n\n"
                "请点「刷新」，然后在下拉框里选一个。")
            return None
        return {"serial_port": port,
                "serial_baud": int(self.serial_baud.get() or 115200)}

    # ---------- 各操作 ----------
    def run_mode(self, mode):
        flash = self._prepare()
        if flash is None:
            return
        serial = self._serial_args()
        if serial is None:
            return

        args = {"mode": mode, "enable_telnet": flash.AUTO_ENABLE_TELNET,
                "enable_ftp": flash.AUTO_ENABLE_FTP, "yes": False,
                "reboot": False,
                "force_backup": not flash.SKIP_BACKUP_IF_COMPLETE}
        args.update(serial)

        label = {"telnet": "开启 Telnet", "ftp": "开启 FTP",
                 "backup": "备份全部分区"}[mode]
        via = "（TTL 串口）" if serial else ""
        self.log_line(f"\n{'=' * 56}\n[*] 开始：{label}{via}"
                      f"（机型：{self.model.short}）\n{'=' * 56}\n")
        self._start_worker(flash.run_flow, args)

    def on_reboot(self):
        if not messagebox.askyesno(
                APP_TITLE,
                "确认重启光猫？\n\n设备会断开 1~3 分钟，期间无法访问。\n"
                "重启也会清掉所有网页会话和 telnet 锁定。"):
            return
        flash = self._prepare()
        if flash is None:
            return
        args = {"mode": "backup", "reboot": True,
                "enable_telnet": False, "enable_ftp": False,
                "yes": False, "force_backup": False}
        args.update(self._serial_args() or {})
        self.log_line(f"\n{'=' * 56}\n[*] 开始：重启光猫"
                      f"（机型：{self.model.short}）\n{'=' * 56}\n")
        self._start_worker(flash.run_flow, args)

    def on_toggle_flash(self):
        # 机型不支持刷 tcboot（烽火）时，勾选也不给解锁
        if self.allow_flash.get() and getattr(self, "_flash_supported", True):
            self.btn_flash.configure(state="normal")
        else:
            self.btn_flash.configure(state="disabled")

    def on_flash(self):
        # 恢复出厂设置的提醒放在界面顶部的固定提示里（不跟刷机按钮绑定）。
        # 这里只做刷机本身该做的确认。
        flash = self._prepare()
        if flash is None:
            return

        uboot = self.uboot_var.get().strip() or "tcboot.bin"
        path = find_resource(uboot)
        if not path:
            messagebox.showerror(
                APP_TITLE,
                f"找不到 U-Boot 文件：{uboot}\n\n"
                f"请把它放在程序同目录：\n{_resource_dir()}")
            return
        size = os.path.getsize(path)
        if size > 0x80000:
            messagebox.showerror(
                APP_TITLE, f"U-Boot 文件过大（{size} 字节），mtd0 仅 512KB。")
            return
        flash.UBOOT_FILE = uboot

        # 最终确认
        if not messagebox.askyesno(
                APP_TITLE,
                "即将刷写 U-Boot\n\n"
                f"文件：{uboot}（{size} 字节）\n"
                "目标：/dev/mtdblock0\n\n"
                "⚠️ 写入不可逆，断电或写错会导致设备变砖！\n"
                "程序会先做全分区备份，但仍请确认已备份重要数据。\n\n"
                "确定现在刷写吗？"):
            self.log_line("[*] 已取消刷机（未做任何写入）。\n")
            return

        args = {"mode": "flash", "enable_telnet": flash.AUTO_ENABLE_TELNET,
                "enable_ftp": flash.AUTO_ENABLE_FTP, "yes": True,
                "reboot": False, "force_backup": False}
        serial = self._serial_args()
        if serial is None:
            return
        args.update(serial)
        via = "（TTL 串口）" if serial else ""
        self.log_line(f"\n{'=' * 56}\n[!] 开始刷写 U-Boot：{uboot}{via}\n{'=' * 56}\n")
        self._start_worker(flash.run_flow, args)

    def on_stock2ubi(self):
        """上传 an758x-stock2ubi 并后台运行，之后在它自己的网页里刷 U-Boot。

        本工具只负责把它送上去跑起来；真正的刷写动作在它的网页里完成，
        所以这里不锁定、也不需要勾选「启用刷机功能」。
        """
        flash = self._prepare()
        if flash is None:
            return

        name = self.stock2ubi_var.get().strip() or "an758x-stock2ubi"
        path = find_resource(name)
        if not path:
            messagebox.showerror(
                APP_TITLE,
                f"找不到文件：{name}\n\n"
                f"请把 an758x-stock2ubi 放在程序同目录：\n{_resource_dir()}\n\n"
                f"下载地址：\n{flash.STOCK2UBI_PROJECT_URL}")
            return

        if not messagebox.askyesno(
                APP_TITLE,
                "即将上传并在光猫后台运行 an758x-stock2ubi\n\n"
                f"文件：{name}（{os.path.getsize(path)} 字节）\n"
                f"运行位置：光猫 /tmp，监听端口 {flash.STOCK2UBI_PORT}\n\n"
                "它会在设备上开一个网页，刷写 U-Boot 在**那个网页里**完成：\n"
                "先备份分区，再上传 preloader.bin / firstblock.bin 与 "
                "bl31-u-boot.fip。\n\n"
                "这一步本身不写入任何分区，但后续刷写有变砖风险。\n"
                "确定继续吗？"):
            self.log_line("[*] 已取消。\n")
            return

        args = {"mode": "stock2ubi",
                "enable_telnet": flash.AUTO_ENABLE_TELNET,
                "enable_ftp": flash.AUTO_ENABLE_FTP, "yes": False,
                "reboot": False, "force_backup": False,
                "stock2ubi_file": name,
                "stock2ubi_ignore_kernel": False}
        self.log_line(f"\n{'=' * 56}\n"
                      f"[*] 开始：运行 an758x-stock2ubi（{name}）\n"
                      f"{'=' * 56}\n")
        self._start_worker(flash.run_flow, args)


def self_test():
    """检查运行环境：flash.py 能否加载、依赖是否齐全。

    打包成 exe 后 flash.py 是运行时动态加载的，PyInstaller 看不到它的 import，
    漏掉某个模块要等真正用到处才炸（曾漏掉 http.server，备份时才报错）。
    这里把所有依赖提前导一遍，早暴露问题。

    命令行运行：`XG-040G-MD工具.exe --selftest`（GUI 版也能跑，会弹窗显示）
    """
    problems = []
    print(f"{APP_TITLE_FULL}")
    print(f"程序目录: {_resource_dir()}")
    print(f"打包资源: {_bundle_dir()}")

    # 1. flash.py 里的标准库依赖
    for m in ("http.server", "http.cookiejar", "urllib.request",
              "urllib.parse", "urllib.error", "posixpath", "base64",
              "hashlib", "subprocess", "threading", "socket", "json", "re"):
        try:
            __import__(m)
            print(f"  [✓] {m}")
        except Exception as e:
            problems.append(f"缺模块 {m}: {type(e).__name__}: {e}")
            print(f"  [✗] {m} — {e}")

    # 2. 第三方依赖
    for m in ("telnetlib3", "telnetlib3.telnetlib"):
        try:
            __import__(m)
            print(f"  [✓] {m}")
        except Exception as e:
            problems.append(f"缺模块 {m}: {type(e).__name__}: {e}")
            print(f"  [✗] {m} — {e}")

    # 2a. TTL 串口（按需加载；没装 pyserial 只影响 TTL，所以只提示不算失败）
    try:
        import serial_shell
        if serial_shell.available():
            print("  [✓] pyserial（TTL 串口可用）")
        else:
            print("  [!] 没装 pyserial —— TTL 串口功能不可用，"
                  "telnet 路线不受影响（pip install pyserial）")
    except Exception as e:  # noqa: BLE001
        print(f"  [!] serial_shell 不可用（{type(e).__name__}: {e}）"
              " —— 只影响 TTL")

    # 2b. 烽火机型后端（按需加载；缺了只影响烽火，所以只提示不算失败）
    try:
        import fhtool
        if fhtool.available():
            print("  [✓] fh_tool_cli（烽火机型可用）")
        else:
            print("  [!] fh_tool_cli 不可用 —— 只有烽火机型需要，"
                  "贝尔机型不受影响")
    except Exception as e:  # noqa: BLE001
        print(f"  [!] fhtool 后端不可用（{type(e).__name__}: {e}）"
              " —— 只有烽火机型需要")

    # 2c. 机型注册表
    try:
        import models
        print("  [✓] models.py（机型: "
              + " / ".join(m.short for m in models.all_models()) + "）")
    except Exception as e:  # noqa: BLE001
        problems.append(f"机型注册表 models.py 加载失败: {type(e).__name__}: {e}")
        print(f"  [✗] models.py — {type(e).__name__}: {e}")

    # 3. tkinter（GUI 自身依赖）
    try:
        import tkinter  # noqa: F401
        print("  [✓] tkinter")
    except Exception as e:
        problems.append(f"缺模块 tkinter: {type(e).__name__}: {e}")
        print(f"  [✗] tkinter — {e}")

    # 4. 真正加载 flash.py（这一步能暴露它内部的所有 import 问题）
    path = find_resource("flash.py")
    if not path:
        problems.append("找不到 flash.py")
        print("  [✗] flash.py 未找到")
    else:
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("flash_selftest", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            print(f"  [✓] flash.py 加载成功"
                  f"（版本号 {getattr(mod, 'VERSION', '未知')}，"
                  f"内置回退分区表 {len(mod.MTD_PARTITIONS)} 个，"
                  f"运行时以设备 /proc/mtd 为准）")
        except Exception as e:
            problems.append(f"flash.py 加载失败: {type(e).__name__}: {e}")
            print(f"  [✗] flash.py — {type(e).__name__}: {e}")

    # 5. tcboot.bin（刷机才需要，缺了只提示）
    if find_resource("tcboot.bin"):
        print("  [✓] tcboot.bin 已找到")
    else:
        print("  [!] 未找到 tcboot.bin（只有刷机需要，放 exe 同目录即可）")

    print()
    if problems:
        print("发现问题：")
        for p in problems:
            print("  -", p)
    else:
        print("自检通过：依赖齐全，flash.py 可正常加载。")
    return problems


def main():
    # flash.py 的备份目录、tcboot.bin 都是相对路径，统一切到程序所在目录
    os.chdir(_resource_dir())

    # 命令行自检：--selftest / -t（在 GUI 起来前跑，方便定位打包问题）
    if any(a in ("--selftest", "-t", "--test") for a in sys.argv[1:]):
        lines = []

        class _Cap:
            def write(self, t):
                lines.append(t)

            def flush(self):
                pass

        old = sys.stdout
        sys.stdout = _Cap()
        try:
            problems = self_test()
        finally:
            sys.stdout = old
        report = "".join(lines)
        # 打包成 GUI 程序后没有控制台，把结果写到文件，方便用户回传
        try:
            with open(os.path.join(_resource_dir(), "selftest.log"),
                      "w", encoding="utf-8") as f:
                f.write(report)
        except Exception:
            pass
        # 只在**失败**时弹窗：成功还弹会挡住自动化调用（踩过）。
        # --quiet（CI 用）则连失败也不弹，只靠 selftest.log 和退出码，
        # 否则弹窗会在无人值守的环境里把流程挂住。
        quiet = any(a in ("--quiet", "-q") for a in sys.argv[1:])
        if problems and not quiet:
            try:
                root = tk.Tk()
                root.withdraw()
                messagebox.showerror(
                    APP_TITLE + " · 自检",
                    "发现问题：\n\n" + "\n".join(problems) +
                    "\n\n详情见程序目录的 selftest.log")
                root.destroy()
            except Exception:
                pass
        return 1 if problems else 0

    root = tk.Tk()
    app = App(root)

    # 结束后恢复按钮（轮询 worker 状态，避免跨线程操作 tk）
    def watch():
        if app.worker and not app.worker.is_alive():
            app.worker = None
            app._set_busy(False)
        root.after(300, watch)

    watch()
    root.mainloop()


if __name__ == "__main__":
    sys.exit(main() or 0)
