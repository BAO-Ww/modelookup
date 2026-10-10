# -*- coding: utf-8 -*-
"""CI 里跑 Mac .app 的 --check 自检，并校验 ASCII 哨兵 RESULT: PASS。
2026-10-11 Mac 端新增，与 check_exe.py（Windows）平行。

用法: python .github/scripts/check_mac.py app/dist/ModelLookup.app
"""
import os
import subprocess
import sys
import time


def main():
    app = sys.argv[1] if len(sys.argv) > 1 else os.path.join("app", "dist", "ModelLookup.app")
    app = os.path.abspath(app)
    if not os.path.isdir(app):
        raise SystemExit("找不到 .app：" + app)
    binp = os.path.join(app, "Contents", "MacOS", "ModelLookup")
    if not os.path.isfile(binp):
        raise SystemExit("找不到可执行文件：" + binp)

    log = os.path.join(os.path.dirname(binp), "exe_check.log")
    if os.path.isfile(log):
        os.remove(log)

    print("Running self check: %s --check" % binp)
    try:
        p = subprocess.run([binp, "--check"], timeout=600)
        print("check process exit code = %s" % p.returncode)
    except subprocess.TimeoutExpired:
        raise SystemExit("--check ran over 10 minutes, stuck")

    for _ in range(60):
        if os.path.isfile(log):
            break
        time.sleep(1)
    if not os.path.isfile(log):
        raise SystemExit("--check finished but no exe_check.log produced")

    with open(log, encoding="utf-8", errors="replace") as f:
        txt = f.read()
    print("-" * 64)
    print(txt)
    print("-" * 64)

    if "RESULT: PASS" not in txt:
        raise SystemExit("self check FAILED: no RESULT: PASS in log")
    print("self check passed (RESULT: PASS)")


if __name__ == "__main__":
    main()
