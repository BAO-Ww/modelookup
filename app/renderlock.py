# -*- coding: utf-8 -*-
"""renderlock.py — 渲染全局互斥（2026-11-04 用户令「加一把锁挡住串图」）。

背景：pei_ai9.py 有十几处模块级全局（BG_DIR / _TONE / GEN_STYLE / POP_FLAVOR /
BAND_MAXW / TX1..TX3 / SCRIM_* / TUNE …），设计上「一次只画一个型号」。两个窗
同时出图、或聚光后台复检（pubwork._qa_bg）撞上新任务时，B 的品类/底色会顶掉 A
→ A 的图套上 B 的背景和配色（串味）。

做法：所有出图入口统一过一把 RLock——拿到锁才画，拿不到就排队等（不是报错）。
用 RLock 是为了同线程可重入：generate 内部再调别的出图入口时不会自己锁死。
超时 15 分钟兜底（正常出图最慢 1~3 分钟，2026-11-10 由 10 分钟放宽：连点多型号
+限流退避叠加时 10 分钟会误杀排队任务），超了抛错让 jobman 记成失败，
绝不「等不到就硬画」——宁可失败也不出一张串味的图。
"""
import functools
import threading

RENDER_LOCK = threading.RLock()

_WAIT_TIMEOUT = 900          # 排队上限（秒）；2026-11-10 600→900：用户连点 3 个型号时
                             # 限流退避 + 串行渲染叠加，10 分钟兜底会误杀第 3 个任务


def serialized(fn):
    """出图入口排队装饰器：@serialized 加在 kitset.generate / white_hero、
    pubwork.gen_style / gen_hero / _auto_qa_spot 上。"""
    @functools.wraps(fn)
    def _w(*args, **kw):
        if not RENDER_LOCK.acquire(blocking=False):
            cb = kw.get("log") or kw.get("prog")
            try:
                if cb:
                    cb("前一个出图任务还没完，排队等它（防串图）…")
            except Exception:
                pass
            if not RENDER_LOCK.acquire(timeout=_WAIT_TIMEOUT):
                raise RuntimeError("出图排队超过 15 分钟，先算了（稍后再点一次）")
        try:
            return fn(*args, **kw)
        finally:
            RENDER_LOCK.release()
    return _w
