#!/usr/bin/env python3
"""从 git 历史生成 Release 的「更新日志」段落。

用法::

    python3 changelog.py                # 当前 HEAD 到最近一个 tag 之间的提交
    python3 changelog.py v2.1.0         # 指定 tag（CI 里用 $GITHUB_REF_NAME）
    python3 changelog.py --range        # 只打印用到的提交区间，便于排查

设计说明：

- 提交信息按 conventional commits 前缀（feat / fix / docs / ...）分组，
  认不出前缀的一律归到「其他改动」，所以老的 ``Update README.MD``
  这类提交也不会丢。
- ``release.sh`` 自动产生的 ``release: vX.Y.Z`` 提交只是改版本号，
  对用户没有意义，跳过。
- 找不到上一个 tag 时（首次发版）退回列出全部提交。
- 输出的是 Markdown 片段，由 CI 插进 Release 说明里；本地也可以直接跑来看。
"""

from __future__ import annotations

import re
import subprocess
import sys

# 提交前缀 → 分组标题
TYPE_TO_GROUP = {
    "feat": "新功能",
    "fix": "修复",
    "perf": "性能优化",
    "refactor": "重构",
    "docs": "文档",
    "test": "测试",
    "ci": "构建 / CI",
    "build": "构建 / CI",
    "chore": "其他改动",
    "update": "其他改动",
    "style": "其他改动",
}

# 分组在输出里的先后顺序（用户最关心的排前面）
GROUP_ORDER = [
    "新功能",
    "修复",
    "性能优化",
    "重构",
    "文档",
    "测试",
    "构建 / CI",
    "其他改动",
]

DEFAULT_GROUP = "其他改动"

# 这些前缀的提交不进更新日志（纯粹是发版机器产生的噪音）
SKIP_TYPES = {"release"}

# feat(scope): 说明   /  fix: 说明  /  feat!: 说明
PREFIX_RE = re.compile(r"^\s*([A-Za-z]+)\s*(?:\([^)]*\))?\s*!?\s*[:：]\s*(.+?)\s*$")

# git log 里用 ASCII 单元分隔符隔开哈希和标题，避免标题里有特殊字符时切错
FIELD_SEP = "\x1f"


def git(*args):
    """跑一条 git 命令，成功返回 stdout（已 strip），失败返回 None。"""
    try:
        proc = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def repo_slug():
    """拿 owner/repo，用于把提交哈希拼成 GitHub 链接。

    优先用 version.py（项目里版本号的唯一来源），拿不到再从 git remote 猜。
    """
    try:
        from version import REPO  # type: ignore

        if REPO:
            return REPO.strip()
    except Exception:
        pass

    url = git("remote", "get-url", "origin") or ""
    m = re.search(r"github\.com[:/]+([^/]+/[^/\s]+?)(?:\.git)?$", url)
    return m.group(1) if m else ""


def previous_tag(ref):
    """ref 之前最近的一个 tag；没有就返回 None（首次发版）。"""
    out = git("describe", "--tags", "--abbrev=0", ref + "^")
    return out or None


def collect(rev_range):
    """返回 [(短哈希, 提交标题), ...]，顺序与 git log 一致（新 → 旧）。"""
    out = git("log", "--no-merges", "--pretty=format:%h" + FIELD_SEP + "%s", rev_range)
    if not out:
        return []

    rows = []
    for line in out.splitlines():
        line = line.strip()
        if not line or FIELD_SEP not in line:
            continue
        short, subject = line.split(FIELD_SEP, 1)
        short, subject = short.strip(), subject.strip()
        if short and subject:
            rows.append((short, subject))
    return rows


def classify(subject):
    """把提交标题拆成 (分组, 说明)；该跳过的返回 (None, None)。"""
    m = PREFIX_RE.match(subject)
    if not m:
        # 没有 conventional 前缀（例如 "Update README.MD"）——原样保留
        return DEFAULT_GROUP, subject

    typ, rest = m.group(1).lower(), m.group(2).strip()
    if typ in SKIP_TYPES:
        return None, None
    return TYPE_TO_GROUP.get(typ, DEFAULT_GROUP), rest


def group_commits(rows):
    """按分组归类，返回 {分组: [(哈希, 说明), ...]}。"""
    groups = {}
    for short, subject in rows:
        name, text = classify(subject)
        if name is None:
            continue
        groups.setdefault(name, []).append((short, text))
    return groups


def render(groups, repo="", prev=None):
    """渲染成 Markdown 片段。"""
    if not groups:
        return "## 更新日志\n\n本次没有需要记录的改动。\n"

    out = ["## 更新日志", ""]
    if prev:
        out.append("自 `%s` 以来的改动：" % prev)
        out.append("")

    for name in GROUP_ORDER:
        items = groups.get(name)
        if not items:
            continue
        out.append("### %s" % name)
        out.append("")
        for short, text in items:
            if repo:
                out.append("- %s（[`%s`](https://github.com/%s/commit/%s)）" % (text, short, repo, short))
            else:
                out.append("- %s（`%s`）" % (text, short))
        out.append("")

    return "\n".join(out).rstrip() + "\n"


def main(argv):
    tag = ""
    want_range = False

    for arg in argv[1:]:
        if arg in ("-h", "--help"):
            print(__doc__.strip())
            return 0
        if arg == "--range":
            want_range = True
        elif arg.startswith("-"):
            sys.stderr.write("未知参数: %s（用 --help 查看用法）\n" % arg)
            return 2
        elif tag:
            sys.stderr.write("多余的参数: %s\n" % arg)
            return 2
        else:
            tag = arg

    ref = tag or "HEAD"
    if not git("rev-parse", "-q", "--verify", ref + "^{commit}"):
        sys.stderr.write("找不到 git 引用: %s\n" % ref)
        return 1

    prev = previous_tag(ref)
    rev_range = "%s..%s" % (prev, ref) if prev else ref

    if want_range:
        print(rev_range)
        return 0

    rows = collect(rev_range)
    groups = group_commits(rows)
    sys.stdout.write(render(groups, repo_slug(), prev))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
