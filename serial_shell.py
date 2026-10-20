#!/usr/bin/env python3
"""TTL / 串口控制台：把串口包装成和 telnetlib3.Telnet 一样的接口。

为什么这么做
------------
flash.py 的备份 / 刷写 / 回读校验逻辑，全部只通过 tn 的四个方法跟设备对话：

    tn.write(bytes)              发数据
    tn.expect([patterns], timeout) -> (index, match, text)
    tn.read_until(bytes, timeout)  -> bytes
    tn.read_very_eager()           -> bytes
    tn.close()

所以只要串口对象实现同样的方法，整套逻辑（读分区表、curl 回传备份、
dd 写入、轮询回读校验）就能**原样**跑在 TTL 上，不用再写第二份实现。
这也是本项目能同时支持「免拆机 telnet」和「拆机 TTL」两条路的原因。

Windows 兼容
------------
pyserial 的 Serial 在 Windows 上直接吃 "COM3" 这种名字，在 Linux 上吃
"/dev/ttyUSB0"；串口列表用 serial.tools.list_ports 枚举，两个平台都能列。

**程序不做任何端口猜测**：端口一律由用户从列表里自己选（命令行用
--serial-port，界面用下拉框）。自动探测在插了多个串口设备时会选错，
而选错串口的代价是往别人的设备里敲字符。
"""
from __future__ import annotations

import re
import sys
import time

# 这些是这类光猫 / 路由常见的控制台波特率，界面下拉框直接用这个顺序。
BAUD_RATES = ("115200", "57600", "38400", "19200", "9600", "230400", "460800")


def _serial_module():
    """延迟导入 pyserial，并给出人话错误。

    pyserial 是唯一一个「只在用 TTL 时才需要」的第三方依赖，所以不放在
    模块顶层：没装 pyserial 时，telnet 路线仍然要能正常跑。
    """
    try:
        import serial  # noqa: F401
        import serial.tools.list_ports  # noqa: F401
        return serial
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            "TTL 串口功能需要 pyserial，但没有安装：\n"
            "    pip install pyserial\n"
            f"（原始错误：{e}）") from e


def available() -> bool:
    """pyserial 是否可用（GUI 用它决定 TTL 区是否可点）。"""
    try:
        _serial_module()
        return True
    except RuntimeError:
        return False


def list_ports():
    """列出当前系统上的串口：[(device, description), ...]。

    只列出来给用户选，不做任何自动选择。列表为空说明没插串口设备。
    """
    try:
        _serial_module()
    except RuntimeError:
        return []
    from serial.tools import list_ports as _lp
    out = []
    for p in _lp.comports():
        desc = p.description or ""
        # Linux 上 ttyS* 会列出主板自带的（通常是空的）串口，description 里
        # 带 "n/a" 的一律标出来，免得用户误选。
        if desc.strip() in ("", "n/a"):
            desc = "无描述"
        out.append((p.device, desc))
    return out


class SerialShell:
    """把 pyserial 的 Serial 包装成 telnetlib3.Telnet 的接口子集。"""

    def __init__(self, port, baudrate=115200, timeout=0.2):
        self.port = port
        self.baudrate = int(baudrate)
        self.timeout = timeout
        self._buf = b""
        self.ser = None
        self.eof = False

    # ---------- 生命周期 ----------
    def open(self):
        serial = _serial_module()
        try:
            self.ser = serial.Serial(
                self.port, self.baudrate, timeout=self.timeout,
                bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                # 光猫控制台不用流控，开了反而收不到数据
                xonxoff=False, rtscts=False, dsrdtr=False)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"打开串口 {self.port} 失败：{e}") from e
        return self

    def close(self):
        self.eof = True
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:  # noqa: BLE001
                pass
            self.ser = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    # ---------- 底层读写 ----------
    def _read_some(self):
        """读一批数据；没有数据时最多等 self.timeout 秒。"""
        if self.ser is None:
            return b""
        try:
            n = self.ser.in_waiting
            if n:
                return self.ser.read(n)
            # in_waiting 为 0 时 read(1) 会阻塞到 timeout，正好用来轮询
            return self.ser.read(1)
        except Exception:  # noqa: BLE001  串口被拔掉 / 设备掉电
            self.eof = True
            return b""

    def write(self, data):
        """发数据。bytes / str 都接受（telnetlib3 只吃 bytes，这里放宽）。"""
        if isinstance(data, str):
            data = data.encode()
        if self.ser is None:
            return
        self.ser.write(data)
        try:
            self.ser.flush()
        except Exception:  # noqa: BLE001
            pass

    def sendline(self, line=""):
        self.write((line if isinstance(line, bytes) else line.encode()) + b"\r\n")

    def read_very_eager(self):
        """把缓冲区里已有的数据全部取走（不等待）。"""
        if self.ser is not None:
            try:
                while self.ser.in_waiting:
                    self._buf += self.ser.read(self.ser.in_waiting)
            except Exception:  # noqa: BLE001
                self.eof = True
        out, self._buf = self._buf, b""
        return out

    # ---------- 与 telnetlib3 对齐的两个方法 ----------
    def expect(self, patterns, timeout=None):
        """等 patterns 里任一正则匹配，返回 (index, match, 含匹配的文本)。

        语义与 telnetlib3.Telnet.expect 一致：
          - 命中：返回 (下标, Match, 从缓冲头到匹配结尾的字节)
          - 超时：返回 (-1, None, 缓冲区剩余字节)
        调用方（flash.py 的 expect_any）依赖这个语义，别改。
        """
        compiled = []
        for p in patterns:
            compiled.append(p if hasattr(p, "search") else re.compile(p))
        deadline = time.time() + (30.0 if timeout is None else timeout)
        while True:
            for i, pat in enumerate(compiled):
                m = pat.search(self._buf)
                if m:
                    end = m.end()
                    text = self._buf[:end]
                    self._buf = self._buf[end:]
                    return i, m, text
            if time.time() >= deadline or self.eof:
                text, self._buf = self._buf, b""
                return -1, None, text
            self._buf += self._read_some()

    def read_until(self, match, timeout=None):
        """读到 match 出现为止，返回含 match 的字节。

        与 telnetlib3 一致：**超时不抛异常**，返回已经收到的内容。
        （所以调用方不能用「没抛异常」当作「匹配到了」，必须自己检查
        返回值里有没有那个字节串 —— flash.py 的 wait_for 已经这么做了。）
        """
        if isinstance(match, str):
            match = match.encode()
        deadline = time.time() + (30.0 if timeout is None else timeout)
        while True:
            i = self._buf.find(match)
            if i >= 0:
                end = i + len(match)
                text = self._buf[:end]
                self._buf = self._buf[end:]
                return text
            if time.time() >= deadline or self.eof:
                text, self._buf = self._buf, b""
                return text
            self._buf += self._read_some()


# ================== 登录 / 识别控制台 ==================

# 控制台可能停在的地方。顺序有意义：越具体的越靠前。
CONSOLE_LOGIN = "login"        # 普通 login: 提示符
CONSOLE_PASSWORD = "password"  # Password: 提示符
CONSOLE_ROOT = "root"          # 已经是 root shell（#）
CONSOLE_USER = "user"          # 非 root shell（$）
CONSOLE_UBOOT = "uboot"        # U-Boot 命令行
CONSOLE_UBOOT_BOOT = "uboot_boot"  # U-Boot 正在自动启动
CONSOLE_NONE = "none"

_LOGIN_RE = rb"login:\s*$"
_PASSWORD_RE = rb"[Pp]assword:\s*$"
_ROOT_RE = rb"[\r\n]#\s*$"
_USER_RE = rb"[\r\n]\$\s*$"
_UBOOT_RE = rb"(U-Boot>|AN7581>|AN7581#[ ]*$|=>\s*$|ar7240>\s*$)"
_UBOOT_BOOT_RE = rb"Hit any key to stop autoboot|Press any key to stop"


def detect_console(shell, timeout=8.0):
    """敲一个回车，看看当前控制台停在什么状态。"""
    shell.read_very_eager()
    shell.write(b"\r\n")
    idx, match, text = shell.expect(
        [_LOGIN_RE, _PASSWORD_RE, _ROOT_RE, _USER_RE, _UBOOT_RE, _UBOOT_BOOT_RE],
        timeout=timeout)
    kind = {0: CONSOLE_LOGIN, 1: CONSOLE_PASSWORD, 2: CONSOLE_ROOT,
            3: CONSOLE_USER, 4: CONSOLE_UBOOT, 5: CONSOLE_UBOOT_BOOT}.get(idx)
    return kind or CONSOLE_NONE, text


def serial_login(shell, username="root", password="", timeout=10.0):
    """在串口控制台上拿到 root shell。

    返回 (ok, 说明)。ok=False 时说明里写清停在哪一步，方便用户判断是不是
    波特率错了 / 线接反了 / 需要先按 Ctrl-C 打断 U-Boot 自动启动。

    注意：这里**不做密码尝试**。串口控制台的 root 密码各机型不同，猜错只会
    拖长流程；用户自己知道就填，不知道就留空（很多机型串口是免密直接 root）。
    """
    kind, text = detect_console(shell, timeout=timeout)

    if kind == CONSOLE_ROOT:
        return True, "串口控制台已是 root shell"
    if kind == CONSOLE_USER:
        return False, ("串口停在普通用户 shell（$），不是 root。"
                       "请用 root 账号登录，或检查设备权限配置。")
    if kind in (CONSOLE_UBOOT, CONSOLE_UBOOT_BOOT):
        return False, ("串口停在 U-Boot（引导器）里，不是 Linux shell。\n"
                       "  如果设备还能正常启动：断电重上电，看到 "
                       "「Hit any key to stop autoboot」时**不要**按键，让它自己启动完。\n"
                       "  如果设备已经起不来（砖了）：这正是 TTL 的用途 —— "
                       "在 U-Boot 里恢复，需要你自己在串口终端里操作。")

    if kind == CONSOLE_LOGIN:
        shell.write(username.encode() + b"\r\n")
        idx, _, t = shell.expect([_PASSWORD_RE, _ROOT_RE, _LOGIN_RE], timeout=timeout)
        if idx == 0:
            if password:
                shell.write(password.encode() + b"\r\n")
            else:
                shell.write(b"\r\n")   # 免密机型直接回车
            idx2, _, t2 = shell.expect([_ROOT_RE, _USER_RE, _LOGIN_RE], timeout=timeout)
            if idx2 == 0:
                return True, f"串口登录成功（{username}，已是 root）"
            if idx2 == 1:
                return False, "串口登录成功，但拿到的是普通用户 shell（$），不是 root。"
            return False, ("串口登录失败：root 密码不对。\n"
                           f"  设备回显：{_tail(t2)}")
        if idx == 1:
            return True, f"串口登录成功（{username}，无需密码）"
        return False, f"串口上不接受账号 {username}。设备回显：{_tail(t)}"

    if kind == CONSOLE_PASSWORD:
        shell.write((password or "").encode() + b"\r\n")
        idx, _, t = shell.expect([_ROOT_RE, _USER_RE, _LOGIN_RE], timeout=timeout)
        if idx == 0:
            return True, "串口登录成功（已是 root）"
        return False, f"串口密码被拒。设备回显：{_tail(t)}"

    return False, ("串口没有任何回应。请检查：\n"
                   "  1) 端口选对了没有（界面下拉框里换一个试试）；\n"
                   "  2) 波特率对不对（这类光猫常见 115200，其次 57600）；\n"
                   "  3) TX/RX 有没有接反（TX 接 RX、RX 接 TX），GND 必须共地；\n"
                   "  4) 设备是否已上电。\n"
                   f"  收到的原始数据：{_tail(text)!r}")


def _tail(data, n=160):
    """把回显裁成一行，方便塞进日志。"""
    if not data:
        return ""
    s = data.decode("utf-8", "replace")
    s = " ".join(s.split())
    return s[-n:]


def serial_connect(port, baudrate=115200, username="root", password="",
                   timeout=10.0):
    """打开串口并登录到 root，返回 SerialShell。失败抛 RuntimeError。"""
    shell = SerialShell(port, baudrate=baudrate).open()
    ok, msg = serial_login(shell, username=username, password=password,
                           timeout=timeout)
    if not ok:
        shell.close()
        raise RuntimeError(msg)
    return shell


if __name__ == "__main__":
    # 直接跑本文件时，只做一件事：列出串口，方便确认设备名。
    ports = list_ports()
    if not ports:
        print("没有发现串口设备。")
        sys.exit(1)
    for dev, desc in ports:
        print(f"{dev:24} {desc}")
