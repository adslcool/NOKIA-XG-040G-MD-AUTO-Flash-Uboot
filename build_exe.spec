# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：把 GUI 打成单个 Windows exe。

用法（在装了 pyinstaller 的 Windows Python 下）：
    pyinstaller --clean --noconfirm build_exe.spec

产物：dist/XG-040G-MD工具.exe（单文件、无控制台窗口）
      用户把 tcboot.bin 放到 exe 同目录即可刷机。

⚠️ 关键点：flash.py 是**运行时用 importlib 动态加载**的（gui.py 里
spec_from_file_location），PyInstaller 静态分析 gui.py 时看不到它内部的
import。所以 flash.py 里用到的标准库必须在这里手工列进 hiddenimports，
否则打包出来的 exe 一跑就报 ModuleNotFoundError（踩过：http.server，
备份用的临时 HTTP 服务器就依赖它）。
"""
import os
import sys

block_cipher = None

# 版本号从 version.py 读（唯一来源），不要在别处再写一遍。
sys.path.insert(0, SPECPATH)
try:
    from version import VERSION, REPO, version_tuple
except Exception:
    VERSION, REPO = "0.0.0", ""
    def version_tuple():
        return (0, 0, 0)

_VT = version_tuple()

# 生成 Windows 版本资源文件：这样 exe 右键「属性 → 详细信息」里能看到版本号，
# 而不是一片空白。PyInstaller 的 version= 参数只接受文件路径，所以在这里现写。
_VERSION_INFO = f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={_VT + (0,)},
    prodvers={_VT + (0,)},
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('080404B0', [
        StringStruct('CompanyName', 'xe5700'),
        StringStruct('FileDescription', 'XG-040G-MD 光猫工具'),
        StringStruct('FileVersion', '{VERSION}'),
        StringStruct('InternalName', 'XG-040G-MD工具'),
        StringStruct('LegalCopyright', 'WTFPL v2'),
        StringStruct('OriginalFilename', 'XG-040G-MD工具.exe'),
        StringStruct('ProductName', 'XG-040G-MD 光猫工具'),
        StringStruct('ProductVersion', '{VERSION}'),
        StringStruct('Comments', 'https://github.com/{REPO}'),
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""
_VERSION_FILE = os.path.join(SPECPATH, "_version_info.txt")
with open(_VERSION_FILE, "w", encoding="utf-8") as _f:
    _f.write(_VERSION_INFO)

# flash.py 作为数据文件带进去（而不是编译进 exe）：这样用户可以直接替换
# exe 同目录的 flash.py 来改配置区，不用重新打包。
# config.py 是 gui.py 的 import 依赖，会被正常编译进 exe，不需要重复打包。
datas = [
    ("flash.py", "."),
]
# tcboot.bin 是可选的：存在就一起打包，作为「找不到外部文件」时的兜底
if os.path.isfile("tcboot.bin"):
    datas.append(("tcboot.bin", "."))
# icon.png 是窗口图标（tkinter 用）；exe 文件本身的图标用下面的 icon.ico。
# 两个都带上：ico 管资源管理器/快捷方式/任务栏，png 管运行时的窗口标题栏。
# 注意 ico 也要进 datas —— EXE(icon=...) 只把它写进 exe 的资源段，
# 那是给 Windows 看的，运行时 find_resource("icon.ico") 在解包目录里找不到它。
for _icon in ("icon.png", "icon.ico"):
    if os.path.isfile(_icon):
        datas.append((_icon, "."))

# flash.py 动态加载 → 它内部的 import 必须手工声明。
# 下面这些是 flash.py 实际用到的模块（stdlib 里 PyInstaller 不一定全收）。
FLASH_HIDDEN = [
    "telnetlib3",
    "telnetlib3.telnetlib",
    "http.server",          # 备份用的临时 HTTP 服务器（曾漏掉，exe 报错）
    "http.cookiejar",
    "urllib.request",
    "urllib.parse",
    "urllib.error",
    "posixpath",
    "base64",
    "hashlib",
    "subprocess",
    "threading",
    "socket",
    "json",
    "re",
    "time",
]

a = Analysis(
    ["gui.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=FLASH_HIDDEN,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # 明显用不到的大件，砍掉能显著减小体积
        "numpy", "pandas", "matplotlib", "PIL", "scipy",
        "PyQt5", "PySide2", "pytest",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="XG-040G-MD工具",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # GUI 程序，不要黑框
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # exe 文件自身的图标（资源管理器 / 快捷方式 / 任务栏用这个）
    icon="icon.ico" if os.path.isfile("icon.ico") else None,
    # Windows 版本资源（右键属性→详细信息），由上面的 _VERSION_FILE 生成
    version=_VERSION_FILE,
)
