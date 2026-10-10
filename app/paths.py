# -*- coding: utf-8 -*-
"""路径（2026-10-11 拆分一期：从 main.py 原样搬出，纯搬运零行为变化）。
依赖方向：config → paths（config 在前，本模块只依赖标准库 + config）。"""
import os
import sys

from config import APP_ID


def is_frozen():
    return bool(getattr(sys, "frozen", False))


def res_dir():
    """打包后的资源目录 / 开发时的 assets 目录。"""
    if is_frozen():
        return os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(sys.executable)), "assets")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")


def work_dir():
    # 2026-10-11 Mac 端：darwin 走标准 Application Support；Windows 路径逐字不变
    if sys.platform == "darwin":
        d = os.path.join(os.path.expanduser("~"), "Library", "Application Support", APP_ID)
    else:
        d = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), APP_ID)
    os.makedirs(d, exist_ok=True)
    return d


def rp(name):
    return os.path.join(res_dir(), name)


def wp(name):
    return os.path.join(work_dir(), name)
