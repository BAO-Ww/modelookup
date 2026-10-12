# -*- coding: utf-8 -*-
"""整理 APK 产物（CI 用）：android/app/build/outputs/apk/release/*.apk
→ out/ModelLookup-Mobile-<num>.apk（ASCII 文件名，GitHub Release 会消毒中文名）。"""
import glob
import io
import os
import shutil
import sys


def main():
    num = sys.argv[1] if len(sys.argv) > 1 else "0.0"
    pat = os.path.join("android", "app", "build", "outputs", "apk", "release", "*.apk")
    apks = sorted(glob.glob(pat))
    if not apks:
        sys.exit("no APK found under %s" % pat)
    unsigned = [p for p in apks if "-unsigned" in os.path.basename(p)]
    if unsigned and len(unsigned) == len(apks):
        sys.exit("APK is UNSIGNED (signing config broken?) — refusing to stage")
    src_apk = ([p for p in apks if "-unsigned" not in os.path.basename(p)] or apks)[-1]
    os.makedirs("out", exist_ok=True)
    dst = os.path.join("out", "ModelLookup-Mobile-%s.apk" % num)
    shutil.copyfile(src_apk, dst)
    print("ARTIFACT: %s (%.2f MB) from %s" % (dst, os.path.getsize(dst) / 1048576.0,
                                              os.path.basename(src_apk)))


if __name__ == "__main__":
    main()
