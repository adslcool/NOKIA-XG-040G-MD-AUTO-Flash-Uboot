#!/usr/bin/env python3
"""把 exe 和 tcboot.bin 打包成一个 zip，作为 Release 附件。

为什么要有这个脚本（而不是在 CI 的 YAML 里内联一段 python）：
  - CI 和 build_exe.sh 都调用它，本地和线上产出一致，不会各写一份逻辑；
  - exe 文件名带中文，在 YAML 块标量 + shell 引号里容易出编码问题，
    用 Python 的 zipfile 直接读写最稳。

zip 里的两个文件都**不带版本号**，解压后直接双击 exe 就能用；
zip 自身的名字也不带版本号 —— Release 页面已经标了 tag，
再往文件名里塞版本号只会让地址变长、还容易和旧包混淆。
"""

import glob
import os
import sys
import zipfile

ZIP_NAME = "XG-040G-MD一键刷机.zip"


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

    out = os.path.join(dist, ZIP_NAME)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.write(exe, os.path.basename(exe))
        z.write(tcboot, "tcboot.bin")

    print("已生成 %s（%d 字节）" % (out, os.path.getsize(out)))
    with zipfile.ZipFile(out) as z:
        for info in z.infolist():
            print("  包含 %s（%d 字节）" % (info.filename, info.file_size))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
