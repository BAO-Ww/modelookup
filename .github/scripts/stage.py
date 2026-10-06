# -*- coding: utf-8 -*-
"""CI 里把构建产物整理到 out/。

为什么用 Python 而不用 PowerShell：
    PowerShell 脚本里写中文文件名，在 runner 上很容易被控制台/脚本编码搞坏，
    生成出乱码名字的文件。Python 源码默认 UTF-8，文件名稳定。

用法: python .github/scripts/stage.py <版本号，如 2.0>
"""
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))


def main():
    ver = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
    if not ver or ver == "0.0":
        ver = "dev"

    out = os.path.join(ROOT, "out")
    if os.path.isdir(out):
        shutil.rmtree(out)
    os.makedirs(out, exist_ok=True)

    ddir = os.path.join(ROOT, "dist_installer")
    src_inst = os.path.join(ddir, "ModelLookup-Setup-%s.exe" % ver)
    if not os.path.isfile(src_inst):
        got = sorted(os.listdir(ddir)) if os.path.isdir(ddir) else []
        raise SystemExit("找不到安装包 %s；dist_installer 里只有：%s" % (src_inst, got))

    pairs = [
        (src_inst, "型号查询-安装包-v%s.exe" % ver),
        (os.path.join(ROOT, "app", "dist", "ModelLookup.exe"), "ModelLookup.exe"),
        (os.path.join(ROOT, "使用说明.txt"), "使用说明.txt"),
        (os.path.join(ROOT, "型号查询-公布版.html"), "型号查询-公布版.html"),
    ]
    for a, b in pairs:
        if os.path.isfile(a):
            shutil.copy2(a, os.path.join(out, b))
            print("  %-30s %10.2f MB" % (b, os.path.getsize(a) / 1048576.0))
        else:
            print("  ! 跳过（源文件不存在）：%s" % a)

    for need in ("型号查询-安装包-v%s.exe" % ver, "ModelLookup.exe"):
        p = os.path.join(out, need)
        if not os.path.isfile(p):
            raise SystemExit("必需的产物没生成：" + p)
    print("out 就绪：%s" % out)


if __name__ == "__main__":
    main()
