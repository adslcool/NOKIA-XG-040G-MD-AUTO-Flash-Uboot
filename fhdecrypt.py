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

import hashlib
import re
import struct

__all__ = [
    "DecryptError",
    "CIPHER_KEYS",
    "ALL_MAGICS",
    "FIRMWARE_KEYS",
    "decrypt_value",
    "decrypt_value_auto",
    "parse_param_xml",
    "find_param_pdt_enc",
    "extract_telnet_info",
    "describe_keys",
    "md5_crypt",
    "verify_md5_crypt",
    "sha256_crypt",
    "verify_sha256_crypt",
    "verify_password_hash",
    "parse_passwd_file",
    "match_password_hash",
]


class DecryptError(Exception):
    """解密失败（密钥不对、格式不对、或固件版本不在表里）。"""


# ---------------------------------------------------------------------------
# 密钥表
# ---------------------------------------------------------------------------
# 密钥按**容器头**索引，不是按固件版本。
#
# 早先我按固件版本索引，结果在另一台机器上翻车：同一份固件、同一份 calidata，
# 有的省份预置包用的是 `2506` 容器，有的用的是 `2502` 容器 —— 两者用**不同的
# 工作密钥**，但都是 AES-256-CBC + PKCS#7。所以版本号根本不是区分依据，
# 容器头那 4 个字节才是。
#
# 这两把密钥都不在 libfhcrypto_new.so 里（搜遍整个 .so 没有），也不是从
# calidata 明文直接推导出来的 —— 它们是运行时由 `ks_wk_pdt_get_by_id`
# 拿静态 blob 经 AES 解出来的。所以只能把结果硬编码在这里，每遇到一个新
# 容器头就往表里加一条。
#
# 怎么补一条新的：用 qemu 跑固件的 `lib/libfhcfg.so` 解密入口，配合
# LD_PRELOAD 拦 `wolfSSL_EVP_CipherInit_ex`，能直接抓到 key(32)/iv(16)。
CIPHER_KEYS = {
    # 浙江/安徽预置包、HG5585F 两台真机的 param.pdt.enc 都是这一套
    b"\x25\x06\x00\x00": (
        "d11683d02a88b86c6ce50e8514fffeefc2e4b339b40ac7de12945fe1fa79720a",
        "22f941441cbff9ed83637e8125c12920",
        "2506",
    ),
    # 这台机器（MAC 70:D5:1E:04:6C:EC）的 pre_usrconfig_conf 用的是这一套
    b"\x25\x02\x00\x00": (
        "818ae275b20863ce83af0cc0fec38c71456d6ecc215662a9b293ce0e9fb50303",
        "747d6633c5f15eac087231efd007037c",
        "2502",
    ),
}

# 默认容器头（param.pdt.enc 用这个）
CIPHER_MAGIC = b"\x25\x06\x00\x00"

# 所有已知容器头，用于「找密文」时逐个试
ALL_MAGICS = tuple(CIPHER_KEYS)

# 兼容旧名字：外部代码/文档里可能还在引用 FIRMWARE_KEYS
FIRMWARE_KEYS = [
    (r".*", CIPHER_KEYS[CIPHER_MAGIC][0], CIPHER_KEYS[CIPHER_MAGIC][1], "2506 容器"),
]

# param.pdt.enc 的大小（用于在 rootfs 镜像里定位）
PARAM_PDT_ENC_SIZE = 1770484


def describe_keys():
    """返回密钥表的可读说明（给 --selftest / 日志用）。"""
    lines = []
    for magic, (k2, iv2, note) in CIPHER_KEYS.items():
        lines.append(f"  {magic.hex()}  k2={k2[:16]}… iv2={iv2[:8]}…  {note} 容器")
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
def _split_magic(raw):
    """把 `25060000<hex>` 拆成 (容器头, 密文 body)。

    不是已知容器头就抛 DecryptError —— **绝不要把密文当明文返回**，
    那会让调用方拿着 34 个 hex 字符去登录，白白烧掉认证次数。
    """
    if len(raw) < 4:
        raise DecryptError(f"太短，不是密文：{raw.hex()}")
    magic = raw[:4]
    if magic not in CIPHER_KEYS:
        raise DecryptError(
            f"不认识的容器头 {magic.hex()}（已知："
            + "、".join(m.hex() for m in CIPHER_KEYS)
            + "）"
        )
    return magic, raw[4:]


def decrypt_value(cipher_hex, key_hex=None, iv_hex=None, has_magic=True):
    """解一个 UCI 里形如 `25060000<32 位 hex>` 的值。

    key_hex/iv_hex 不传就按容器头自动选（推荐）。has_magic=False 时把整个
    输入都当密文，此时必须显式给 key/iv。
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
        magic, body = _split_magic(raw)
        if key_hex is None:
            key_hex, iv_hex, _ = CIPHER_KEYS[magic]
    else:
        body = raw
        if key_hex is None or iv_hex is None:
            raise DecryptError("has_magic=False 时必须显式给 key 和 iv")

    return _aes_cbc_decrypt(bytes.fromhex(key_hex), bytes.fromhex(iv_hex), body)


def decrypt_value_auto(cipher_hex, firmware_version=""):
    """按容器头挑密钥解密，返回 (明文, 命中的说明)。

    firmware_version 参数只为向后兼容保留 —— 密钥是按容器头选的，与固件
    版本无关（同一版本的不同省份预置包会用不同容器）。
    """
    if isinstance(cipher_hex, str):
        s = cipher_hex.strip().strip("'\"")
        try:
            raw = bytes.fromhex(s[2:] if s[:2].lower() == "0x" else s)
        except ValueError:
            raise DecryptError(f"不是合法的十六进制串：{cipher_hex[:40]}…")
    else:
        raw = bytes(cipher_hex)

    magic, body = _split_magic(raw)
    key_hex, iv_hex, note = CIPHER_KEYS[magic]
    plain = _aes_cbc_decrypt(bytes.fromhex(key_hex), bytes.fromhex(iv_hex), body)
    try:
        text = plain.decode("utf-8")
    except UnicodeDecodeError:
        raise DecryptError(f"容器头 {magic.hex()} 解出来不是 UTF-8（密钥不对）")
    if text and not all(0x20 <= ord(ch) <= 0x7E for ch in text):
        raise DecryptError(f"容器头 {magic.hex()} 解出来的不是可打印文本：{text[:20]!r}")
    return text, f"{note} 容器"


# ---------------------------------------------------------------------------
# param.pdt.enc
# ---------------------------------------------------------------------------
def find_param_pdt_enc(blob):
    """在 rootfs 镜像（squashfs）或裸文件里定位 param.pdt.enc。

    做法：找任意**已知容器头**开头、且按对应密钥能解出 XML 的那一段。
    返回 (起始偏移, 内容) 或 (None, None)。

    为什么不按固定偏移：不同机型的 rootfs 布局不一样，硬编码偏移必然翻车。
    为什么试所有容器头：同一份固件里 `param.pdt.enc` 是 2506，但个别省份的
    `pre_usrconfig_conf` 是 2502，两者都要能认。
    """
    body_len = PARAM_PDT_ENC_SIZE - 4
    for magic in ALL_MAGICS:
        pos = 0
        while True:
            pos = blob.find(magic, pos)
            if pos < 0:
                break
            chunk = blob[pos:pos + PARAM_PDT_ENC_SIZE]
            if len(chunk) >= 4 + 16:
                # 只试第一块，能解出 `<!--` 就认定命中（省时间）
                try:
                    key_hex, iv_hex, _ = CIPHER_KEYS[magic]
                    head = _aes_cbc_decrypt(
                        bytes.fromhex(key_hex), bytes.fromhex(iv_hex), chunk[4:4 + 16]
                    )
                    if head.startswith(b"<") and len(chunk) >= body_len:
                        return pos, chunk[:PARAM_PDT_ENC_SIZE]
                except (DecryptError, Exception):
                    pass
            pos += 1
    return None, None


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
    if len(param_bytes) < 4 or param_bytes[:4] not in CIPHER_KEYS:
        # 允许直接喂 UCI 文本（pre_usrconfig_conf）
        return _from_uci_text(param_bytes, firmware_version)

    magic = param_bytes[:4]
    key = _pick_key(firmware_version, magic)
    plain = _aes_cbc_decrypt(key[0], key[1], param_bytes[4:])
    if not plain.lstrip().startswith(b"<"):
        raise DecryptError("param.pdt.enc 解密后不是 XML（密钥或容器头不对）")
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
    """从 `option TelnetPassword '2506…'` 这种 UCI 文本里抽并解密。

    **解不了就不要给值**。早期版本在解密失败时把密文原样写进结果，导致
    调用方拿着 34 个 hex 字符当密码去登录 telnet —— 白白消耗认证次数，
    而且看起来像「密码找错了」。
    """
    if isinstance(data, bytes):
        text = data.decode("utf-8", "replace")
    else:
        text = data
    out = {}
    for m in re.finditer(r"option\s+(Telnet\w+)\s+'([^']*)'", text):
        name, val = m.group(1), m.group(2)
        if not val:
            continue
        if _looks_like_cipher(val):
            try:
                out[name] = decrypt_value_auto(val, firmware_version)[0]
            except DecryptError:
                # 有密文但解不了：宁可缺这个键，也不要返回密文
                continue
        else:
            out[name] = val
    return out


def _looks_like_cipher(val):
    """判断一个 UCI 值是不是密文（已知容器头 + 纯 hex）。"""
    if not isinstance(val, str):
        return False
    s = val.strip().strip("'\"")
    if s[:2].lower() == "0x":
        s = s[2:]
    if len(s) < 36 or len(s) % 2:
        return False
    try:
        raw = bytes.fromhex(s)
    except ValueError:
        return False
    return raw[:4] in CIPHER_KEYS


# ---------------------------------------------------------------------------
# md5-crypt（$1$）—— 用来在**本地**核对 telnet 密码，不碰 telnet
# ---------------------------------------------------------------------------
# 为什么需要：设备上的 `/var/tel_passwd` 是 `用户名:哈希:...` 格式，哈希由
# libcrypt 的 `crypt()` 生成。把它免登录取回来，就能在本地把候选密码算成哈希
# 逐个比对 —— 对上了说明密码肯定正确，一次 telnet 认证机会都不浪费；
# 全对不上才去解固件里的密码。
#
# Python 3.13 起标准库移除了 `crypt` 模块，所以这里自带一份 md5-crypt 实现
# （算法出自 FreeBSD crypt-md5.c，公开规范，不依赖任何第三方包）。
_MD5_CRYPT_MAGIC = b"$1$"
_MD5_CRYPT_ALPHABET = (
    "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
)


def _to64(value, length):
    """md5-crypt 的 64 进制编码（低位先出，字符表见上）。"""
    out = []
    for _ in range(length):
        out.append(_MD5_CRYPT_ALPHABET[value & 0x3F])
        value >>= 6
    return "".join(out)


def md5_crypt(password, salt):
    """按 $1$（md5-crypt）算法算哈希，返回完整的 `$1$salt$hash`。

    password / salt 都可以是 str 或 bytes。salt 只用前 8 个字符。
    """
    if isinstance(password, str):
        password = password.encode("utf-8")
    if isinstance(salt, str):
        salt = salt.encode("utf-8")
    salt = salt[:8]

    # 第一步：ctx = MD5(pw + magic + salt + 若干份 MD5(pw + salt + pw))
    mixin = hashlib.md5(password + salt + password).digest()
    ctx = hashlib.md5(password + _MD5_CRYPT_MAGIC + salt)
    pl = len(password)
    while pl > 0:
        ctx.update(mixin[: min(pl, 16)])
        pl -= 16
    # 「奇怪的一步」：按密码长度的二进制位追加 0x00 或密码首字节
    i = len(password)
    while i:
        ctx.update(b"\x00" if (i & 1) else password[:1])
        i >>= 1
    final = ctx.digest()

    # 第二步：跑 1000 轮加强
    for i in range(1000):
        ctx1 = hashlib.md5()
        if i & 1:
            ctx1.update(password)
        else:
            ctx1.update(final)
        if i % 3:
            ctx1.update(salt)
        if i % 7:
            ctx1.update(password)
        if i & 1:
            ctx1.update(final)
        else:
            ctx1.update(password)
        final = ctx1.digest()

    # 第三步：按固定顺序把 16 字节重排成 22 个字符
    encoded = (
        _to64((final[0] << 16) | (final[6] << 8) | final[12], 4)
        + _to64((final[1] << 16) | (final[7] << 8) | final[13], 4)
        + _to64((final[2] << 16) | (final[8] << 8) | final[14], 4)
        + _to64((final[3] << 16) | (final[9] << 8) | final[15], 4)
        + _to64((final[4] << 16) | (final[10] << 8) | final[5], 4)
        + _to64(final[11], 2)
    )
    return "$1$%s$%s" % (salt.decode("latin1"), encoded)


def verify_md5_crypt(password, hashed):
    """候选密码与 `$1$salt$hash` 是否匹配。hashed 不是 md5-crypt 就返回 False。"""
    if not hashed or not isinstance(hashed, str):
        return False
    if not hashed.startswith("$1$"):
        return False
    parts = hashed.split("$")
    # ["", "1", salt, hash]
    if len(parts) < 4:
        return False
    salt = parts[2]
    try:
        return md5_crypt(password, salt) == hashed
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# sha256-crypt（$5$）—— 烽火 HG5585F 实际用的就是这个，不是 md5-crypt
# ---------------------------------------------------------------------------
# 实测设备上的 `/var/tel_passwd` 是：
#   telnetadmin:$5$fh$bg0a9bk15PO3Ngg4aEbKF9SeXypmpk.pEpClSXdumcB:100:100:...
# 早期版本只实现了 md5-crypt，遇到 `$5$` 直接放弃比对，于是退化成「在 telnet
# 会话里一个个试」—— 正是用户指出的问题。这里补上 sha256-crypt。
#
# 算法出自 Ulrich Drepper 的公开规范（SHA-crypt），与 `openssl passwd -5`
# 逐字节一致（已用 `openssl passwd -5 -salt fh 'FH-nE7jA%5m'` 交叉验证）。
_SHA_CRYPT_ALPHABET = (
    "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
)


def _sha_crypt_b64(b2, b1, b0, n):
    """SHA-crypt 的 64 进制编码（低位先出）。"""
    w = (b2 << 16) | (b1 << 8) | b0
    out = []
    for _ in range(n):
        out.append(_SHA_CRYPT_ALPHABET[w & 0x3F])
        w >>= 6
    return "".join(out)


def sha256_crypt(password, salt, rounds=5000):
    """生成 `$5$salt$hash`。rounds 为 5000 时省略 rounds= 段（默认值）。"""
    if isinstance(password, str):
        password = password.encode("utf-8")
    if isinstance(salt, str):
        salt = salt.encode("utf-8")
    salt = salt[:16]

    # B = SHA256(password + salt + password)
    b = hashlib.sha256(password + salt + password).digest()

    # A = SHA256(password + salt + <B 重复到 len(password)> + <按位选的 B/password>)
    ctx = hashlib.sha256()
    ctx.update(password)
    ctx.update(salt)
    cnt = len(password)
    while cnt > 32:
        ctx.update(b)
        cnt -= 32
    ctx.update(b[:cnt])
    cnt = len(password)
    while cnt:
        ctx.update(b if (cnt & 1) else password)
        cnt >>= 1
    a = ctx.digest()

    # DP = SHA256(password * len(password))，再按 len(password) 截成 P
    dp = hashlib.sha256()
    for _ in range(len(password)):
        dp.update(password)
    dp = dp.digest()
    p = b""
    cnt = len(password)
    while cnt > 32:
        p += dp
        cnt -= 32
    p += dp[:cnt]

    # DS = SHA256(salt * (16 + A[0]))，再按 len(salt) 截成 S
    ds = hashlib.sha256()
    for _ in range(16 + a[0]):
        ds.update(salt)
    ds = ds.digest()
    s = b""
    cnt = len(salt)
    while cnt > 32:
        s += ds
        cnt -= 32
    s += ds[:cnt]

    # 5000 轮
    c = a
    for r in range(rounds):
        h = hashlib.sha256()
        h.update(p if (r & 1) else c)
        if r % 3:
            h.update(s)
        if r % 7:
            h.update(p)
        h.update(c if (r & 1) else p)
        c = h.digest()

    out = (
        _sha_crypt_b64(c[0], c[10], c[20], 4)
        + _sha_crypt_b64(c[21], c[1], c[11], 4)
        + _sha_crypt_b64(c[12], c[22], c[2], 4)
        + _sha_crypt_b64(c[3], c[13], c[23], 4)
        + _sha_crypt_b64(c[24], c[4], c[14], 4)
        + _sha_crypt_b64(c[15], c[25], c[5], 4)
        + _sha_crypt_b64(c[6], c[16], c[26], 4)
        + _sha_crypt_b64(c[27], c[7], c[17], 4)
        + _sha_crypt_b64(c[18], c[28], c[8], 4)
        + _sha_crypt_b64(c[9], c[19], c[29], 4)
        + _sha_crypt_b64(b"\0"[0], c[31], c[30], 3)
    )
    prefix = "$5$" if rounds == 5000 else f"$5$rounds={rounds}$"
    return f"{prefix}{salt.decode('utf-8', 'replace')}${out}"


def verify_sha256_crypt(password, hashed):
    """候选密码与 `$5$[rounds=N$]salt$hash` 是否匹配。"""
    if not hashed or not isinstance(hashed, str) or not hashed.startswith("$5$"):
        return False
    parts = hashed.split("$")
    # ["", "5", (rounds=N), salt, hash]
    if len(parts) < 4:
        return False
    rounds = 5000
    idx = 2
    if parts[2].startswith("rounds="):
        try:
            rounds = int(parts[2][7:])
        except ValueError:
            return False
        idx = 3
    if len(parts) < idx + 2:
        return False
    salt = parts[idx]
    try:
        return sha256_crypt(password, salt, rounds) == hashed
    except Exception:  # noqa: BLE001
        return False


def verify_password_hash(password, hashed):
    """按哈希类型自动选择实现（`$1$` md5-crypt / `$5$` sha256-crypt）。"""
    if not hashed or not isinstance(hashed, str):
        return False
    if hashed.startswith("$1$"):
        return verify_md5_crypt(password, hashed)
    if hashed.startswith("$5$"):
        return verify_sha256_crypt(password, hashed)
    return False


def _unwrap_tar(data):
    """如果 data 是 tar 包，返回里面第一个普通文件的内容；否则原样返回。

    **为什么必须处理**：`fh_tool` 的 `DownloadFile` 接口返回的不是裸文件，
    而是把文件打成 tar 再吐出来（实测 `/var/tel_passwd` 的响应开头就是
    `var/tel_passwd\\0\\0…0000644…ustar`）。早期版本直接把 tar 字节丢给
    parse_passwd_file，解析出的「用户名」是整个 tar 头，哈希是垃圾。
    """
    if len(data) < 512:
        return data
    # tar 的 magic 在偏移 257：`ustar`
    if data[257:262] != b"ustar":
        return data
    pos = 0
    while pos + 512 <= len(data):
        header = data[pos:pos + 512]
        if header[:512] == b"\0" * 512:
            break
        name = header[:100].split(b"\0", 1)[0]
        try:
            size = int(header[124:136].split(b"\0", 1)[0].strip() or b"0", 8)
        except ValueError:
            break
        typeflag = header[156:157]
        body_start = pos + 512
        body = data[body_start:body_start + size]
        if typeflag in (b"0", b"\0", b"7") and body:
            return body
        pos = body_start + ((size + 511) // 512) * 512
        if not name:
            break
    return data


def parse_passwd_file(data):
    """解析 `用户名:哈希:uid:gid:备注:家目录:shell` 格式的 passwd 文件。

    设备上 `/var/tel_passwd`、`/var/telWan_passwd`、`/var/telsu` 都是这个格式，
    由 telnetd / protocolmgr 在启动时按配置生成。返回 {用户名: 哈希}，
    哈希不是 `$` 开头的（明文或 `x`）也会原样带出，交给调用方判断。

    自动处理 tar 包装（见 `_unwrap_tar`）。
    """
    if isinstance(data, bytes):
        data = _unwrap_tar(data)
        text = data.decode("utf-8", "replace")
    else:
        text = data or ""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        parts = line.split(":")
        if len(parts) < 2:
            continue
        user = parts[0].strip()
        hashed = parts[1].strip()
        # 用户名必须是正常标识符，否则就是没剥干净的 tar 头
        if user and re.fullmatch(r"[A-Za-z0-9_.@-]{1,32}", user):
            out[user] = hashed
    return out


def match_password_hash(candidates, hashed):
    """在候选密码里找哈希对得上的那个，返回 (密码, 说明) 或 (None, 说明)。

    这一步纯本地计算，**不消耗 telnet 认证次数** —— 比在 telnet 会话里
    一个个试要安全得多（烽火连续错 3 次就锁）。

    支持 `$1$`（md5-crypt）与 `$5$`（sha256-crypt）。烽火 HG5585F 实测用
    的是 `$5$fh$…`，早期版本只认 `$1$`，于是白白退化成在线试密码。
    """
    if not hashed:
        return None, "没有可用的哈希"
    if not hashed.startswith("$"):
        if hashed not in ("x", "*", "!"):
            return hashed, "密码文件里就是明文"
        return None, "密码文件里没有可用哈希"
    kind = hashed.split("$")[1]
    if kind not in ("1", "5"):
        return None, f"哈希类型 ${kind}$ 暂不支持本地比对（只做了 md5-crypt / sha256-crypt）"
    tried = [c for c in candidates if c]
    for cand in tried:
        if verify_password_hash(cand, hashed):
            return cand, "候选密码与设备上的哈希一致（本地比对，未连 telnet）"
    return None, f"试了 {len(tried)} 个候选密码，都与设备上的哈希对不上"


def _pick_key(firmware_version="", magic=CIPHER_MAGIC):
    """按容器头取 (key, iv)。firmware_version 参数已废弃，仅为兼容保留。"""
    if magic not in CIPHER_KEYS:
        raise DecryptError(
            f"不认识的容器头 {magic.hex()}：\n{describe_keys()}"
        )
    k2, iv2, _ = CIPHER_KEYS[magic]
    return bytes.fromhex(k2), bytes.fromhex(iv2)


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
    if len(blob) >= 4 and blob[:4] in CIPHER_KEYS and len(blob) > 1000:
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
