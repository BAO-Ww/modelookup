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


# ---------------- 素材存储根设置（2026-10-24 用户定稿：可改存储路径，peiwork+copy 共用，老数据搬） ----------------
# 存本机 settings.json（{"data_root": "<绝对路径>"}），跟机器走不跟账号走；
# 没设置时 = work_dir()（原行为，零变化）。设置后 素材库peiwork/ 文案库copy/ 都落在 data_root 下。

_SETTINGS_CACHE = [None]


def _settings():
    if _SETTINGS_CACHE[0] is None:
        try:
            import json as _json
            with open(wp("settings.json"), "r", encoding="utf-8") as f:
                v = _json.load(f)
                _SETTINGS_CACHE[0] = v if isinstance(v, dict) else {}
        except Exception:
            _SETTINGS_CACHE[0] = {}
    return _SETTINGS_CACHE[0]


def data_root():
    """素材存储根：settings.json 里的 data_root（绝对路径），没设就返回 None（用默认 work_dir）。"""
    v = str((_settings() or {}).get("data_root") or "").strip()
    return v or None


def set_data_root(p):
    """设置素材存储根（None/空串=恢复默认）。写 settings.json 并清缓存。"""
    import json as _json
    p = str(p or "").strip() or None
    if p and not os.path.isabs(p):
        raise ValueError("必须是绝对路径")
    s = _settings()
    if p:
        s["data_root"] = p
    else:
        s.pop("data_root", None)
    _SETTINGS_CACHE[0] = s
    with open(wp("settings.json"), "w", encoding="utf-8") as f:
        _json.dump(s, f, ensure_ascii=False, indent=1)
    return p


def update_proxy():
    """更新下载代理（2026-10-29 用户令「把我给你的节点链接挂上去」）：
    settings.json 里的 update_proxy（如 http://127.0.0.1:7891），
    自更新下载 /appdl 时走它（实测直连 0.10MB/s → 走节点 0.55MB/s）。
    没设置 = 直连（原行为）。"""
    v = str((_settings() or {}).get("update_proxy") or "").strip()
    return v or None


def reveal(p):
    """在文件管理器里打开目录（2026-10-25 跨平台）：Win=资源管理器 / macOS=Finder。
    替代 os.startfile（Windows 专属，Mac 上 AttributeError）。"""
    p = os.path.abspath(p)
    os.makedirs(p, exist_ok=True)
    if sys.platform == "darwin":
        import subprocess
        subprocess.Popen(["open", p])
    else:
        os.startfile(p)  # noqa 仅 Windows 分支
