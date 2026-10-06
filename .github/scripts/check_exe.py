# -*- coding: utf-8 -*-
"""在 CI 里跑 exe 的 --check 自检，并校验 ASCII 哨兵 RESULT: PASS。

为什么不直接在 workflow 里写 `& .\\ModelLookup.exe --check`：
    exe 是 **GUI 子系统**（spec 里 console=False），PowerShell / cmd 调用 GUI 程序
    **不会等待它结束**，会立刻返回；紧接着读 exe_check.log 就是「文件不存在」。
    Python 的 subprocess 才可靠地等进程结束（不管是不是 GUI 子系统）。

用法: python .github/scripts/check_exe.py app/dist/ModelLookup.exe
"""
import os
import subprocess
import sys
import time


def main():
    exe = sys.argv[1] if len(sys.argv) > 1 else os.path.join("app", "dist", "ModelLookup.exe")
    exe = os.path.abspath(exe)
    if not os.path.isfile(exe):
        raise SystemExit("找不到 exe：" + exe)

    log = os.path.join(os.path.dirname(exe), "exe_check.log")
    if os.path.isfile(log):
        os.remove(log)

    print("运行自检：%s --check" % exe)
    try:
        p = subprocess.run([exe, "--check"], timeout=600)
        print("自检进程退出码 = %s" % p.returncode)
    except subprocess.TimeoutExpired:
        raise SystemExit("exe --check 跑超过 10 分钟没结束，卡住了")

    # onefile 会先解压再跑，日志落地可能略晚于进程退出（正常不会），留点余量
    for _ in range(60):
        if os.path.isfile(log):
            break
        time.sleep(1)
    if not os.path.isfile(log):
        raise SystemExit("exe --check 跑完了却没生成 exe_check.log —— 自检根本没执行")

    with open(log, encoding="utf-8", errors="replace") as f:
        txt = f.read()
    print("-" * 64)
    print(txt)
    print("-" * 64)

    if "RESULT: PASS" not in txt:
        raise SystemExit("成品自检没通过：日志里没有 RESULT: PASS")
    print("成品自检通过 (RESULT: PASS)")


if __name__ == "__main__":
    main()
