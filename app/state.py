# -*- coding: utf-8 -*-
"""全部可变全局状态（2026-10-11 拆分一期：从 main.py 原样搬出，纯搬运零行为变化）。
依赖方向：config → paths → state（只依赖标准库 + config + paths）。

⚠ 访问规矩（拆分成败的关键）：
- 可变对象（STATE/STOCK/PERM_STATE/LOG/_ALLRUN…）改**内容**随便，但**禁止重新绑定**——
  其他模块 `from state import X` 拿到的是引用，重新赋值会让别人读到旧对象。
- 需要整体替换的（STOCK 在 stock_load/stock_pull_cloud/yy_finish 里整体换新）：
  一律 `import state` 后写 `state.STOCK = {...}`（模块属性赋值），读也走 `state.STOCK`。
- 函数式入口（set_state/read_state/_tick/_all_stage/stock_load/stock_save/price_load/has_perm）
  从这里 import，行为与拆分前一致。

与拆分设计.md 的一处有意偏差：payload 缓存（_PAY/_PAY_LOCK/_DATA_JSON/_IMG_BYTES）
不进本模块——它们只被 payload 域的 4 个函数读写，随 payload.py 走模块私有更干净。
"""
import io
import json
import os
import threading
import time

from config import APP_ID, YY_STOCK_FILE
from paths import wp

# ---- 启动耗时埋点（设 ML_TIMING=1 才写日志，平时零开销） ----
_T0 = time.time()
_TIMING = []


def _tick(label):
    _TIMING.append((label, time.time() - _T0))
    if not os.environ.get("ML_TIMING"):
        return
    try:
        # 2026-10-11 Mac 端：统一走 wp()（Windows 上与原 LOCALAPPDATA 路径逐字相同）
        with io.open(wp("boot_timing.log"), "a", encoding="utf-8") as f:
            f.write("%7.2f s  %s\n" % (time.time() - _T0, label))
    except Exception:
        pass


# ------------------------------------------------------------------ 更新状态

STATE = {"status": "idle", "msg": "", "built": "", "rev": 0,
         "price_at": "", "stock_at": "", "alert_ts": 0, "alert_msg": ""}
SLOCK = threading.Lock()
LOG = []

# 更新流程互斥（金山表格同步 / 本机更新入口共用一把）
_update_lock = threading.Lock()


def set_state(status=None, msg=None, rev=None, built=None,
              price_at=None, stock_at=None, alert_ts=None, alert_msg=None):
    with SLOCK:
        if status is not None:
            STATE["status"] = status
        if msg is not None:
            STATE["msg"] = msg
        if rev is not None:
            STATE["rev"] = rev
        if built is not None:
            STATE["built"] = built
        if price_at is not None:
            STATE["price_at"] = price_at
            try:                            # 2026-10-09：型号库时间持久化，重启不丢
                with io.open(wp("price_ts.txt"), "w", encoding="utf-8") as f:
                    f.write(price_at)
            except Exception:
                pass
        if stock_at is not None:
            STATE["stock_at"] = stock_at
        if alert_ts is not None:
            STATE["alert_ts"] = alert_ts
        if alert_msg is not None:
            STATE["alert_msg"] = alert_msg


def read_state():
    with SLOCK:
        return dict(STATE)


# ------------------------------------------------------------------ 实时库存缓存 + 权限

STOCK = {"ts": None, "rows": 0, "stock": {}}
STOCK_LOCK = threading.Lock()

# 当前登录者的权限（页面登录后 POST /api/perm 上报）—— 原生菜单点按的时候校验用
PERM_STATE = {"u": None, "role": None, "perm": {}}


def has_perm(k):
    """菜单/按钮权限：管理号全开；子账号按云端下发的权限表。"""
    if PERM_STATE.get("role") == "admin":
        return True
    return bool(PERM_STATE.get("perm", {}).get(k))


def price_load():
    """启动时把上次的型号库更新时间读回侧栏（重启不丢）。"""
    p = wp("price_ts.txt")
    if os.path.isfile(p):
        try:
            t = io.open(p, encoding="utf-8").read().strip()
            if t:
                STATE["price_at"] = t
        except Exception:
            pass


def stock_load():
    """启动时把上次的实时库存读回内存（没有就当没有）。"""
    global STOCK
    p = wp(YY_STOCK_FILE)
    if os.path.isfile(p):
        try:
            with io.open(p, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d.get("stock"), dict):
                STOCK = {"ts": d.get("ts"), "rows": d.get("rows") or 0, "stock": d["stock"],
                         "sbc": d.get("sbc") or None}
                if d.get("ts"):
                    STATE["stock_at"] = d["ts"]      # 侧栏一打开就显示上次的库存时间
        except Exception:
            pass


def stock_save():
    with STOCK_LOCK:
        try:
            with io.open(wp(YY_STOCK_FILE), "w", encoding="utf-8") as f:
                json.dump(STOCK, f, ensure_ascii=False)
        except Exception:
            pass


# ------------------------------------------------------------------ 「全部更新」进度条状态

_ALLRUN = [False, "", "", "", 0, 0.0]   # [在跑, 错误, 摘要, 阶段文案, 估算进度%, 开始时间] —— 进度条用（2026-10-09）


def _all_stage(stage, pct):
    """更新全部更新的阶段文案与估算进度（前端进度条轮询用）。"""
    _ALLRUN[3] = stage
    _ALLRUN[4] = max(int(_ALLRUN[4] or 0), int(pct))   # 只进不退


_UPD_DETAIL = [""]             # 最近一次金山表格更新的明细（do_update 填）
_STK_DETAIL = [""]             # 最近一次用友库存更新的明细（do_stock_update 填）
