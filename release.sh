#!/usr/bin/env bash
# ============================================================================
#  XG-040G-MD 光猫工具 · 发版脚本
# ============================================================================
#  一条命令完成：改版本号 → 提交 → 打 tag → 推送 → 触发 GitHub Actions
#  编译 Windows exe → 把 exe 挂到 Release 上。
#
#  用法:
#      ./release.sh patch              # 末位 +1（修 bug）
#      ./release.sh minor              # 中间位 +1（加功能）
#      ./release.sh major              # 首位 +1（不兼容大改）
#      ./release.sh 1.4.2              # 直接指定版本号
#      ./release.sh patch --no-push    # 只改+提交+打 tag，不推送
#      ./release.sh patch -m "说明"    # 自定义 commit / release 说明
#      ./release.sh --dry-run patch    # 只打印将要做什么，不落盘
#      ./release.sh --current          # 显示当前版本
#
#  版本号唯一来源是 version.py；本脚本负责改它，不要手改。
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VERSION_FILE="version.py"
DRY_RUN=0
NO_PUSH=0
BUMP=""
MSG=""

# ---------- 输出辅助 ----------
if [ -t 1 ]; then
    C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YEL=$'\033[33m'
    C_BLU=$'\033[36m'; C_BLD=$'\033[1m'; C_RST=$'\033[0m'
else
    C_RED=""; C_GRN=""; C_YEL=""; C_BLU=""; C_BLD=""; C_RST=""
fi
log()  { printf '%s==>%s %s\n' "$C_BLU$C_BLD" "$C_RST" "$*"; }
info() { printf '    %s\n' "$*"; }
ok()   { printf '%s  ✓%s %s\n' "$C_GRN" "$C_RST" "$*"; }
warn() { printf '%s  !%s %s\n' "$C_YEL" "$C_RST" "$*" >&2; }
die()  { printf '%s错误:%s %s\n' "$C_RED$C_BLD" "$C_RST" "$*" >&2; exit 1; }

usage() {
    # 从文件头的注释里抽出用法段：从「用法:」那行到后面的分隔线为止。
    # 用标记定位而不是写死行号 —— 否则往文件头加一行注释，帮助就会串味。
    sed -n '/^#  用法:/,/^# ===/p' "${BASH_SOURCE[0]}" \
        | sed 's/^# \{0,1\}//' \
        | sed '$d'          # 去掉结尾那行分隔线
    exit 0
}

# ---------- 解析参数 ----------
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        --no-push) NO_PUSH=1; shift ;;
        --current)
            python3 -c "import version; print(version.VERSION)"
            exit 0 ;;
        -m|--message) [ $# -ge 2 ] || die "-m 后面要跟说明文字"; MSG="$2"; shift 2 ;;
        -h|--help) usage ;;
        -*) die "未知参数: $1（用 --help 查看用法）" ;;
        *)
            [ -z "$BUMP" ] || die "多余的参数: $1（版本号/类型只能给一个）"
            BUMP="$1"; shift ;;
    esac
done

[ -n "$BUMP" ] || { usage; }

# ---------- 前置检查 ----------
log "检查工作区"
[ -f "$VERSION_FILE" ] || die "找不到 $VERSION_FILE（请在项目根目录运行）"
command -v python3 >/dev/null || die "需要 python3"
command -v git >/dev/null || die "需要 git"

# 必须先提交干净，否则 tag 会指向一个不含改动的提交
if [ -n "$(git status --porcelain)" ] && [ "$DRY_RUN" -eq 0 ]; then
    warn "工作区有未提交的改动："
    git status --short | sed 's/^/      /'
    die "请先提交或 stash 这些改动，再发版（保证 tag 指向的提交是完整的）"
fi

CUR_VER="$(python3 -c "import version; print(version.VERSION)")"
REPO="$(python3 -c "import version; print(version.REPO)")"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
info "当前版本: $CUR_VER"
info "当前分支: $BRANCH"
info "目标仓库: $REPO"

# ---------- 计算新版本号 ----------
NEW_VER="$(python3 - "$CUR_VER" "$BUMP" <<'PY'
import sys

cur, bump = sys.argv[1], sys.argv[2]

def parse(v):
    core = v.split("-", 1)[0].split("+", 1)[0]
    out = []
    for p in core.split(".")[:3]:
        try:
            out.append(int(p))
        except ValueError:
            out.append(0)
    while len(out) < 3:
        out.append(0)
    return out

# 直接给了版本号（形如 1.2.3）就用它，否则按类型递增
if bump[0].isdigit():
    parts = parse(bump)
    if not any(c.isdigit() for c in bump) or parts == [0, 0, 0]:
        sys.exit("非法版本号: " + bump)
    print(".".join(str(x) for x in parts))
    sys.exit(0)

major, minor, patch = parse(cur)
if bump == "major":
    major, minor, patch = major + 1, 0, 0
elif bump == "minor":
    minor, patch = minor + 1, 0
elif bump == "patch":
    patch += 1
else:
    sys.exit("未知的版本类型: " + bump + "（可选 major / minor / patch，或直接给 1.2.3）")
print(f"{major}.{minor}.{patch}")
PY
)" || die "版本号计算失败"

TAG="v$NEW_VER"

info "新版本号: $C_BLD$NEW_VER$C_RST"
info "新 tag  : $C_BLD$TAG$C_RST"

# ---------- 冲突检查 ----------
if git rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
    die "tag $TAG 已存在（先 git tag -d $TAG 或换个版本号）"
fi
if git ls-remote --exit-code --tags origin "$TAG" >/dev/null 2>&1; then
    die "远程已存在 tag $TAG（先删掉远程的：git push origin :refs/tags/$TAG）"
fi

if [ "$DRY_RUN" -eq 1 ]; then
    echo
    log "${C_YEL}--dry-run：以下动作都不会真的执行${C_RST}"
    # 用计数器编号，两条分支（改版本号 / 版本号已一致）的步骤数不同也不会串号
    _n=1
    if [ "$NEW_VER" = "$CUR_VER" ]; then
        info "$((_n++)). 版本号已是 \"$NEW_VER\"，跳过修改 version.py 和提交"
    else
        info "$((_n++)). 把 $VERSION_FILE 里的 VERSION 改成 \"$NEW_VER\""
        info "$((_n++)). git add $VERSION_FILE && git commit -m \"release: $TAG\""
    fi
    info "$((_n++)). git tag -a $TAG -m \"$TAG\""
    if [ "$NO_PUSH" -eq 1 ]; then
        info "$((_n++)). （--no-push）不推送"
    else
        info "$((_n++)). git push origin $BRANCH && git push origin $TAG"
        info "$((_n++)). GitHub Actions 自动编译 Windows exe 并挂到 Release $TAG"
    fi
    echo
    ok "dry-run 结束，什么都没改"
    exit 0
fi

# ---------- 1. 改版本号 ----------
# 目标版本号与当前一致时（例如 version.py 已经提到 2.1.0，现在要正式发 v2.1.0），
# 不改文件也不提交 —— 否则 git commit 会因「没有改动」而失败。
if [ "$NEW_VER" = "$CUR_VER" ]; then
    log "版本号已经是 $CUR_VER，跳过修改和提交，直接给当前提交打 tag"
else
    log "更新 $VERSION_FILE"
    python3 - "$VERSION_FILE" "$CUR_VER" "$NEW_VER" <<'PY'
import re
import sys

path, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
src = open(path, encoding="utf-8").read()

# 只替换模块级 VERSION 赋值，别误伤 docstring 里的示例
pat = re.compile(r'^VERSION\s*=\s*["\'][^"\']*["\']', re.M)
if not pat.search(src):
    sys.exit("在 %s 里找不到 VERSION = \"...\" 赋值" % path)

src = pat.sub('VERSION = "%s"' % new, src, count=1)
open(path, "w", encoding="utf-8").write(src)
print("    VERSION: %s -> %s" % (old, new))
PY
    ok "版本号已更新"

    # 立刻校验，改错了就回滚，别把坏版本提交上去
    GOT="$(python3 -c "import version; print(version.VERSION)" 2>/dev/null || echo "")"
    [ "$GOT" = "$NEW_VER" ] || { git checkout -- "$VERSION_FILE"; die "改完后读回来是 '$GOT'，不是 '$NEW_VER'，已回滚"; }
    ok "校验通过（import version → $GOT）"

    # ---------- 2. 提交 ----------
    log "提交"
    COMMIT_MSG="${MSG:-release: $TAG}"
    git add "$VERSION_FILE"
    git commit -m "$COMMIT_MSG"
    ok "已提交: $(git log -1 --format='%h %s')"
fi

# ---------- 3. 打 tag ----------
log "打 tag"
TAG_MSG="${MSG:-$TAG}"
git tag -a "$TAG" -m "$TAG_MSG"
ok "已打 tag: $TAG"

# ---------- 4. 推送 ----------
if [ "$NO_PUSH" -eq 1 ]; then
    warn "--no-push：没有推送。手动推送："
    info "git push origin $BRANCH && git push origin $TAG"
    exit 0
fi

log "推送到 origin"
git push origin "$BRANCH"
git push origin "$TAG"
ok "已推送 $BRANCH 和 $TAG"

echo
log "${C_GRN}发版完成${C_RST}"
info "版本   : $NEW_VER"
info "tag    : $TAG"
info "提交   : $(git log -1 --format='%h %s')"
info "Actions: https://github.com/$REPO/actions"
info "Release: https://github.com/$REPO/releases/tag/$TAG"
echo
info "GitHub Actions 会自动编译 Windows exe 并上传到上面这个 Release。"
info "编译大约需要 2~4 分钟，编译完 exe 会出现在 Release 的附件里。"
