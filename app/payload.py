# -*- coding: utf-8 -*-
"""payload 构建/缓存/渲染（2026-10-11 拆分一期：从 main.py 原样搬出，纯搬运零行为变化）。
依赖方向：… → parsing → payload（parsing 之上、net/cloud 之下）。
缓存 _PAY/_DATA_JSON/_IMG_BYTES 是本模块私有全局（拆分设计.md 的 state.py 归属
在此按「只被本域读写 → 模块私有」收口，行为一致）。"""
import base64
import io
import json
import os
import threading
import time

import state
from config import CLEAN_XLSX, PRICE_XLSX, PAYLOAD_VER, SOURCES, ONLINE_SHARE, FILE_ID, SUBSIDY_XLSX
from parsers_yr import load_records_from_path, merge_records, parse_subsidy_rows
from normalize import norm_model
from paths import rp, wp


def load_img_library():
    """全量图库：打包进 assets 的 images_cache.json（core → dataURL）。"""
    p = rp("images_cache.json")
    if os.path.isfile(p):
        try:
            with io.open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def build_payload(allrecs, imgs_cache, month=""):
    rows = merge_records(allrecs)
    imgs = {ck: v for ck, v in (imgs_cache or {}).items()
            if ck in rows and isinstance(v, str) and v.startswith("data:image/")}
    now = time.strftime("%Y-%m-%d %H:%M")
    return {
        "meta": {"month": month, "src": "", "built": now,
                 "online": {"share": ONLINE_SHARE, "fileId": FILE_ID},
                 "sources": SOURCES},
        "rows": sorted(rows.values(),
                       key=lambda r: (r.get("b") or "", r.get("cat") or "", r["mo"])),
        "imgs": imgs,
    }


_PAY = None
_PAY_LOCK = threading.Lock()
_DATA_JSON = None
_IMG_BYTES = {}


def get_payload():
    global _PAY
    with _PAY_LOCK:
        if _PAY is None:
            _PAY = load_current_payload()
        return _PAY


def set_payload(p):
    global _PAY, _DATA_JSON
    with _PAY_LOCK:
        p.setdefault("meta", {})["pv"] = PAYLOAD_VER
        _PAY = p
        _DATA_JSON = None
        _IMG_BYTES.clear()
        with state.SLOCK:
            state.STATE["rev"] += 1          # 数据换新 → 前端轮询到就无感重载


def api_data_bytes():
    """给页面的数据：只有 meta + rows + 图片 key 列表（不含图本身）。"""
    global _DATA_JSON
    if _DATA_JSON is None:
        p = get_payload()
        keys = [k for k, v in (p.get("imgs") or {}).items() if isinstance(v, str)]
        body = json.dumps({"meta": p.get("meta") or {}, "rows": p.get("rows") or [],
                           "imgkeys": keys,
                           "web": p.get("web") or {}}, ensure_ascii=False, separators=(",", ":"))
        _DATA_JSON = body.encode("utf-8")
    return _DATA_JSON


def img_bytes(core):
    """按需给一张机图（首次从 dataURL 解出来，之后走内存缓存）。"""
    b = _IMG_BYTES.get(core)
    if b is not None:
        return b if b else None, ""
    v = (get_payload().get("imgs") or {}).get(core)
    if not isinstance(v, str) or not v.startswith("data:image/"):
        _IMG_BYTES[core] = b""
        return None, ""
    head, _, b64 = v.partition(",")
    ctype = (head[5:].split(";")[0] or "image/jpeg")
    try:
        raw = base64.b64decode(b64)
    except Exception:
        _IMG_BYTES[core] = b""
        return None, ""
    _IMG_BYTES[core] = raw
    return raw, ctype


def render(payload, template=None):
    """把数据内联进模板 → 离线单文件 HTML（exe 版不用这条路径）。"""
    if template is None:
        with io.open(rp("template.html"), encoding="utf-8") as f:
            template = f.read()
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    raw = raw.replace("</", "<\\/")
    if "/*__DATA__*/null" not in template:
        raise RuntimeError("模板缺少 /*__DATA__*/null 占位")
    return template.replace("/*__DATA__*/null", raw, 1)


def bootstrap():
    """写查询页。exe 版**只写模板**（几十 KB）——数据走 /api/data、图片走 /img/。

    ⚠ 这里以前是把 payload 内联进 HTML，页面 1.9MB，WebView2 光解析首屏就要 4 秒。
      改成「骨架页先行」后窗口 1.5 秒左右就出来了。
    """
    with io.open(rp("template.html"), encoding="utf-8") as f:
        tpl = f.read()
    with io.open(wp("index.html"), "w", encoding="utf-8") as f:
        f.write(tpl)
    return wp("index.html")


def load_current_payload():
    """当前数据：优先工作目录里的（更新后落在那），否则用 exe 内嵌的。

    ⚠ 工作目录缓存带 meta.pv 版本号：与代码里的 PAYLOAD_VER 不一致就视为过期
      （老版本升级上来时字段结构可能变了），自动改用 exe 内嵌的新数据。
    """
    p = wp("payload.json")
    if os.path.isfile(p):
        try:
            with io.open(p, encoding="utf-8") as f:
                d = json.load(f)
            if (d.get("meta") or {}).get("pv") == PAYLOAD_VER:
                return d
        except Exception:
            pass
    p = rp("payload.json")
    with io.open(p, encoding="utf-8") as f:
        return json.load(f)


def attach_subsidy(payload, log):
    """把以旧换新表的 69码+售价 挂到 rows 上（r['sub2']）。匹配不到的型号不动。"""
    p = wp(SUBSIDY_XLSX)
    if not os.path.isfile(p):
        return
    try:
        subs = parse_subsidy_rows(p)
    except Exception as e:
        log("补贴表解析失败：%s（这轮不带补贴数据）" % e)
        return
    if not subs:
        log("补贴表里没解析出数据（列名可能变了）")
        return
    exact = {}
    for s in subs:
        exact.setdefault(s["mo"], s)      # 同型号取第一条
    # 兜底：尾号索引（如主表 CXW-358-C6Q90AGUD ↔ 表里 C6Q90AGUD 这类短型号）
    tails = {}
    for s in subs:
        if len(s["mo"]) >= 6:
            tails.setdefault(s["mo"][-6:], s)
    n_hit = 0
    for r in payload.get("rows") or []:
        k = norm_model(r.get("mo"))
        s = exact.get(k)
        if not s and len(k) >= 6:
            s = tails.get(k[-6:])         # 兜底：主表型号的 6 位尾号对表里的短型号
        if s:
            r["sub2"] = {"c": s["c"], "p": s["p"]}
            n_hit += 1
    log("补贴表：%d 条 → 命中表中 %d 个型号" % (len(subs), n_hit))


def rebuild(log):
    """用工作目录里的 data.xlsx / price.xlsx 重建 payload。"""
    allrecs, month = [], ""
    for name in (CLEAN_XLSX, PRICE_XLSX):
        p = wp(name)
        if not os.path.isfile(p):
            continue
        try:
            recs, mo = load_records_from_path(p)
            allrecs += recs
            month = month or mo
            log("%s → %d 条记录" % (name, len(recs)))
        except Exception as e:
            log("%s 解析失败：%s" % (name, e))

    if not allrecs:
        raise RuntimeError("没有可用的数据文件（data.xlsx / price.xlsx 都不在）")

    cache = load_img_library()
    old = None
    try:
        old = load_current_payload()
    except Exception:
        pass
    if old:
        merged = dict(cache)
        merged.update(old.get("imgs") or {})      # 老 payload 里的图兜底
        cache = merged

    payload = build_payload(allrecs, cache, month)
    payload["meta"]["src"] = ""
    attach_subsidy(payload, log)          # 补贴表（以旧换新）：69码 + 售价
    # 官网详情页直链（web 映射）从老 payload 继承，按新 rows 的 key 过滤
    if old and old.get("web"):
        _web = {r.get("c"): old["web"][r.get("c")] for r in payload["rows"] if r.get("c") in old["web"]}
        if _web:
            payload["web"] = _web
    set_payload(payload)
    with io.open(wp("payload.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)      # 记为「当前数据」，下次启动沿用
    log("重建完成：%d 个型号 / %d 张机图" % (len(payload["rows"]), len(payload["imgs"])))
    state.set_state(built=payload["meta"]["built"])
    return payload
