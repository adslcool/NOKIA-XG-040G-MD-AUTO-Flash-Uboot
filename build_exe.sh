#!/usr/bin/env bash
# ============================================================================
# build_exe.sh —— 在 Linux / macOS 上用 wine 打包 Windows exe
#
#   ./build_exe.sh                  完整流程：准备环境 → 打包 → 自检
#   ./build_exe.sh --no-selftest    打包后不跑自检
#   ./build_exe.sh --rebuild-python 强制重装 wine 里的 Windows Python
#
# 首次运行会自动下载并安装 Windows 版 Python 到 wine 前缀（约 26MB，从
# 中科大镜像拉，比官网快很多），之后再跑就只做「打包」这一步，几秒钟。
#
# 所有工具链都放在 _winbuild/ 里，已在 .gitignore 中；重装系统后删掉
# _winbuild/ 重跑本脚本即可。
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ---------------------------- 可调参数 --------------------------------------
PYVER="3.11.9"                       # Windows Python 版本
PY_DIR_WIN='C:\Py311'                # 装到 wine 里的哪个目录
PY_EXE_WIN='C:\Py311\python.exe'     # 装好后的解释器路径
# 打包需要的第三方包。格式「pip 包名:import 名」——两者不一定同名
# （pyinstaller 的 import 名是 PyInstaller，写错了会每次都被判定缺失、
# 反复重装，实测踩过）。
PKGS=("telnetlib3:telnetlib3" "pyinstaller:PyInstaller")
EXE_NAME="XG-040G-MD工具.exe"
SELFTEST_TIMEOUT=180                 # exe 自检超时（秒）

# Python 安装包下载源：中科大镜像优先，失败回退官网
PY_INSTALLER="python-${PYVER}-amd64.exe"
PY_URLS=(
    "https://mirrors.ustc.edu.cn/python/${PYVER}/${PY_INSTALLER}"
    "https://www.python.org/ftp/python/${PYVER}/${PY_INSTALLER}"
)

WINBUILD_DIR="$SCRIPT_DIR/_winbuild"
export WINEPREFIX="$WINBUILD_DIR/wineprefix"
export WINEDEBUG="${WINEDEBUG:--all}"   # 屏蔽 wine 的调试噪音

# ---------------------------- 输出工具 --------------------------------------
log()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
ok()   { printf '\033[1;32m[✓]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\n\033[1;31m[✗] %s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------- 参数解析 --------------------------------------
usage() {
    cat <<'USAGE'
build_exe.sh —— 在 Linux / macOS 上用 wine 打包 Windows exe

  ./build_exe.sh                  完整流程：准备环境 → 打包 → 自检
  ./build_exe.sh --no-selftest    打包后不跑自检（快一点）
  ./build_exe.sh --rebuild-python 强制重装 wine 里的 Windows Python
  ./build_exe.sh --help           显示本帮助

首次运行会自动下载并安装 Windows 版 Python 到 wine 前缀（约 26MB，从中科大
镜像拉），之后再跑就只做「打包」这一步，几秒钟。

所有工具链都放在 _winbuild/ 里，已在 .gitignore 中；想从零重来就删掉
_winbuild/ 再跑本脚本。
USAGE
}

RUN_SELFTEST=1
REBUILD_PYTHON=0
for arg in "$@"; do
    case "$arg" in
        --no-selftest)    RUN_SELFTEST=0 ;;
        --rebuild-python) REBUILD_PYTHON=1 ;;
        -h|--help)        usage; exit 0 ;;
        *) die "未知参数：$arg（用 --help 看用法）" ;;
    esac
done

# ---------------------------- 0. 检查工程文件 -------------------------------
log "检查工程文件"
for f in flash.py gui.py config.py version.py build_exe.spec package_release.py; do
    [ -f "$f" ] || die "缺少 $f，请在本项目根目录运行本脚本"
done
ok "flash.py / gui.py / config.py / version.py / build_exe.spec 都在"

# ---------------------------- 1. 检查 wine ----------------------------------
log "检查 wine"
command -v wine >/dev/null 2>&1 || die "找不到 wine，请先安装（Arch: pacman -S wine / Debian: apt install wine）"
command -v wineserver >/dev/null 2>&1 || die "找不到 wineserver，wine 安装不完整"
info "wine: $(wine --version 2>/dev/null || echo '未知版本')"

# ---------------------------- 2. 准备 wine 前缀 -----------------------------
if [ ! -d "$WINEPREFIX" ]; then
    log "初始化 wine 前缀（首次运行，稍等）"
    mkdir -p "$WINBUILD_DIR"
    wineboot -i >/dev/null 2>&1 || true
    wineserver -w 2>/dev/null || true
    ok "wine 前缀已建好：$WINEPREFIX"
else
    info "复用已有 wine 前缀：$WINEPREFIX"
fi

# ---------------------------- 3. 准备 Windows Python ------------------------
have_win_python() { wine "$PY_EXE_WIN" -c "import sys" >/dev/null 2>&1; }

if [ "$REBUILD_PYTHON" = "1" ]; then
    log "按要求重装 Windows Python"
    rm -rf "$WINEPREFIX/drive_c/Py311"
fi

if have_win_python; then
    info "Windows Python 已就绪：$(wine "$PY_EXE_WIN" -c 'import sys;print(sys.version.split()[0])' 2>/dev/null)"
else
    mkdir -p "$WINBUILD_DIR"

    # 3a. 下载安装包（已存在就跳过，避免每次重下 26MB）
    if [ ! -f "$WINBUILD_DIR/$PY_INSTALLER" ]; then
        log "下载 Windows Python $PYVER"
        got=0
        for url in "${PY_URLS[@]}"; do
            info "尝试 $url"
            if curl -fL --retry 3 --connect-timeout 20 -o "$WINBUILD_DIR/$PY_INSTALLER.part" "$url"; then
                mv "$WINBUILD_DIR/$PY_INSTALLER.part" "$WINBUILD_DIR/$PY_INSTALLER"
                got=1; break
            fi
            rm -f "$WINBUILD_DIR/$PY_INSTALLER.part"
        done
        [ "$got" = "1" ] || die "Python 安装包下载失败，请手动下载 $PY_INSTALLER 放到 _winbuild/ 后重试"
    else
        info "安装包已存在，跳过下载：_winbuild/$PY_INSTALLER"
    fi

    # 3b. 静默安装
    log "静默安装 Windows Python 到 $PY_DIR_WIN"
    wine "$WINBUILD_DIR/$PY_INSTALLER" /quiet InstallAllUsers=1 \
        TargetDir="$PY_DIR_WIN" Include_test=0 Include_launcher=0 >/dev/null 2>&1 || true
    wineserver -w 2>/dev/null || true
    have_win_python || die "Windows Python 安装失败，可加 --rebuild-python 重试"
    ok "已安装：$(wine "$PY_EXE_WIN" -c 'import sys;print(sys.version)' 2>/dev/null | head -1)"
fi

# ---------------------------- 4. 安装依赖 -----------------------------------
log "检查打包依赖"
missing=()
for spec in "${PKGS[@]}"; do
    pkg="${spec%%:*}"      # pip 包名
    imp="${spec##*:}"      # import 名
    if wine "$PY_EXE_WIN" -c "import $imp" >/dev/null 2>&1; then
        info "已安装 $pkg"
    else
        info "缺少 $pkg"
        missing+=("$pkg")
    fi
done
if [ "${#missing[@]}" -gt 0 ]; then
    info "安装：${missing[*]}"
    # 注意这里不能 `|| true` 吞掉错误，装失败要在下面报出来
    wine "$PY_EXE_WIN" -m pip install --quiet --disable-pip-version-check "${missing[@]}" \
        2>&1 | grep -viE "mesa|dri2|drm device|fixme:" || true
fi
for spec in "${PKGS[@]}"; do
    pkg="${spec%%:*}"
    imp="${spec##*:}"
    wine "$PY_EXE_WIN" -c "import $imp" >/dev/null 2>&1 || die "$pkg 安装失败（import $imp 不通）"
done
ok "依赖齐全"

# ---------------------------- 5. 语法检查 -----------------------------------
log "语法检查"
for f in flash.py gui.py config.py version.py package_release.py; do
    python3 -m py_compile "$f" || die "$f 语法有错"
done
rm -rf __pycache__
ok "源文件语法正常"

# ---------------------------- 6. 打包 ---------------------------------------
log "开始打包（PyInstaller）"
rm -rf build
wine "$PY_EXE_WIN" -m PyInstaller --clean --noconfirm build_exe.spec \
    > _winbuild/build.log 2>&1 || {
        grep -viE "mesa|dri2|drm device|fixme:" _winbuild/build.log | tail -30
        die "打包失败，完整日志见 _winbuild/build.log"
    }
grep -iE "Building EXE from EXE-00.toc completed successfully" _winbuild/build.log >/dev/null \
    || warn "日志里没看到成功标记，请检查 _winbuild/build.log"

EXE_PATH="dist/$EXE_NAME"
[ -f "$EXE_PATH" ] || die "打包失败：找不到 $EXE_PATH"
ok "已生成 $EXE_PATH（$(stat -c%s "$EXE_PATH" 2>/dev/null || stat -f%z "$EXE_PATH") 字节）"

# ---------------------------- 7. 自检 ---------------------------------------
if [ "$RUN_SELFTEST" = "1" ]; then
    log "运行 exe 自检"
    rm -f "dist/selftest.log"

    # exe 是 GUI 子系统程序，从 shell 调用时退出码不一定可靠 ——
    # 所以以 selftest.log 的内容为准，轮询等它写出来。
    ( cd dist && timeout "$SELFTEST_TIMEOUT" wine "$EXE_NAME" --selftest --quiet >/dev/null 2>&1 ) || true
    for _ in $(seq 1 30); do
        [ -s "dist/selftest.log" ] && break
        sleep 1
    done

    if [ ! -s "dist/selftest.log" ]; then
        warn "没有生成 selftest.log —— exe 可能根本没跑起来"
    elif grep -q "自检通过" "dist/selftest.log"; then
        ok "自检通过"
    else
        warn "自检未通过（多半是漏了 hiddenimports）"
    fi

    if [ -f "dist/selftest.log" ]; then
        echo
        sed 's/^/    /' "dist/selftest.log"
    fi
fi

# ---------------------------- 8. 打包 zip -----------------------------------
# 和 CI 用同一个脚本，保证本地和线上产出一致
log "打包 zip（exe + tcboot.bin）"
if [ -f package_release.py ]; then
    python3 package_release.py dist || warn "zip 打包失败（不影响 exe）"
else
    warn "找不到 package_release.py，跳过 zip 打包"
fi

# ---------------------------- 收尾 -----------------------------------------
rm -rf __pycache__ build
log "完成"
echo "    产物：$EXE_PATH"
echo "    用法：把 $EXE_NAME 和 tcboot.bin 放同一目录，双击运行"
echo
echo "    提醒：exe 里的 flash.py 是运行时动态加载的，改了 flash.py 需要重新"
echo "          跑本脚本才会生效；只改 config.ini 则不用重打包。"
