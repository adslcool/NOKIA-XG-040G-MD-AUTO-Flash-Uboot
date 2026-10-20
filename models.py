#!/usr/bin/env python3
"""机型注册表：不同光猫厂商/型号的差异集中在这里。

为什么单独一个模块（而不是散在 flash.py 的配置区）：
  - GUI 要按机型切换账号密码默认值、提示文案、可用功能；
  - 命令行也要能 `--model hg5585f` 选；
  - 两边必须用同一份定义，否则迟早对不上。

每种机型提供两类信息：
  1. **静态档案**（MODELS 里的字段）：管理地址、端口、默认账号密码、
     提权方式、是否需要 FTP、分区表来源……这些是「这台机器长什么样」。
  2. **行为开关**（capabilities）：某些功能只对部分机型有意义
     （比如贝尔的 tcboot 刷写只对 econet en7581 有效）。

字段说明见 Model 的 docstring。新增机型只需在 MODELS 里加一条。
"""
import os

# ---------- 机型 id（命令行 / config.ini 里用的短名）----------
BEL = "bel"          # 贝尔 / 上海诺基亚贝尔（NBEL）XG-040G-MD、XG-140G-MD
HG5585F = "hg5585f"  # 烽火 FiberHome HG5585F CT（电信）
DEFAULT_MODEL = BEL


class Model:
    """一种机型的全部差异点。

    属性分组：
      - 网络：web_port、router_ip、web_scheme
      - 网页超级账号：super_user、super_password、web_login_path
      - telnet：telnet_port、telnet_user、telnet_password、
                telnet_password_from_mac（True 时密码由 MAC 后 6 位推导）
      - 提权：su_user、su_password、su_password_from_mac、need_ftp_for_su
      - 功能：supports_web_telnet（能否网页一键开 telnet）、
              supports_stock2ubi、supports_uboot_flash
    """

    def __init__(self, key, name, short, **kw):
        self.key = key
        self.name = name            # 界面上显示的完整名
        self.short = short          # 日志里用的简称
        # 网络
        self.router_ip = kw.get("router_ip", "192.168.1.1")
        self.web_port = kw.get("web_port", 80)
        self.web_scheme = kw.get("web_scheme", "http")
        # 网页超级账号
        self.super_user = kw.get("super_user", "")
        self.super_password = kw.get("super_password", "")
        # telnet
        self.telnet_port = kw.get("telnet_port", 23)
        self.telnet_user = kw.get("telnet_user", "")
        self.telnet_password = kw.get("telnet_password", "")
        self.telnet_password_from_mac = kw.get("telnet_password_from_mac", False)
        self.telnet_password_prefix = kw.get("telnet_password_prefix", "")
        # 是否额外试「固定密码 + MAC 后 6 位」这种流传写法。
        # 烽火两种说法都在网上流传（固定值 / 固定值+MAC 后缀），而固件证据
        # 只支持固定值。两个都试最多烧 2 次认证机会（3 次才锁），比让用户
        # 自己猜、猜错锁 300 秒划算。
        self.telnet_password_try_mac_suffix = kw.get(
            "telnet_password_try_mac_suffix", False)
        # 提权
        self.su_user = kw.get("su_user", "root")
        self.su_password = kw.get("su_password", "")
        self.su_password_from_mac = kw.get("su_password_from_mac", False)
        self.su_password_prefix = kw.get("su_password_prefix", "")
        # 该机型提权是否必须先开 FTP（贝尔：user_ftp 只在 FTP 启用后存在）
        self.need_ftp_for_su = kw.get("need_ftp_for_su", False)
        # 提权账号是否跟随 telnet 账号（贝尔：user → user_ftp）。
        # 烽火的 su 目标固定是 root，不跟随。
        self.su_user_follows_telnet = kw.get(
            "su_user_follows_telnet", self.need_ftp_for_su)
        # 备份传输方式：curl=设备侧 curl 上传到主机的临时 HTTP 服务器；
        # tftp=设备侧 busybox tftp 推到主机的临时 TFTP 服务器；
        # auto=先探测 curl，没有才退 tftp（默认，最稳）。
        self.backup_transport = kw.get("backup_transport", "auto")
        # telnet 开启方式：web=网页一键开（贝尔）、fhtool=用 fh_tool RPC 开
        # （烽火，无需登录）、manual=只能手动开
        self.telnet_enable = kw.get("telnet_enable", "web")
        # 功能开关
        self.supports_web_telnet = kw.get("supports_web_telnet",
                                          self.telnet_enable == "web")
        self.supports_stock2ubi = kw.get("supports_stock2ubi", False)
        self.supports_uboot_flash = kw.get("supports_uboot_flash", False)
        # 刷机流程家族：决定 GUI 用哪个 tab、以及「备份」和「刷 U-Boot」
        # 是两步还是一步。贝尔 = "bel"（tcboot.bin 写 mtd0，只适配移动版）；
        # 烽火/其他 AN7581 = "fh"（备份与刷写都走 stock2ubi 网页，合成一步）。
        self.family = kw.get("family", "bel")
        # 说明文案
        self.note = kw.get("note", "")

    def __repr__(self):
        return f"<Model {self.key} {self.name}>"


# ---------- 机型定义 ----------
MODELS = {
    BEL: Model(
        BEL,
        name="贝尔 XG-040G-MD / XG-140G-MD（移动版）",
        short="贝尔",
        router_ip="192.168.1.1",
        web_port=80,
        super_user="CMCCAdmin",
        super_password="aDm8H%MdA",
        telnet_user="user",
        telnet_password="j3ba73@8",
        # 贝尔的 user_ftp 只在 FTP 服务启用后才被 IGD FTP Server 动态注入，
        # 所以提权前必须先开 FTP。这是贝尔机型最特别的一点。
        su_user="user_ftp",
        su_password="",          # 留空 = 与 telnet 密码相同
        need_ftp_for_su=True,
        backup_transport="curl",
        telnet_enable="web",
        supports_web_telnet=True,
        supports_stock2ubi=True,
        supports_uboot_flash=True,
        family="bel",
        note="刷 U-Boot 走 tcboot.bin（写 mtd0）或 an758x-stock2ubi。",
    ),
    HG5585F: Model(
        HG5585F,
        name="烽火 HG5585F CT（电信版）",
        short="烽火",
        router_ip="192.168.1.1",
        # 烽火的管理网页在 8080；80 是另一个（电信智能网关）页面。
        web_port=8080,
        super_user="telecomadmin",
        super_password="nE7jA%5m",
        # telnet 账号固定 telnetadmin。
        # 密码是**固定值 FH-nE7jA%5m，不带 MAC 后缀** —— 这一条是从固件里
        # 解出来的，不是从网上教程抄的：
        #   · 省级预置包 fhrom/preconfig/FH_HGU_CH_CT_Zhejiang/pre_usrconfig_conf
        #     里 TelnetPassword 是密文 2506000049ECBE32C8DE760F0E960B0F0A53953F，
        #     用固件自带的 libfhcfg.so:fhcfg_dencryp_string_with_mode 解出来
        #     就是明文 FH-nE7jA%5m；
        #   · 设备上的 /var/tel_passwd 里 telnetadmin 的 $5$ 哈希用
        #     `openssl passwd -5 -salt fh 'FH-nE7jA%5m'` 可精确复现；
        #   · 真机 telnetadmin / FH-nE7jA%5m 实测登录成功。
        # 网上教程写的「FH-nE7jA%5m + MAC 后 6 位」在 V4.20.M5000 上不成立，
        # 若遇到别的固件版本登录不上，可在 config.ini 里手工覆盖这个密码。
        telnet_user="telnetadmin",
        telnet_password="FH-nE7jA%5m",
        telnet_password_from_mac=False,
        # 网上另有一派说法是「FH-nE7jA%5m + MAC 后 6 位」。固件证据只支持
        # 固定值，但不同省/不同固件版本确实可能不同，所以固定值失败后
        # 再试一次带后缀的写法（最多 2 次，不会触发 3 次锁 300 秒）。
        telnet_password_try_mac_suffix=True,
        # su root，密码 = Fh@ + MAC 后 6 位大写
        su_user="root",
        su_password_from_mac=True,
        su_password_prefix="Fh@",
        # 烽火不需要开 FTP 就能 su root（与贝尔相反）
        need_ftp_for_su=False,
        # 备份走 an758x-stock2ubi 自带的 HTTP 服务（主机主动连设备的 3333，
        # 属于出站连接，不受主机防火墙入站规则影响；实测 8~11 MB/s，
        # 比 tftp 快一个数量级）。工具会自动把 stock2ubi 上传并跑起来。
        # 之所以不用 curl：烽火的 busybox 根本没有 curl（实测只有 wget，
        # 而 wget 不支持 --post-data，只能下载不能上传）；tftp 虽然能用，
        # 但要主机 root 绑 69 端口，且入站 UDP 常被 firewalld 挡掉。
        backup_transport="stock2ubi",
        # 网页里没有一键开 telnet 的接口，但 8080 上的 fh_tool RPC 可以直接开
        telnet_enable="fhtool",
        supports_web_telnet=False,
        supports_stock2ubi=True,
        supports_uboot_flash=False,  # 刷 U-Boot 走 stock2ubi 网页，不写 tcboot
        family="fh",
        note="开 telnet 走 8080 的 fh_tool 加密 RPC（无需登录后台）；"
             "刷 U-Boot 走 an758x-stock2ubi 网页。",
    ),
}


def mac_required(model):
    """该机型是否必须填 MAC（密码由 MAC 推导时为真）。"""
    return bool(model.telnet_password_from_mac or model.su_password_from_mac)


def display_name(model):
    return model.name


def by_short(short):
    """按界面上的简称找机型（下拉框用完整名，但日志里用简称）。"""
    for m in MODELS.values():
        if m.short == short:
            return m
    return None


def get(key):
    """按 id 取机型；未知 id 退回默认机型（不让程序因为一个拼写错误起不来）。"""
    return MODELS.get(key or DEFAULT_MODEL, MODELS[DEFAULT_MODEL])


def all_models():
    """按「默认机型在前」的顺序返回所有机型，给 GUI 下拉框用。"""
    order = [DEFAULT_MODEL] + [k for k in MODELS if k != DEFAULT_MODEL]
    return [MODELS[k] for k in order]


def keys():
    return list(MODELS.keys())


# ---------- 由 MAC 推导密码 ----------
def mac_suffix(mac):
    """取 MAC 后 6 位并转大写。

    烽火的规则是「机器背面 MAC 的后六位，大写」。
    接受 70:D5:1E:04:6C:EC / 70-D5-1E-04-6C-EC / 70D51E046CEC 三种写法。
    """
    digits = "".join(ch for ch in (mac or "") if ch.isalnum()).upper()
    if len(digits) < 6:
        return ""
    return digits[-6:]


def normalize_mac(mac):
    """去掉分隔符并转大写，得到 12 位纯十六进制（fh_tool 的加密要用这个形式）。"""
    digits = "".join(ch for ch in (mac or "") if ch.isalnum()).upper()
    return digits


def derive_telnet_password(model, mac):
    """按机型规则推导 telnet 密码。

    静态密码机型（贝尔）直接返回配置值；烽火返回 前缀 + MAC后6位。
    MAC 不合法时返回空串，让调用方决定是报错还是让用户手填。
    """
    if not model.telnet_password_from_mac:
        return model.telnet_password
    suffix = mac_suffix(mac)
    if not suffix:
        return ""
    return f"{model.telnet_password_prefix}{suffix}"


def derive_su_password(model, mac):
    """按机型规则推导 su 密码（静态密码机型返回配置值）。"""
    if not model.su_password_from_mac:
        return model.su_password
    suffix = mac_suffix(mac)
    if not suffix:
        return ""
    return f"{model.su_password_prefix}{suffix}"


def guess_from_mac(mac):
    """根据 MAC 猜机型？做不到 —— 两个厂商的 MAC 前缀不唯一。

    留这个函数是为了明确「不要猜」：机型必须由用户选或由设备探测，
    猜错了会把错误密码发出去，白白消耗认证机会。
    """
    return None
