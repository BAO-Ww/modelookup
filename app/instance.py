# -*- coding: utf-8 -*-
"""单实例互斥 + 端口登记（2026-10-11 拆分一期二级细拆：从 main.py 原样搬出，纯搬运零行为变化）。
依赖方向：config → paths → … → instance（被 server 与入口 main() 调用）。"""
import io
import sys
import urllib.request

from config import HOST, PORTS
from paths import wp

# ⚠ 单实例判定**绝对不能用「连一下端口」的办法**：
#   本机（以及不少装了安全软件的机器）连 127.0.0.1 的**未监听端口**不会立刻回 ECONNREFUSED，
#   而是一路等到超时 —— 实测 0.6 秒/个 × 8 个端口 = 4.8 秒，全压在启动路径上，
#   这才是「打开要 5.9 秒」的真正原因（跟页面多大没关系）。
#   改用命名互斥量：CreateMutexW 是瞬时的，进程退出系统自动释放。
_MUTEX = None


def already_running():
    """True = 本机已有本程序在跑（互斥量已被别人持有）。"""
    global _MUTEX
    # 2026-10-11 Mac 端：flock 锁文件（瞬时），语义与 Windows 命名互斥量对齐
    if sys.platform == "darwin":
        return _already_running_posix()
    try:
        import ctypes
        k = ctypes.windll.kernel32
        k.CreateMutexW.restype = ctypes.c_void_p
        k.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        h = k.CreateMutexW(None, 0, "Local\\ModelLookup_SingleInstance")
        if not h:
            return False
        if k.GetLastError() == 183:        # ERROR_ALREADY_EXISTS
            k.CloseHandle(ctypes.c_void_p(h))
            return True
        _MUTEX = h                          # 拿着不放，进程退出自动释放
        return False
    except Exception:
        return False


_LOCKFD = None


def _already_running_posix():
    """Mac：flock 单实例（fcntl，进程退出系统自动释放，同互斥量语义）。"""
    global _LOCKFD
    try:
        import fcntl
        f = open(wp("single.lock"), "a+")
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return True
        _LOCKFD = f                      # 拿着不放，进程退出自动释放
        return False
    except Exception:
        return False


def save_port(port):
    try:
        with io.open(wp("port.txt"), "w", encoding="utf-8") as f:
            f.write(str(port))
    except Exception:
        pass


def read_port():
    try:
        t = io.open(wp("port.txt"), encoding="utf-8").read().strip()
        return int(t) if t.isdigit() else 0
    except Exception:
        return 0


def activate_existing(port):
    """把一个已在跑的实例的窗口叫到前面来（连的是**在监听**的端口，秒回）。"""
    for path in ("/api/focus", "/api/ping"):
        try:
            urllib.request.urlopen("http://%s:%d%s" % (HOST, port, path), timeout=1.5).read()
            return True
        except Exception:
            continue
    return False


def any_live_instance():
    """逐个端口找**活着的**实例（/api/ping 有应答才算活）。
    2026-10-10 实锤：实例会僵死（互斥量被占、端口在 LISTEN 但不 accept、
    taskkill/Stop-Process 都杀不动，只能等重启）——这种不能算「已在运行」。"""
    for p in PORTS:
        try:
            urllib.request.urlopen("http://%s:%d/api/ping" % (HOST, p), timeout=0.6).read()
            return p
        except Exception:
            continue
    return 0
