# 上下文交接文档

> 生成时间：2026-10-22
> 仓库：`xe5700/xg-040g-md-auto-flash-uboot`（GitHub，SSH 推送）
> 本地路径：`/mnt/g/project/jdc/贝尔XG-040G-MD`
> 当前版本：`v2.3.4`（`version.py:15`）
> HEAD：`docs: 加交接文档 HANDOFF.md，README 明确能备份完整固件`（`git log --oneline -1` 自查），工作区干净

本文档是给「新会话的自己」看的。读完它应该能直接接着干活，不需要重新摸一遍。

---

## 一、这个项目是什么

**邪恶的光猫刷机工具箱**（`xe-pon-flash-toolkit`）—— 给上海诺基亚贝尔代工的中国移动光猫
**XG-040G-MD / XG-140G-MD** 以及烽火 **HG5585F CT**（电信版）做：

1. 自动开启 Telnet（贝尔走网页 CGI，烽火走 8080 的 fh_tool 加密 RPC）
2. 自动开启 FTP（仅贝尔，`su user_ftp` 提权需要）
3. 备份全部 17 个 MTD 分区（带 `device.json` 清单 + SHA256 校验）
4. 刷写 U-Boot（贝尔走 `tcboot.bin` 写 mtd0；烽火走 an758x-stock2ubi 的网页）
5. 从 `mtd13_factory.bin` 自动生成 OpenWrt 用的 1 MiB `factory-1m.bin`
6. telnet 密码找回（免登录取设备密码文件比哈希 / 解密固件配置）
7. 导出诊断包给作者分析

**两种运行形态**：tkinter GUI（`gui.py`，两个 tab）+ 命令行（`flash.py --mode xxx`）。
两者共用 `flash.py:run_flow()`，避免刷机逻辑出现两份实现。

---

## 二、代码结构

| 文件 | 行数 | 职责 |
|---|---|---|
| `flash.py` | 3404 | 主流程。telnet/网页操作、备份、刷写、密码找回、`run_flow()` |
| `gui.py` | 1563 | tkinter 界面，贝尔/烽火两个 tab |
| `config.py` | 309 | `config.ini` 读写（`configparser(interpolation=None)`，必须） |
| `models.py` | 254 | 机型档案（`bel` / `hg5585f`） |
| `fhtool.py` | 265 | 烽火 fh_tool 加密 RPC 客户端 + MAC 探测 |
| `fhdecrypt.py` | 923 | 烽火配置密文解密 + md5-crypt/sha256-crypt + tar 解包 |
| `fhfactory.py` | 783 | 1 MiB factory 生成（移植自 GPL-2.0 的 fiberhome-factory） |
| `fhreport.py` | 560 | 诊断包生成 |
| `serial_shell.py` | 330 | TTL 串口（把串口包装成 telnet 接口）。**GUI 里已隐藏** |
| `version.py` | 41 | 版本号唯一来源 |
| `package_release.py` | 211 | 打包 zip + 从 GitHub API 取 stock2ubi |
| `changelog.py` | 213 | 从 git 历史生成更新日志 |
| `build_exe.spec` | — | PyInstaller 配置 |
| `release.sh` | — | 一条命令发版 |

### 关键入口

- `flash.py:3032 run_flow(args, confirm_flash=None)` —— 主流程
- `flash.py:3391 main()` —— CLI 入口（会 `os.chdir` 到脚本目录）
- `gui.py:1502 main()` —— GUI 入口，支持 `--selftest` / `--quiet`
- `flash.py:3336 _load_model_globals(model_key)` —— 把机型档案写进全局变量

### 运行模式

```
--mode telnet     仅开 Telnet
--mode ftp        仅开 FTP（仅贝尔）
--mode backup     默认。开 Telnet + FTP + 备份
--mode flash      备份 + 刷 U-Boot（危险，需要输 YES）
--mode stock2ubi  开 Telnet + 上传 stock2ubi + 自动备份 + 开网页
--mode report     只导出诊断包（不登录、不改设备）
```

---

## 三、机型档案（`models.py` 实测值）

### 贝尔 XG-040G-MD / XG-140G-MD（移动版）

```
family=bel  router_ip=192.168.1.1  web_port=80  telnet_port=23
telnet_user=user            telnet_password=j3ba73@8
su_user=user_ftp            su_password=""（= telnet 密码）
need_ftp_for_su=True        backup_transport=curl
telnet_enable=web           supports_web_telnet=True
supports_stock2ubi=True     supports_uboot_flash=True
super_user=CMCCAdmin        super_password=aDm8H%MdA
```

- 设备 CPU `econet,en7581`，Linux 5.4.55 aarch64，Buildroot 2021.02.3，busybox 1.16.0，**无 python**
- `user_ftp` 只在**开启 FTP 之后**由 `IGD FTP Server` 动态注入 `/etc/passwd`
- 网页是 CGI（ELF，`/webs/*.cgi`，thttpd :80），`login.cgi` 接受**明文** POST
- 单网页会话限制；`rm -f /tmp/cgi_session`（root）可立即释放

### 烽火 HG5585F CT（电信版）

```
family=fh   router_ip=192.168.1.1  web_port=8080  telnet_port=23
telnet_user=telnetadmin     telnet_password=FH-nE7jA%5m
telnet_password_try_mac_suffix=True
su_user=root                su_password 由 MAC 推导 = Fh@ + MAC 后 6 位大写
need_ftp_for_su=False       backup_transport=stock2ubi
telnet_enable=fhtool        supports_web_telnet=False
supports_stock2ubi=True     supports_uboot_flash=False（不走 tcboot）
super_user=telecomadmin     super_password=nE7jA%5m
```

- 闪存：SPI NAND，`writesize=2048` / `oobsize=64` / `erasesize=131072`（128 KiB），容量 256 MiB
- **读不到闪存厂商**（固件没暴露 `spi-nand` 节点），界面上只做「批次可能不同」的提醒
- telnet 密码**两种说法**都流传，代码两个都试（先固定值，再带 MAC 后缀）
- 设备 `/var/tel_passwd` 存的是 **sha256-crypt `$5$`**（不是 md5-crypt）

---

## 四、已经打通的关键技术（别重新踩坑）

### 4.1 烽火 MAC 免认证探测

```
GET http://192.168.1.1:8080/cgi-bin/ajax?ajaxmethod=web_get_brmad
→ {"randstr":1027784428,"brmad":"70D51E046CEC"}
```

无需任何认证。MAC 是 fh_tool 加密的密钥种子，所以「能访问 8080」= 「有权限」。
MAC 必须传**无分隔符大写**（`70D51E046CEC`），带冒号算出来密钥全错，设备只回 403。

### 4.2 fh_tool 加密 RPC（免登录）

密钥派生（`fh_tool_cli/crypto.py:derive_crypto`）：

```python
digest = sha256(mac_ascii).hexdigest()
key = "".join(digest[2*i+2] for i in range(16))   # 16 字节 ASCII
iv  = "".join(digest[3*i+3] for i in range(16))   # 16 字节 ASCII
# AES-128-CBC + PKCS#7，请求体 base64
```

端点 `/fh_tool/api`，常用 func：`GetDevInfo` / `GetAdminAccount` / `TelnetEnable` /
`GetTelnetInfo` / `DownloadFile` / `SetPreconfig`。

**重要**：`GetTelnetInfo` 在实测固件上返回的字段全是空的（`info4` 不是密码），别指望它。

**`DownloadFile` 返回的是 tar 包**（设备跑 `tar -cvf x.tar.gz`），必须先解 tar。
它只放行 `/fhconf/` 与 `/fhdata/` 目录。

能下到的（实测）：
```
/var/tel_passwd  /var/telWan_passwd  /var/telsu
/fhdata/pre_usrconfig_conf  /fhconf/usrconfig_conf
/fhdata/factory_conf  /fhdata/table
```

下不到的（一律 `FhToolUnavailable`）：`/fhrom/fhconf/param.pdt.enc`、`/etc/passwd`、
`/etc/shadow`、`/fhrom/fhconf/calidata` 等。

### 4.3 烽火配置密文解密（`fhdecrypt.py`）

**格式**：4 字节容器头 + N 字节 AES-CBC 密文（PKCS#7）。容器头不参与加密。

**密钥按容器头索引**（不是按固件版本）：

```python
# 值是三元组：(key_hex, iv_hex, 标签)
CIPHER_KEYS = {
    b"\x25\x06\x00\x00": (  # 2506：param.pdt.enc、浙江/安徽预置包
        "d11683d02a88b86c6ce50e8514fffeefc2e4b339b40ac7de12945fe1fa79720a",
        "22f941441cbff9ed83637e8125c12920", "2506"),
    b"\x25\x02\x00\x00": (  # 2502：这台机器的 pre_usrconfig_conf
        "818ae275b20863ce83af0cc0fec38c71456d6ecc215662a9b293ce0e9fb50303",
        "747d6633c5f15eac087231efd007037c", "2502"),
}
```

- **密钥是固件级、与设备无关**（两台不同 MAC 的设备 `param.pdt.enc` 逐字节相同）
- 密钥是运行时由 `libfhcrypto_new.so` 的 `ks_wk_pdt_get_by_id`（偏移 `0x9554`）
  从静态 blob 解出来的，**无法纯静态算出**，只能硬编码
- 抓取方法留档：`LD_PRELOAD=./hook.so qemu-aarch64 -L . ./dec 0 <hex>`，
  hook 拦 `wolfSSL_EVP_CipherInit_ex`（返回地址恒为 `libfhcrypto_new.so + 0x8694`）。
  `hook.c` 签名必须 `typedef int (*init_fn)(...)` 且 `real = 0`，
  编译 `aarch64-linux-gnu-gcc -shared -fPIC -o hook.so hook.c -ldl`，
  必须在 `_fw_rootfs/` 目录下跑（`lib/libfhcfg.so` 是相对路径加载）

**同一个字段名在 param.xml 里有多份，必须按完整路径区分**：

```
...X_CT-COM_ServiceManage.TelnetPassword                 = FH-nE7jA%5m   ← 真正登录用
...X_CT-COM_ServiceManage.Lan.TelnetPassword             = abcd           ← LAN 侧
...X_CT-COM_ServiceManage.TelnetAccount.0.TelnetPassword = admin          ← 多账号占位
```

`extract_telnet_info()` 只认 `X_CT-COM_ServiceManage` 下直属、不含 `.Lan.` /
`.TelnetAccount.` 的那一份。

**`param.pdt.enc` 嵌在 rootfs 分区镜像里**：`mtd3_RootfsA.bin` 偏移 `0x6af11c`，
1770484 字节，md5 `21209ddb60305df56b2fa99d0a7e3d2e`。`find_param_pdt_enc()`
用「找 magic + 试解第一块是否以 `<` 开头」定位，不硬编码偏移。

### 4.4 密码哈希（`fhdecrypt.py`）

- `md5_crypt(password, salt)` → `$1$salt$hash`，验证向量：
  `md5_crypt("1234", "") == "$1$$iC.dUsGpxNNJGeOm1dFio/"`（与固件 `/usr/etc/passwd` 里 admin 一致）
- `sha256_crypt(password, salt, rounds=5000)` → `$5$salt$hash`，验证向量：
  `sha256_crypt("FH-nE7jA%5m", "fh") == "$5$fh$bg0a9bk15PO3Ngg4aEbKF9SeXypmpk.pEpClSXdumcB"`
- `verify_password_hash(password, hashed)` 按 `$1$` / `$5$` 分派
- `parse_passwd_file(data)` 会先 `_unwrap_tar()`
- Python 3.14 已移除 `crypt` 模块，所以必须自带实现

### 4.5 telnet 登录状态机（`flash.py:_telnet_login_once`）

**烽火设备密码错一次就退回 `Login:` 提示，不是重新问 `Password:`。**
所以每换一个密码必须先重发账号。正确姿势：

```python
# at_prompt 状态机
if not at_prompt:
    tn.write(user + b"\n"); expect_any(tn, ["Password:", ...])
tn.write(pwd + b"\n")
h, t = expect_any(tn, ["#", "$", "Password:", "Login incorrect", "Login:", "forbidden"])
if h == "forbidden":  return "lockout"
if h in ("#", "$"):   return confirm_identity(tn)   # ← 只有这里才能发 id\n
if h in ("Login incorrect", "Login:"):  at_prompt = False; continue
if h == "Password:":  at_prompt = True; continue
```

**两个致命坑**：
1. 发完密码**立刻**调 `confirm_identity()` —— 它会发 `id\n`，密码错时 `id` 被当成
   下一个密码提交 → 每试一个密码烧掉**两次**认证机会，3 次就锁
2. 看到 `Login:` 就 `break` —— 后面的正确密码根本没提交，表现为「密码明明对却登不上」

### 4.6 密码找回顺序（用户明确要求，别改回原来的顺序）

```
1. 手动填的 MANUAL_TELNET_PASSWORD（最高优先级，永远不覆盖）
2. 免登录核对：
   a. DownloadFile 取 /var/tel_passwd → 解 tar → 本地算哈希比对（不消耗认证机会）
   b. 对不上 → 解密 /fhdata/pre_usrconfig_conf（2502）或 param.pdt.enc（2506）
3. 才去 telnet 在线试
```

**弱口令表 `WEAK_CREDENTIALS`（`flash.py`）只用于本地比对哈希，绝不拿去在线试。**
11 组来自论坛截图：`admin/Fh@{mac6}`、`root/Fh@{mac6}`、`telnetadmin/FH-nE7jA%5m{mac6}`、
`telnetadmin/FH-nE7jA%5m`、`telecom/nE7jA%5m`、`admin/hg2x0`、`root/hg2x0`、
`admin/admin`、`telecom/telecom`、`admin/abcd`、`root/abcd`。

### 4.7 1 MiB factory 生成（`fhfactory.py`）

原厂 `mtd13_factory.bin` 是 **3 MiB** 的 JFFS2/UBI 容器，**不能**直接喂给 U-Boot
（stock2ubi 那一栏标签是 `factory · UBI · 1.0 MiB`）。必须按
`an7581-fiberhome-ubi-parts.dtsi` 的布局重拼：

```
PON_OFFSET     0x0000  size 0x0800   APONCAL 头
WIFI_OFFSET    0x1000  size 0x1000   MT7916 EEPROM
BASE_MAC_OFFSET 0x2000 size 0x0006
PON_SN_OFFSET  0x2010  size 0x0008
DEVICE_SN_OFFSET 0x2020 size 0x0020
其余 0xff 填充
```

**Wi-Fi MAC 必须派生**（上游 commit `d802ac6`）：
`wifi_2g = base` 但第二字节 +1；`wifi_5g = wifi_2g` 但末字节 +8（溢出时 `(last-255)&0xFF`）。
实测 `F0ABF3496098` → 2.4G `F0ACF3496098`、5G `F0ACF34960A0`。

> 用户手上的 `fiberhome-factory-新.exe` 是 2026-09-14 的版本，早于上游 9-24 的派生提交，
> 所以它生成的镜像那 11 个字节是旧行为。**本工具输出是对的。**

**两套 CRC 必须区分**：
- JFFS2 用初值 **0**（内核 `crc32_le(0, ...)` 的原始寄存器值，不取反）
- UBI/UBIFS 用初值 **0xFFFFFFFF**

**AES state 装填必须按列**：`s = [list(block[4*i:4*i+4]) for i in range(4)]`。
曾误写成 `block[i::4]`（按行取），加密/解密结果全错。

### 4.8 备份通道

| 通道 | 适用 | 说明 |
|---|---|---|
| `curl` | 贝尔 | 设备 `dd \| curl -T - http://主机:8080/...` |
| `tftp` | 备用 | 需要 root 绑 69，且主机入站 UDP 常被 firewalld 挡 |
| `stock2ubi` | 烽火 | **主机主动连设备 3333**（出站，不受 firewalld 影响） |

`stock2ubi` 通道：设备跑 `an758x-stock2ubi`，监听 3333，主机
`GET http://192.168.1.1:3333/backup/{i}` 下载。实测 7~11 MB/s，17 分区 434MB 约 40 秒。

**stock2ubi 自己不做任何校验**，大小和 SHA256 都由本工具补上并写进 `device.json`。

**`Text file busy` 坑**：上传前必须先 `killall an758x-stock2ubi` 并等 3333 让出，
下载落到 `.new` 再 `mv -f`（`mv` 只改目录项，不碰旧 inode）。

**`verify_backup_complete` 按分区号比对**，不按分区名 —— 固件升级后 `apps→apps2`
这类改名会把完好备份误判成「清单缺少」。

---

## 五、最近修复的 5 个 bug（都已真机验证）

| # | 现象 | 根因 | 修法 |
|---|---|---|---|
| 1 | 解析密码文件得到一串带 NUL 的垃圾用户名 | `DownloadFile` 返回 tar 包 | 新增 `_unwrap_tar()` |
| 2 | 报「哈希类型不是 md5-crypt（5）」 | 设备用 `$5$` sha256-crypt | 新增 `sha256_crypt` / `verify_sha256_crypt` |
| 3 | 「解出了密码」但登录失败 | `2502` 容器用另一套密钥，旧代码只认 `2506` | `CIPHER_KEYS` 按容器头索引 |
| 4 | 每试一个密码烧掉两次认证机会 | `confirm_identity()` 的 `id\n` 被当密码 | 先 `expect_any` 看回显，只有 `#`/`$` 才发 `id` |
| 5 | 密码正确却登不上 | 密码错后设备退回 `Login:`，旧代码 `break` | `at_prompt` 状态机，重发账号 |

**另一个必修的**：`_from_uci_text()` 解密失败时曾把密文原样返回，上层当成密码去登录。
现在 `_looks_like_cipher()` 命中但解不出来就 `continue`，**绝不返回密文**。

---

## 六、当前状态

### 设备

**离线**（`ping 192.168.1.1` 不通，22/23/80/8080/3333 全 closed）。
上次在线时是烽火 HG5585F CT，MAC `70D51E046CEC`，SN `4A84370D51E046CEC`，
固件 `V4.20.M5000`，硬件 `V4.2.0`，区域 `Zhejiang`，编译时间 `2024-08-12 01:18:08 +0800`。

主机网卡 `enp17s0f3u3` = `192.168.1.2/24` + `192.168.0.2/24`。

### 备份目录（都在工作区，已 gitignore）

| 目录 | 设备 | 内容 |
|---|---|---|
| `backup_20261020_234343/` | 设备 A，SN `30831F0ABF3496098` | 17 分区 + `factory-1m.bin`（手工基线） |
| `backup_20261021_184307/` | 设备 B，SN `4A84370D51E046CEC` | 17 分区 + `factory-1m.bin` |
| `backup_hg558f_70d15e/` | 设备 B 的早期备份 | 11 分区，**没有 `device.json`** |

### 工作区状态

`git status` 干净。未跟踪目录（都已 gitignore）：
`_fw_rootfs/`（108M 固件根，含 `hook.so` + `dec` 抓密钥工具）、`_decwork/`、
`fiberhome-factory/`（Rust 源码，GPL-2.0）、`_webjs/`、`_recover/`、`dist/`、
`build/`、`_winbuild/`、`HG5585F-CT-Telnet/`、`esp32c3_ttl_bridge/` 等。

---

## 七、待办 / 已知问题

### 用户侧待验证（我这边做不了）

1. **用户实测刷一次**，确认 stock2ubi 网页的「板级数据」栏是否接受 1.0 MiB。
   我无法验证 —— 设备已刷成 QWRT 且无密码，另一台离线。

### 恩山发帖草稿（`恩山发帖.md:35`）—— 已改

原稿写着「完整备份流程还没有在烽火真机上跑过」「TTL 串口功能也还没有在真机上验证」，
**两条都已过时**，2026-10-22 已改为：

> 烽火 HG5585F 的开 Telnet、读账号、**全量备份（17 个分区 / 434 MB）都已在真机验证**，
> 备份会边下边算 SHA256 并写进 device.json，同一台设备（按序列号判断）已有完整备份时自动跳过。
> TTL 串口功能代码保留但界面里暂时隐藏，因为没有真机验证过。

该文件在 `.gitignore` 里，不入库。

### 未解之谜：那个 Windows 工具（`5585F-telnet.exe`）的密钥

用户贴了一张截图，工具打印 `key=5a8316b2fce41886 iv=7adadf0ffdcf9877`，
MAC 是 `68B76B13F6AC`。**用标准公式算出来是 `cd621a55191bfe01` / `262ac1dba0768921`，对不上。**

已穷举验证过、全部无命中：
- 13 种 MAC 写法（大小写/冒号/破折号/反转/去 OUI/带换行空格/加 0x）
- 11 种哈希算法 × 6 种表示（hex/HEX/b64/raw/反转）
- 从任意摘要字符串按 `step*i+off`（step 1..9，off 0..9）抽 16 字符
- UTF-16LE/BE/32 编码的 MAC
- 迭代哈希、md5-then-sha256、加盐组合（`fhtool`/`fh`/`fiberhome` 等 16 个盐）

**用户已明确说「不需要解释这个密钥了」**，所以别再去查。只留档：
这个工具最后报的是 `400 Bad Request`，说明它的请求格式或端点就不对
（我们的 `set_services` 是网页 CGI 的方法名，不是 fh_tool 的 func 名）。

### 已知限制

- **TTL 串口未在真机验证**，GUI 里已按用户要求注释掉（变量保留，`_serial_args()` 恒返回 `{}`）
- **贝尔 telnet 密码检查**用户让先放弃（m10016），只做烽火的

### 刚修掉的（2026-10-22，未发版）

`config.py:apply_model()` 原先**不写 `flash.BACKUP_TRANSPORT`**，于是逛过烽火 tab
再回贝尔时会残留 `'stock2ubi'` —— 贝尔设备上没有 an758x-stock2ubi 在跑，
备份必然连不上 3333。已在 `apply_model()` 里补 `flash.BACKUP_TRANSPORT = m.backup_transport`。
回归验证：贝尔 → 烽火 → 贝尔 往返后是 `curl / bel / 80`，`MANUAL_*` 为空、`MODEL_MAC` 为空。

---

## 八、开发工作流（务必遵守）

### 发版：只推 tag，不本地编译

用户 m10178 明确说过：「实际上你不必做编译打包吧 不是github自己会干吗 之前做了github action」

```bash
./release.sh patch -m "fix: xxx"     # 改版本号 → 提交 → 打 tag → 推送
```

CI（`.github/workflows/build.yml`）会在 push `v*` tag 时自动：
编译 Windows exe → 自检 → 打包 zip（含 exe + tcboot.bin + an758x-stock2ubi）→ 发 Release。

push master 只做打包 + 自检 + Artifact 冒烟测试，**不发 Release**。

### CI 的两道闸（别绕过）

- 产物 `< 18000000` 字节判失败（正常 19~21MB）
- `selftest.log` 里出现 `[✗]` 判失败

`gui.py:_missing_optional()` 区分「冻结环境」和「源码运行」：
frozen 时缺 `pyserial` / `fh_tool_cli` 直接算失败，源码运行时只是 `[!]` 提示。

### 本地验证

```bash
python3 -c "import ast;ast.parse(open('gui.py',encoding='utf-8').read())"   # 语法
xvfb-run -a --server-args="-screen 0 1020x900x24" python3 -u gui.py --selftest --quiet
```

GUI 冒烟测试注意：
- `log_line()` **只入队**，必须调 `_pump_log()` 才写进控件
- 子线程**不能**碰 Tk（会抛 `main thread is not in main loop`），要用 `root.after` 轮询
- 设了 tk 变量后必须调 `_save_from_ui()` 才写进 `config.ini`

### 环境

- Python 3.14.7（Arch Linux，xanmod 7.1.13，**不是 WSL**）
- `xvfb-run` / ImageMagick `import`+`magick` / `wine-11.17` / `qemu-aarch64` /
  `aarch64-linux-gnu-gcc` / `unsquashfs` 都可用
- Windows 打包用 wine + `C:\Py311`（`_winbuild/wineprefix/`）
- **`/tmp` 不跨 bash 调用保留**，中间产物必须放工作区
- 代理：`proxychains4` 可用，`/etc/proxychains.conf` 走 `http 127.0.0.1 15081`，
  但出口 IP 的 GitHub 匿名额度已耗尽（60/小时）

---

## 九、用户明确的偏好与红线

| 时间 | 要求 |
|---|---|
| m00261 | **默认不刷机**，只开 telnet + 备份；**不要测试刷机路径** |
| m01053 | 每次网页操作后都要登出 |
| m00798 | 后台任务里不要用 `sleep` |
| m01253 | 绝不尝试候选密码，只用用户密码（后来放宽为「只在本地比哈希」） |
| m09918 | 密码找回要**先下载比哈希**，对不上才解密；不要先在 telnet 里一个个试 |
| m10178 | **不要本地编译打包**，GitHub Actions 会做 |
| m09919 | 以后更新用 patch 版本号 |
| m07223 | TTL 在 GUI 里注释掉，代码保留 |
| m07994 | 重建 UBI 前没刷 preloader、启动前没刷 bl31 → **必砖**（两条红线要显眼） |
| m08515 | 刷机后**绝对不能断电**；重启后**必须**访问 `192.168.0.1` 继续安装，否则要 XMODEM 救砖 |
| m11298 | 读不到闪存品牌就算了，页面上加「不同批次可能闪存不一样」的提醒 |

---

## 十、下次接手时先做什么

1. `git log --oneline -5` + `git status` 确认状态（应该干净，HEAD 是「加交接文档」那条）
2. `ping 192.168.1.1` 看设备在不在
3. 如果要做密码相关的事：先读本文档第 4.4 / 4.5 / 4.6 节，别改回原来的顺序
4. 如果要动 factory 生成：先读第 4.7 节，`fhfactory.py` 是 GPL-2.0，改之前想清楚许可
5. 改完跑 `xvfb-run ... gui.py --selftest --quiet`，EXIT=0 才算过
6. 发版用 `./release.sh patch`，**不要本地 PyInstaller**
