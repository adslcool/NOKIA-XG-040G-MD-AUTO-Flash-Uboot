#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""烽火（FiberHome）配置密文解密 —— telnet 密码错误时的兜底通道。

## 为什么需要这个文件

烽火光猫的 telnet 密码存在固件的 `param.pdt.enc` 里（UCI 里是
`option TelnetPassword '2506000049ECBE32C8DE760F0E960B0F0A53953F'` 这种密文）。
网上流传的默认密码（`FH-nE7jA%5m`、带 MAC 后缀的变体）只对**没改过密码**的机器有效。
运营商装维改过密码、或者恢复出厂后预置包不同，就会出现「telnet 密码错误」，
而这时候恰恰**没有** telnet 权限去读配置文件 —— 死锁。

本模块用固件里的 AES 工作密钥直接解 `param.pdt.enc`，绕开 telnet 拿明文密码。

## 密钥是怎么来的（逆向记录）

固件 `/fhrom/lib/libfhcrypto_new.so` 的 `fh_decrypt_string_aes` 最终调到
`fhssl_aes_encryptdecrypt` → `wolfSSL_EVP_CipherInit_ex`。用 LD_PRELOAD 钩住
后者抓到的第二段 key/iv 就是工作密钥：

    k2  = d11683d02a88b86c6ce50e8514fffeefc2e4b339b40ac7de12945fe1fa79720a  (AES-256)
    iv2 = 22f941441cbff9ed83637e8125c12920

（第一段 key 是 `rol32(calidata 头 4 字节, i)` 派生的，只用于解 calidata 自身，
不是工作密钥；别混用。）

## 为什么可以内置（不按设备抓）

`calidata` 和 `param.pdt.enc` 都是**固件级文件**，同一固件的所有机器完全相同。
实测两台不同设备（base MAC `F0:AB:F3:49:60:98` 与 `70:D5:1E:16:C6:68`，
固件 V4.20.M5000）：

    calidata       md5 28bbd2bc83e3362429bf83a1a8215492  (980 字节)
    param.pdt.enc  md5 21209ddb60305df56b2fa99d0a7e3d2e  (1770484 字节)

两台逐字节一致，所以密钥表按**固件版本**索引即可，不必每台现场抓。

## 密文容器格式

前 4 字节是容器头（`25 06 00 00`，原样保留不参与解密），后面是 AES-256-CBC
密文 + PKCS#7 填充：

    25060000 49ECBE32C8DE760F0E960B0F0A53953F  ->  FH-nE7jA%5m
    25060000 A2E7DA14CC47FA6DA5A980B5E957F438  ->  0

注意：**换固件版本密钥可能不同**。表里没命中时本模块会明确报错，不会瞎猜
（猜错只会给出乱码，比报错更糟）。
"""

import re
import struct

__all__ = [
    "DecryptError",
    "FIRMWARE_KEYS",
    "decrypt_value",
    "decrypt_value_auto",
    "parse_param_xml",
    "find_param_pdt_enc",
    "extract_telnet_info",
    "describe_keys",
]


class DecryptError(Exception):
    """解密失败（密钥不对、格式不对、或固件版本不在表里）。"""


# ---------------------------------------------------------------------------
# 密钥表
# ---------------------------------------------------------------------------
# 每项: (固件版本匹配正则, k2_hex, iv2_hex, 说明)
# 匹配用 re.search，所以写版本号的一部分就行。
FIRMWARE_KEYS = [
    (
        r"V4\.20\.M5000",
        "d11683d02a88b86c6ce50e8514fffeefc2e4b339b40ac7de12945fe1fa79720a",
        "22f941441cbff9ed83637e8125c12920",
        "HG5585F 电信版 V4.20.M5000（实测：浙江/安徽预置包 + 两台真机）",
    ),
    (
        # 兜底：目前验证过的烽火 AN7581 固件都用同一套。放最后，版本没命中时
        # 仍然尝试一次 —— 解出来是 XML 才算成功，解错会自然失败。
        r".*",
        "d11683d02a88b86c6ce50e8514fffeefc2e4b339b40ac7de12945fe1fa79720a",
        "22f941441cbff9ed83637e8125c12920",
        "通用兜底（AN7581 家族目前共用的固件密钥）",
    ),
]

# 容器头：4 字节。出现它就说明后面跟着 AES-256-CBC 密文。
CIPHER_MAGIC = b"\x25\x06\x00\x00"

# param.pdt.enc 的大小（用于在 rootfs 镜像里定位）
PARAM_PDT_ENC_SIZE = 1770484


def describe_keys():
    """返回密钥表的可读说明（给 --selftest / 日志用）。"""
    lines = []
    for pat, k2, iv2, note in FIRMWARE_KEYS:
        lines.append(f"  {pat:<16} k2={k2[:16]}… iv2={iv2[:8]}…  {note}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# AES（优先用 cryptography，没有就用内置的纯 Python 实现）
# ---------------------------------------------------------------------------
def _aes_cbc_decrypt(key, iv, data):
    """AES-CBC 解密，返回明文（已去 PKCS#7 填充）。"""
    if len(data) % 16:
        raise DecryptError(f"密文长度 {len(data)} 不是 16 的倍数，不是 AES-CBC 数据")
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        out = dec.update(data) + dec.finalize()
    except ImportError:
        out = _py_aes_cbc_decrypt(key, iv, data)
    return _strip_pkcs7(out)


def _strip_pkcs7(data):
    if not data:
        return data
    pad = data[-1]
    if 1 <= pad <= 16 and data[-pad:] == bytes([pad]) * pad:
        return data[:-pad]
    return data


# ---- 纯 Python AES-128/192/256 解密（cryptography 不可用时兜底）------------
_SBOX = None
_INV_SBOX = None


def _init_sbox():
    """生成 AES S-box 与逆 S-box。

    用 GF(2^8) 的指数/对数表算乘法逆元，再做仿射变换 —— 这样最不容易写错。
    （早先那版用「p 乘 3 / q 除 3」的位技巧，写错了且 FIPS-197 测试向量不过关。）
    """
    global _SBOX, _INV_SBOX
    if _SBOX is not None:
        return
    # 生成元取 3：exp[i] = 3^i，log 是反查表
    exp = [0] * 512
    log = [0] * 256
    x = 1
    for i in range(255):
        exp[i] = x
        log[x] = i
        x = _mul(x, 3)
    for i in range(255, 512):
        exp[i] = exp[i - 255]

    def inverse(a):
        if a == 0:
            return 0
        return exp[255 - log[a]]

    def affine(b):
        # s = b ^ rotl(b,1) ^ rotl(b,2) ^ rotl(b,3) ^ rotl(b,4) ^ 0x63
        r = b
        for s in (1, 2, 3, 4):
            r ^= ((b << s) | (b >> (8 - s))) & 0xFF
        return r ^ 0x63

    sbox = [affine(inverse(a)) for a in range(256)]
    inv = [0] * 256
    for i, v in enumerate(sbox):
        inv[v] = i
    _SBOX, _INV_SBOX = sbox, inv


def _xtime(a):
    a <<= 1
    if a & 0x100:
        a ^= 0x11B
    return a & 0xFF


def _mul(a, b):
    r = 0
    while b:
        if b & 1:
            r ^= a
        a = _xtime(a)
        b >>= 1
    return r


def _expand_key(key):
    """AES 密钥扩展，返回 4*(Nr+1) 个 32 位字。"""
    nk = len(key) // 4
    if nk not in (4, 6, 8):
        raise DecryptError(f"AES 密钥长度 {len(key)} 字节不合法")
    nr = nk + 6
    w = [int.from_bytes(key[4 * i:4 * i + 4], "big") for i in range(nk)]
    rcon = 1
    for i in range(nk, 4 * (nr + 1)):
        t = w[i - 1]
        if i % nk == 0:
            # RotWord + SubWord + Rcon
            t = ((t << 8) | (t >> 24)) & 0xFFFFFFFF
            t = (
                (_SBOX[(t >> 24) & 0xFF] << 24)
                | (_SBOX[(t >> 16) & 0xFF] << 16)
                | (_SBOX[(t >> 8) & 0xFF] << 8)
                | _SBOX[t & 0xFF]
            )
            t ^= rcon << 24
            rcon = _xtime(rcon)
        elif nk > 6 and i % nk == 4:
            t = (
                (_SBOX[(t >> 24) & 0xFF] << 24)
                | (_SBOX[(t >> 16) & 0xFF] << 16)
                | (_SBOX[(t >> 8) & 0xFF] << 8)
                | _SBOX[t & 0xFF]
            )
        w.append(w[i - nk] ^ t)
    return w, nr


def _py_aes_decrypt_block(w, nr, block):
    """解密单个 16 字节块。"""
    # AES 的 state 按列装填：第 c 列 = block[4c:4c+4]。
    # 曾误写成 block[i::4]（按行取），加密/解密结果全错。
    s = [list(block[4 * i:4 * i + 4]) for i in range(4)]  # 4 列 × 4 字节

    def add_round_key(rnd):
        for c in range(4):
            k = w[rnd * 4 + c]
            s[c][0] ^= (k >> 24) & 0xFF
            s[c][1] ^= (k >> 16) & 0xFF
            s[c][2] ^= (k >> 8) & 0xFF
            s[c][3] ^= k & 0xFF

    def inv_shift_rows():
        for r in range(1, 4):
            vals = [s[c][r] for c in range(4)]
            vals = vals[-r:] + vals[:-r]
            for c in range(4):
                s[c][r] = vals[c]

    def inv_sub_bytes():
        for c in range(4):
            for r in range(4):
                s[c][r] = _INV_SBOX[s[c][r]]

    def inv_mix_columns():
        for c in range(4):
            a = s[c][:]
            s[c][0] = _mul(a[0], 14) ^ _mul(a[1], 11) ^ _mul(a[2], 13) ^ _mul(a[3], 9)
            s[c][1] = _mul(a[0], 9) ^ _mul(a[1], 14) ^ _mul(a[2], 11) ^ _mul(a[3], 13)
            s[c][2] = _mul(a[0], 13) ^ _mul(a[1], 9) ^ _mul(a[2], 14) ^ _mul(a[3], 11)
            s[c][3] = _mul(a[0], 11) ^ _mul(a[1], 13) ^ _mul(a[2], 9) ^ _mul(a[3], 14)

    add_round_key(nr)
    for rnd in range(nr - 1, 0, -1):
        inv_shift_rows()
        inv_sub_bytes()
        add_round_key(rnd)
        inv_mix_columns()
    inv_shift_rows()
    inv_sub_bytes()
    add_round_key(0)

    return bytes(b for c in range(4) for b in s[c])


def _py_aes_cbc_decrypt(key, iv, data):
    _init_sbox()
    w, nr = _expand_key(key)
    out = bytearray()
    prev = iv
    for off in range(0, len(data), 16):
        blk = data[off:off + 16]
        dec = _py_aes_decrypt_block(w, nr, blk)
        out += bytes(a ^ b for a, b in zip(dec, prev))
        prev = blk
    return bytes(out)


# ---------------------------------------------------------------------------
# 解密
# ---------------------------------------------------------------------------
def decrypt_value(cipher_hex, key_hex, iv_hex, has_magic=True):
    """解一个 UCI 里形如 `25060000<32 位 hex>` 的值。

    has_magic=False 时把整个输入都当密文（用于直接喂二进制）。
    """
    if isinstance(cipher_hex, str):
        cipher_hex = cipher_hex.strip().strip("'\"")
        if cipher_hex.startswith("0x") or cipher_hex.startswith("0X"):
            cipher_hex = cipher_hex[2:]
        try:
            raw = bytes.fromhex(cipher_hex)
        except ValueError:
            raise DecryptError(f"不是合法的十六进制串：{cipher_hex[:40]}…")
    else:
        raw = bytes(cipher_hex)

    if has_magic:
        if not raw.startswith(CIPHER_MAGIC):
            raise DecryptError(
                f"密文没有 `25060000` 容器头（实际开头 {raw[:4].hex()}），格式不对"
            )
        raw = raw[4:]

    key = bytes.fromhex(key_hex)
    iv = bytes.fromhex(iv_hex)
    return _aes_cbc_decrypt(key, iv, raw)


def decrypt_value_auto(cipher_hex, firmware_version=""):
    """按固件版本挑密钥解密，返回 (明文, 命中的说明)。

    明文按 UTF-8 解；解不出来（说明密钥不对）就抛 DecryptError。
    """
    last_err = None
    for pat, k2, iv2, note in FIRMWARE_KEYS:
        if firmware_version and not re.search(pat, firmware_version):
            continue
        try:
            raw = decrypt_value(cipher_hex, k2, iv2)
        except DecryptError as exc:
            last_err = exc
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            last_err = DecryptError("解出来不是 UTF-8（密钥不对）")
            continue
        # 明文应当是可见字符（密码、数字、开关）
        if text and all(0x20 <= ord(ch) <= 0x7E for ch in text):
            return text, note
        last_err = DecryptError(f"解出来的不是可打印文本：{text[:20]!r}")
    raise last_err or DecryptError(
        f"固件版本 {firmware_version!r} 不在密钥表里（已知：{describe_keys()}）"
    )


# ---------------------------------------------------------------------------
# param.pdt.enc
# ---------------------------------------------------------------------------
def find_param_pdt_enc(blob):
    """在 rootfs 镜像（squashfs）或裸文件里定位 param.pdt.enc。

    做法：找 `25060000` 开头、且按已知密钥能解出 XML 的那一段。
    返回 (起始偏移, 内容) 或 (None, None)。

    为什么不按固定偏移：不同机型的 rootfs 布局不一样，硬编码偏移必然翻车。
    """
    body_len = PARAM_PDT_ENC_SIZE - 4
    pos = 0
    while True:
        pos = blob.find(CIPHER_MAGIC, pos)
        if pos < 0:
            return None, None
        chunk = blob[pos:pos + PARAM_PDT_ENC_SIZE]
        if len(chunk) >= 4 + 16:
            # 只试第一块，能解出 `<!--` 就认定命中（省时间）
            try:
                head = decrypt_value(chunk[:4 + 16], FIRMWARE_KEYS[0][1], FIRMWARE_KEYS[0][2])
                if head.startswith(b"<!--") or head.startswith(b"<?xml") or head.startswith(b"<"):
                    if len(chunk) >= body_len:
                        return pos, chunk[:PARAM_PDT_ENC_SIZE]
            except DecryptError:
                pass
        pos += 1


_TAG_RE = re.compile(rb"<(/?)(obj|trf|param)\b([^>]*?)(/?)>", re.S)
_NAME_RE = re.compile(rb'name="([^"]*)"')
_VALUE_RE = re.compile(rb'value="([^"]*)"')


def parse_param_xml(xml_bytes):
    """把解出来的 param.pdt.enc XML 解析成 {完整路径: value}。

    **必须带路径**：同一个 `TelnetPassword` 在 param.xml 里有三份，含义完全不同：

        ...X_CT-COM_ServiceManage.TelnetPassword          = FH-nE7jA%5m  ← 真正登录用的
        ...X_CT-COM_ServiceManage.Lan.TelnetPassword      = abcd          ← LAN 侧备用
        ...X_CT-COM_ServiceManage.TelnetAccount.0.TelnetPassword = admin  ← 多账号表里的占位

    早期版本按裸名字解析，结果拿到最后一个（`admin`），看起来像解错了，
    其实是覆盖问题。所以这里用 <obj>/<trf> 嵌套栈拼出完整路径。
    """
    if isinstance(xml_bytes, str):
        xml_bytes = xml_bytes.encode("utf-8")
    out = {}
    stack = []
    for m in _TAG_RE.finditer(xml_bytes):
        closing, tag, attrs, selfclose = m.groups()
        if tag == b"param":
            nm = _NAME_RE.search(attrs)
            if not nm:
                continue
            val = _VALUE_RE.search(attrs)
            path = ".".join(stack + [nm.group(1).decode("utf-8", "replace")])
            out[path] = val.group(1).decode("utf-8", "replace") if val else ""
        elif closing:
            if stack:
                stack.pop()
        elif not selfclose:
            nm = _NAME_RE.search(attrs)
            stack.append(nm.group(1).decode("utf-8", "replace") if nm else tag.decode())
    return out


# 真正的 telnet 登录凭据在这条路径下（见 parse_param_xml 的注释）
SERVICE_MANAGE = "X_CT-COM_ServiceManage"


def extract_telnet_info(param_bytes, firmware_version=""):
    """从 param.pdt.enc 二进制里直接抽 telnet 账号/密码/开关/端口。

    返回 dict：{"TelnetUserName":…, "TelnetPassword":…, "TelnetEnable":…, "TelnetPort":…}
    缺失的键不会出现。**只认 X_CT-COM_ServiceManage 下的那一份**，因为
    Lan. 和 TelnetAccount.0. 下的是别的用途。
    """
    if not param_bytes.startswith(CIPHER_MAGIC):
        # 允许直接喂 UCI 文本（pre_usrconfig_conf）
        return _from_uci_text(param_bytes, firmware_version)

    key = _pick_key(firmware_version)
    plain = _aes_cbc_decrypt(key[0], key[1], param_bytes[4:])
    if not plain.lstrip().startswith(b"<"):
        raise DecryptError("param.pdt.enc 解密后不是 XML（密钥或版本不对）")
    params = parse_param_xml(plain)

    wanted = ("TelnetUserName", "TelnetPassword", "TelnetEnable", "TelnetPort")
    out = {}
    for name in wanted:
        # 优先 ServiceManage 直属的那一份（路径里不含 .Lan. 或 .TelnetAccount.）
        best = None
        for path, value in params.items():
            if not path.endswith("." + name) and path != name:
                continue
            if SERVICE_MANAGE not in path:
                continue
            if ".Lan." in path or ".TelnetAccount." in path:
                continue
            best = value
            break
        if best is None:
            # 退一步：任何 ServiceManage 下的同名项
            for path, value in params.items():
                if (path.endswith("." + name) or path == name) and SERVICE_MANAGE in path:
                    best = value
                    break
        if best is not None:
            out[name] = best
    return out


def _from_uci_text(data, firmware_version=""):
    """从 `option TelnetPassword '2506…'` 这种 UCI 文本里抽并解密。"""
    if isinstance(data, bytes):
        text = data.decode("utf-8", "replace")
    else:
        text = data
    out = {}
    for m in re.finditer(r"option\s+(Telnet\w+)\s+'([^']*)'", text):
        name, val = m.group(1), m.group(2)
        if val.startswith("25060000"):
            try:
                out[name] = decrypt_value_auto(val, firmware_version)[0]
            except DecryptError:
                out[name] = val  # 解不了就原样给出，方便人工判断
        else:
            out[name] = val
    return out


def _pick_key(firmware_version):
    for pat, k2, iv2, note in FIRMWARE_KEYS:
        if not firmware_version or re.search(pat, firmware_version):
            return bytes.fromhex(k2), bytes.fromhex(iv2)
    raise DecryptError(
        f"固件版本 {firmware_version!r} 不在密钥表里：\n{describe_keys()}"
    )


# ---------------------------------------------------------------------------
# 命令行自测
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法：")
        print("  python3 fhdecrypt.py <param.pdt.enc 或 pre_usrconfig_conf>")
        print("  python3 fhdecrypt.py --value 2506000049ECBE32...")
        print()
        print("已知密钥：")
        print(describe_keys())
        sys.exit(0)

    if sys.argv[1] == "--value":
        print(decrypt_value_auto(sys.argv[2])[0])
        sys.exit(0)

    blob = open(sys.argv[1], "rb").read()
    if blob.startswith(CIPHER_MAGIC) and len(blob) > 1000:
        info = extract_telnet_info(blob)
        for k, v in info.items():
            print(f"{k} = {v}")
    else:
        off, chunk = find_param_pdt_enc(blob)
        if chunk is None:
            print("没找到 param.pdt.enc，按 UCI 文本解析：")
            for k, v in _from_uci_text(blob).items():
                print(f"{k} = {v}")
        else:
            print(f"在偏移 {off:#x} 找到 param.pdt.enc（{len(chunk)} 字节）")
            for k, v in extract_telnet_info(chunk).items():
                print(f"{k} = {v}")
