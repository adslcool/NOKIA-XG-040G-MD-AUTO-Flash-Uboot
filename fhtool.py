#!/usr/bin/env python3
"""烽火（FiberHome）机型后端：直接 import `fh_tool_cli` 的库函数。

为什么不用它的命令行：
  1. 打包 exe 时不能指望用户在 Windows 上 `pip install` 一个 CLI 再 spawn 子进程；
     直接 import 库函数，PyInstaller 能把整个包静态打进 exe。
  2. 命令行输出是给人看的文本，我们要的是结构化结果 —— 库函数直接返回 dict。
  3. 子进程在 GUI 里还得处理编码/管道/僵尸进程，纯属自找麻烦。

依赖面：只用 `fh_tool_cli.backends.fh_tool`（RPC）、
`fh_tool_cli.backends.telnet`（telnet + su）、`fh_tool_cli.credentials`（密码推导）。
`fh_tool_cli.output` 会 import rich，但那是可选的显示层，我们不用它。

⚠️ MAC 必须用**无分隔符大写**形式（`70D51E046CEC`）：
   `derive_crypto()` 直接拿 MAC 字符串做 SHA256，带冒号算出来的密钥是错的，
   设备会返回 403（实测踩过）。
"""


import re


class FhToolUnavailable(RuntimeError):
    """没装 fh_tool_cli 或版本不兼容。"""


def _import_backend():
    """延迟导入，并给出可操作的错误提示。

    延迟导入的原因：贝尔机型完全用不到这个包，没装也不该影响它启动。
    """
    try:
        from fh_tool_cli.backends.fh_tool import api_payload, fh_tool_call
    except Exception as e:  # noqa: BLE001
        raise FhToolUnavailable(
            "缺少 fh_tool-cli 依赖，烽火机型需要它。\n"
            "  安装：pip install fh_tool-cli==0.2.7\n"
            f"  （{type(e).__name__}: {e}）"
        ) from e
    return api_payload, fh_tool_call


def available():
    """探测依赖是否可用（GUI 用来决定要不要提示安装）。"""
    try:
        _import_backend()
        return True
    except FhToolUnavailable:
        return False


def normalize_mac(mac):
    """统一成无分隔符大写（fh_tool 的 AES 密钥就是拿这个字符串算的）。"""
    return "".join(ch for ch in (mac or "") if ch.isalnum()).upper()


def probe_mac(ip, port=8080, timeout=6.0):
    """从设备上**只读**地读回 MAC（无分隔符大写 12 位），失败返回空串。

    为什么能读到：烽火在 8080 上有一个**不需要任何认证**的 AJAX 接口
    `GET /cgi-bin/ajax?ajaxmethod=web_get_brmad`，返回
    `{"brmad": "70D51E046CEC", ...}`。这个接口本来就是给网页 JS 拿 MAC 用的
    —— 而 MAC 正是 fh_tool 加密的密钥种子，所以「能访问 8080」就等于
    「能算出密钥」，这也是 fh_tool 无需登录就能改配置的根本原因。

    拿到 MAC 后就不必再让用户去机壳背贴上手抄 MAC 了：抄错一位，
    AES 密钥就全错，设备只会回一个 403，很难看出是抄错还是协议不对。
    """
    import json
    import urllib.error
    import urllib.request

    url = f"http://{ip}:{int(port)}/cgi-bin/ajax?ajaxmethod=web_get_brmad"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"http://{ip}:{int(port)}/",
        })
        with urllib.request.urlopen(req, timeout=float(timeout)) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError):
        return ""
    # 兼容两种返回：纯 JSON，或被包了一层的 JSONP。
    m = re.search(r'\{[^{}]*"brmad"\s*:\s*"([^"]+)"[^{}]*\}', raw)
    if m:
        cand = m.group(1)
    else:
        try:
            cand = (json.loads(raw) or {}).get("brmad", "")
        except (ValueError, TypeError):
            return ""
    cand = normalize_mac(cand)
    # 必须正好 12 位十六进制，否则说明接口返回了别的东西（登录页 / 404 页面）
    return cand if re.fullmatch(r"[0-9A-F]{12}", cand) else ""


class FhClient:
    """一台烽火光猫的 fh_tool RPC 客户端（只做读写，不碰 telnet）。

    fh_tool 是烽火在 8080 端口上开的一个**无需登录**的加密 RPC：
    请求体用 MAC 派生的 AES 加密，所以「知道 MAC」就等于「有权限」。
    这也是它能在不登录后台的情况下读账号、开 telnet 的原因。
    """

    def __init__(self, ip, mac, port=8080, timeout=8.0):
        self.ip = ip
        self.mac = normalize_mac(mac)
        self.port = int(port)
        self.timeout = float(timeout)
        self._api_payload = None
        self._fh_tool_call = None

    def _ensure(self):
        if self._fh_tool_call is None:
            self._api_payload, self._fh_tool_call = _import_backend()

    def call(self, func, params=None):
        """调一个 fh_tool API，返回解密后的 dict。

        `func` 是设备端的函数名，例如 GetDevInfo / TelnetEnable /
        GetAdminAccount / GetPppoeAccount / DeviceReboot。
        """
        self._ensure()
        if len(self.mac) != 12:
            raise ValueError(
                f"MAC 格式不对（{self.mac!r}）：需要 12 位十六进制，"
                "例如 70D51E046CEC")
        payload = self._api_payload(func, params)
        return self._fh_tool_call(self.ip, self.mac, payload, self.timeout,
                                  port=self.port)

    # ---------- 具体功能（都用上面的 call）----------
    def dev_info(self):
        return self.call("GetDevInfo")

    def admin_account(self):
        """读网页超级管理员账号密码（无需登录）。

        返回 {"username": ..., "password": ...}；设备不支持时返回空串。
        """
        r = self.call("GetAdminAccount") or {}
        return {
            "username": r.get("adminname", "") or "",
            "password": r.get("adminpwd", "") or "",
        }

    def pppoe_account(self):
        r = self.call("GetPppoeAccount") or {}
        return {
            "username": r.get("username", "") or "",
            "password": r.get("password", "") or "",
        }

    def telnet_enabled(self, value=True):
        """开 / 关 telnet。value=True 开，False 关。"""
        return self.call("TelnetEnable", {"telnet": "1" if value else "0"})

    def reboot(self):
        return self.call("DeviceReboot")

    def result(self):
        """读注册结果（运营商开通状态）。"""
        return self.call("GetResult")


# ---------- telnet / su ----------
def telnet_shell(ip, username, password, port=23, timeout=10.0):
    """建一个 TelnetShell（fh_tool_cli 的实现，会处理 IAC 和提示符）。

    注意：它的 `run_as_root` 走的是 `su root`，正好符合烽火的提权方式；
    贝尔机型用的是 `su user_ftp`，所以贝尔不用这个函数。
    """
    try:
        from fh_tool_cli.backends.telnet import TelnetCredentials, TelnetShell
    except Exception as e:  # noqa: BLE001
        raise FhToolUnavailable(
            f"缺少 fh_tool-cli 的 telnet 后端（{type(e).__name__}: {e}）"
        ) from e
    return TelnetShell(TelnetCredentials(
        host=ip, port=int(port), username=username, password=password,
        timeout=float(timeout)))


def run_as_root(shell, command, su_password):
    """以 root 跑一条命令（内部：telnet 登录 → su root → 执行 → 退出）。"""
    return shell.run_as_root(command, su_password=su_password)
