# -*- coding: utf-8 -*-
"""窗口引用容器（2026-10-11 拆分一期，新写 ~20 行）。
依赖方向：被 kdocs/yyclient/updater/server/appwin 四方 import，自己零依赖。

⚠ MAIN_WIN/LOGIN_WIN/YY_WIN 会被 run_app 重新赋值——任何模块都不要
`from winref import MAIN_WIN`（拿到的是 None 旧引用），一律 `import winref`
后读写 `winref.MAIN_WIN`。两个 Event 只调方法不换对象，同样走属性访问最稳。
"""
import threading

MAIN_WIN = None        # 主窗口（pywebview Window，native 异步创建）
LOGIN_WIN = None       # 金山文档登录/取数窗口（先建好藏着）
LOGIN_CLOSED = threading.Event()
YY_WIN = None          # 用友登录窗口（先建好藏着）
YY_CLOSED = threading.Event()


def js_eval(win, script, default=None):
    """窗口里跑 JS 的统一兜底入口（原 main.py 的 js_eval，窗口引用域的天然成员）。"""
    if win is None:
        return default
    try:
        return win.evaluate_js(script)
    except Exception:
        return default
