# -*- coding: utf-8 -*-
"""jobman.py — 轻量后台任务槽（2026-10-25）。
背景：macOS WKWebView 对 fetch 有 ~60 秒默认超时（超时报 "Load failed"），
写文案（1-2 分钟）/蓝底套图（1-2 分钟）/AI 自检（几十秒）这类同步长接口在 Mac 上必炸。
对策：一律「submit 起后台线程 → 前端每 1.5s 轮询 state → done 后取 res」。
Windows 行为同步接口保留不变（旧前端兼容）。"""
import threading

_JOBS = {}
_LOCK = threading.Lock()

_EMPTY = {"running": False, "done": False, "ok": None, "res": None, "err": ""}


def submit(key, fn, *args, **kw):
    """按 key 起后台任务；该 key 已有任务在跑时返回 False（不重复起）。"""
    with _LOCK:
        j = _JOBS.get(key)
        if j and j.get("running"):
            return False
        _JOBS[key] = {"running": True, "done": False, "ok": None, "res": None, "err": ""}
    log = kw.pop("_log", None)

    def _run():
        try:
            r = fn(*args, **kw)
            ok = bool(r.get("ok", True)) if isinstance(r, dict) else True
            with _LOCK:
                _JOBS[key].update(running=False, done=True, ok=ok, res=r)
        except Exception as e:
            if log:
                try:
                    log("任务失败：%r" % e)
                except Exception:
                    pass
            with _LOCK:
                _JOBS[key].update(running=False, done=True, ok=False, res=None, err=str(e))

    threading.Thread(target=_run, daemon=True).start()
    return True


def state(key):
    with _LOCK:
        j = _JOBS.get(key)
        return dict(j) if j else dict(_EMPTY)
