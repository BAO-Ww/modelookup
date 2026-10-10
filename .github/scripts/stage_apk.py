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
    apks = [p for p in glob.glob(pat) if "-unsigned" not in os.path.basename(p)] or glob.glob(pat)
    if not apks:
        sys.exit("no APK found under %s" % pat)
    os.makedirs("out", exist_ok=True)
    dst = os.path.join("out", "ModelLookup-Mobile-%s.apk" % num)
    shutil.copyfile(sorted(apks)[-1], dst)
    print("ARTIFACT: %s (%.2f MB)" % (dst, os.path.getsize(dst) / 1048576.0))


if __name__ == "__main__":
    main()
