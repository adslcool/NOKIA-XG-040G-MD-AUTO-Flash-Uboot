#!/usr/bin/env python3
"""把 exe 和 tcboot.bin 打包成 zip，并生成可以直接上传到 Release 的附件。

为什么要这个脚本（而不是在 CI 的 YAML 里内联一段 python）：
  - CI 和 build_exe.sh 都调用它，本地和线上产出一致，不会各写一份逻辑；
  - exe 文件名带中文，在 YAML 块标量 + shell 引号里容易出编码问题，
    用 Python 的 zipfile 直接读写最稳。

产出（都在 dist 目录下）：
  1. dist/XG-040G-MD一键刷机.zip
     本地用的中文名包，解压出来是中文名的 exe，双击即用。
  2. dist/release/ —— 专门给 GitHub Release 用的 ASCII 名副本：
       XG-040G-MD.exe / XG-040G-MD.zip / SHA256SUMS.txt

为什么非要另存一份 ASCII 名的副本：GitHub 上传 Release 附件时会**直接删掉
文件名里的非 ASCII 字符**，`XG-040G-MD工具.exe` 在 Release 页面上会变成
`XG-040G-MD.exe`。于是 Release 上的附件名和我们写进 SHA256SUMS.txt 的名字
对不上，用户下载后跑 `sha256sum -c` 必然报「没有那个文件或目录」。
所以干脆自己先把上传用的名字定成 ASCII，不去依赖 GitHub 的隐式清洗。
zip 内部仍然保留中文 exe 名（解压出来还是中文，用户看到的名字不变）。

zip 里的文件**不带版本号**，解压后直接双击 exe 就能用；zip 自身的名字也不带
版本号 —— Release 页面已经标了 tag，再往文件名里塞版本号只会让地址变长、
还容易和旧包混淆。
"""

import glob
import hashlib
import os
import shutil
import sys
import zipfile

ZIP_NAME = "XG-040G-MD一键刷机.zip"

# Release 附件的 ASCII 名字。这两个名字是给 GitHub 页面用的，
# 也是 SHA256SUMS.txt 里写的名字，两边必须一字不差。
RELEASE_DIR = "release"
RELEASE_EXE = "XG-040G-MD.exe"
RELEASE_ZIP = "XG-040G-MD.zip"


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv):
    dist = argv[1] if len(argv) > 1 else "dist"

    # exe 名带中文，用 glob 拿，别写死
    exes = sorted(glob.glob(os.path.join(dist, "*.exe")))
    if not exes:
        print("错误：%s 里没有 .exe" % dist, file=sys.stderr)
        return 1
    if len(exes) > 1:
        print("错误：%s 里有多个 exe，不知道该打哪个：%s" % (dist, exes), file=sys.stderr)
        return 1
    exe = exes[0]

    # tcboot.bin 优先用 dist 里的（可能被换成了别的版本），否则退回仓库根目录的。
    # exe 自己内置了一份，但同目录的文件优先级更高 —— 用户想换 uboot 时
    # 直接替换 zip 里的这个文件即可，不用重新打包 exe。
    tcboot = os.path.join(dist, "tcboot.bin")
    if not os.path.isfile(tcboot):
        tcboot = "tcboot.bin"
    if not os.path.isfile(tcboot):
        print("错误：找不到 tcboot.bin", file=sys.stderr)
        return 1

    # 1) 本地用的中文名 zip（exe 在包里也保持中文名）
    out = os.path.join(dist, ZIP_NAME)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.write(exe, os.path.basename(exe))
        z.write(tcboot, "tcboot.bin")

    print("已生成 %s（%d 字节）" % (out, os.path.getsize(out)))
    with zipfile.ZipFile(out) as z:
        for info in z.infolist():
            print("  包含 %s（%d 字节）" % (info.filename, info.file_size))

    # 2) 给 Release 用的 ASCII 名副本。
    # 先整个清掉，免得上次的残留被 CI 的 dist/release/* 一起传上去。
    rel = os.path.join(dist, RELEASE_DIR)
    if os.path.isdir(rel):
        shutil.rmtree(rel)
    os.makedirs(rel)

    rel_exe = os.path.join(rel, RELEASE_EXE)
    rel_zip = os.path.join(rel, RELEASE_ZIP)
    shutil.copyfile(exe, rel_exe)
    shutil.copyfile(out, rel_zip)

    # SHA256SUMS.txt 里写的必须是**上传后的附件名**，也就是上面这两个 ASCII 名。
    # 两个空格是 `sha256sum -c` 要求的文本模式分隔符，别改成单个空格。
    sums = [
        "%s  %s" % (_sha256(rel_exe), RELEASE_EXE),
        "%s  %s" % (_sha256(rel_zip), RELEASE_ZIP),
    ]
    sums_path = os.path.join(rel, "SHA256SUMS.txt")
    with open(sums_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(sums) + "\n")

    print("已生成 Release 附件目录 %s：" % rel)
    for line in sums:
        h, name = line.split("  ")
        print("  %-20s %10d 字节  sha256 %s" % (name, os.path.getsize(os.path.join(rel, name)), h))
    print("  %-20s %10d 字节" % ("SHA256SUMS.txt", os.path.getsize(sums_path)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
