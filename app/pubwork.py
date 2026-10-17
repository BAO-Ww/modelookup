# -*- coding: utf-8 -*-
"""pubwork.py — 发布图工厂编排层（2026-10-17，XianyuPeiwen 整体并入型号查询）

把闲鱼配文 Pro 的全部生成能力接到型号查询的 HTTP 服务上：
  素材抓取（官网三站+兴达+参数图识别）→ 5 风格 AI 套图（pei_ai9）→ 促销首图（pei_hero）
  → 图片库分组 → 保存精选（按勾选发布顺序）→ AI 成片自检/自动优化 → 推 GitHub 私有仓库

铁律（沿用 peiwen 定稿）：
  · 机器像素不动：官方透明图直接裁 bbox 用（pei_ai9.load_cut），不重抠
  · 背景 = CogView 实时生成，按机型+风格缓存到 素材/<型号>/AI背景/
  · 素材根 = wp("素材")（可写目录，源码/exe 通用）
  · 线程模型：HTTP 线程只入队，重活在 daemon 线程跑，前端轮询 /api/pubjob
"""
import base64
import json
import os
import re
import shutil
import threading

from paths import wp

# ---------------------------------------------------------------- 素材根与型号目录

_ROOT = [None]


def sroot():
    if not _ROOT[0]:
        # 2026-10-17：目录名用 ASCII「peiwork」——沙箱/安全软件对 AppData 下中文目录名
        # 会拦（WinError 5 实锤）；界面文案仍叫「素材文件夹」，行为不变
        d = wp("peiwork")
        os.makedirs(d, exist_ok=True)
        _ROOT[0] = d
    return _ROOT[0]


def _safe(model):
    """型号做目录名：去掉文件系统不安全字符"""
    return re.sub(r'[\\/:*?"<>|]+', "", (model or "").strip()) or "未命名"


def mdir(model):
    return os.path.join(sroot(), _safe(model))


# ---------------------------------------------------------------- 后台任务状态（前端轮询）

JOB = {"running": False, "model": "", "what": "", "pct": 0, "msg": "",
       "ok": None, "err": "", "t": 0}
_JLOCK = threading.Lock()


def _job_begin(model, what):
    with _JLOCK:
        JOB.update({"running": True, "model": model, "what": what, "pct": 1,
                    "msg": "准备中 ...", "ok": None, "err": "", "t": 0})


def _job_prog(pct, msg):
    with _JLOCK:
        JOB["pct"] = max(1, min(100, int(pct)))
        if msg:
            JOB["msg"] = msg


def _job_end(ok, err=""):
    with _JLOCK:
        JOB["running"] = False
        JOB["pct"] = 100 if ok else JOB["pct"]
        JOB["ok"] = bool(ok)
        JOB["err"] = err or ""


def job_state():
    with _JLOCK:
        return dict(JOB)


def _logf():
    try:
        import state
        return lambda s: state.LOG.append("[发布图] " + s)
    except Exception:
        return lambda s: None


def _ai_key():
    try:
        import copywriter
        return (copywriter._load_glm_key() or "").strip()
    except Exception:
        return ""


# ---------------------------------------------------------------- 素材抓取（对齐 app.py._work）

def _normalize_info(info):
    """旧版 info.json 或缺字段的数据统一补默认值（peiwen app._normalize_info 原样）"""
    if not isinstance(info, dict):
        return {}
    for k, dv in (("brand", ""), ("model", ""), ("title", ""), ("cat", ""),
                  ("avail", ""), ("site", ""), ("url", "")):
        if not isinstance(info.get(k), str):
            info[k] = dv
    try:
        info["price"] = int(info.get("price") or 0)
    except Exception:
        info["price"] = 0
    props = info.get("props")
    if not isinstance(props, (list, tuple)):
        info["props"] = []
    else:
        info["props"] = [(str(a), str(b)) for a, b in props if a and b]
    return info


def load_info(model):
    """本地已有素材直接载入（不联网）；没有返回 None"""
    d = mdir(model)
    p = os.path.join(d, "info.json")
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as f:
                info = json.load(f)
            info = _normalize_info(info)
            info["dir"] = d
            return info
        except Exception:
            pass
    return None


def _save_info(info):
    try:
        with open(os.path.join(info["dir"], "info.json"), "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def work(model, prog=None):
    """抓素材：本地有就直接用；没有走 peiwen 抓取链（新爬虫→旧检索→兴达），存 素材/<型号>/"""
    log = _logf()
    prog = prog or (lambda p, m: None)
    d = mdir(model)
    info = load_info(model)
    if info:
        # 遗留修复：参数为空且有官网链接 → 补抓一次（peiwen 同款，只在空参数时触发）
        if not (info.get("props") or []) and info.get("url"):
            try:
                import pei_core
                info = pei_core.spider_backfill(info, log=log) or info
                _save_info(info)
            except Exception as e:
                log("补抓参数失败（%s），仍用原数据" % e)
        return info
    os.makedirs(d, exist_ok=True)
    import pei_core
    import pei_xdjdy
    prog(8, "定位产品页 ...")
    log("定位产品页 ...")
    site, url, prod = pei_core.spider_locate(model, log=log)
    if not url:
        prog(15, "新爬虫未命中，回退旧检索 ...")
        site, url = pei_core.find_page(model, sites=("haier", "casarte", "leader", "xdjdy"), log=log)
        prod = None
    if not url:
        raise RuntimeError("没找到这个型号，检查一下型号是否正确（海尔/卡萨帝/统帅/实体店）")
    prog(30, "抓取详情页与图片 ...")
    if site == "xdjdy":
        info = pei_xdjdy.fetch_xdjdy(url, d, log=log, progress=prog)
    else:
        info = pei_core.fetch_product(site, url, d, log=log, progress=prog)
        if info:
            info = pei_core.spider_enrich_info(info, site, url, prod, log=log)
    if not info:
        raise RuntimeError("页面抓取失败，稍后再试")
    info = _normalize_info(info)
    info["dir"] = d
    _save_info(info)
    _enrich_specs(model, info, prog)
    return info


def _enrich_specs(model, info, prog=None):
    """参数不足（<6 项）时补：兴达官方参数图视觉识别（peiwen app._enrich_info 同款）"""
    try:
        props = info.get("props") or []
        if len(props) >= 6:
            return
        prog and prog(80, "参数较少（%d 项），从官方参数图识别 ..." % len(props))
        import pei_hero
        d = info["dir"]
        imgs = [os.path.join(d, f) for f in sorted(os.listdir(d))
                if f.lower().endswith((".jpg", ".jpeg", ".png"))]
        newp = pei_hero.enrich_specs(info, api_key=_ai_key(), log=_logf(), img_paths=imgs)
        if newp and len(newp) > len(props):
            info["props"] = newp
            _save_info(info)
    except Exception as e:
        _logf()("参数补充失败：%s" % e)


# ---------------------------------------------------------------- 生成（套图 / 首图）

# peiwen app.py 原样：5 风格 → 目录名 / 中文名；每套 6 张精修（槽位 1,2,3,4,6,9）
SLOTS6 = [1, 2, 3, 4, 6, 9]
STYLE_DIRS = {"studio": "AI6张", "spot": "AI6张_聚光", "white": "AI6张_亮白",
              "pure": "AI6张_纯白", "scene": "AI6张_实景"}
STYLE_NAMES = {"studio": "深灰影棚", "spot": "暗角聚光", "white": "亮白影棚",
               "pure": "纯白", "scene": "实景合成"}

# 最近一次生成（自检/自动优化要用：出图路径 + 输出目录 + 风格）
_LAST = {}
_LAST_LOCK = threading.Lock()


def _last_set(model, paths, out, style):
    with _LAST_LOCK:
        _LAST[_safe(model)] = {"paths": list(paths or []), "out": out, "style": style}


def _last_get(model):
    with _LAST_LOCK:
        return dict(_LAST.get(_safe(model)) or {})


def gen_style(model, style, sell_price=None, version="N"):
    """生成一套 6 张精修（后台线程跑，JOB 汇报进度）"""
    name = STYLE_NAMES.get(style, style)
    _job_begin(model, "发布图 · " + name)
    try:
        info = load_info(model) or work(model, prog=_job_prog)
        import pei_ai9
        src = info["dir"]
        out = os.path.join(src, STYLE_DIRS.get(style, style))
        paths = pei_ai9.generate(info, src, out, version=version, sell_price=sell_price,
                                 api_key=_ai_key(), progress=_job_prog, style=style,
                                 slots=SLOTS6, overview=False)
        _last_set(model, paths, out, style)
        _logf()("发布图·%s 生成完成：%d 张 -> %s" % (name, len(paths), out))
        _job_end(True)
    except Exception as e:
        _logf()("发布图·%s 生成失败：%s" % (name, e))
        _job_end(False, str(e))


def gen_hero(model, sell_price=None, version="N"):
    """促销首图（深色版 2048）：发布图/0_主图_卖点拼版.jpg"""
    _job_begin(model, "促销首图")
    try:
        info = load_info(model) or work(model, prog=_job_prog)
        import pei_hero
        src = info["dir"]
        sel = os.path.join(src, "发布图")
        os.makedirs(sel, exist_ok=True)
        out = os.path.join(sel, "0_主图_卖点拼版.jpg")
        prod_img = pei_hero.pick_product_img(info, src)
        if not prod_img:
            raise RuntimeError("没找到可用的产品图")
        pei_hero.make_hero_promo(prod_img, info, out, ship=version, sell_price=sell_price,
                                 api_key=_ai_key(), log=lambda s: _job_prog(55, s))
        _job_prog(100, "首图完成")
        _logf()("促销首图已生成：%s" % out)
        _job_end(True)
    except Exception as e:
        _logf()("促销首图生成失败：%s" % e)
        _job_end(False, str(e))


# ---------------------------------------------------------------- 图片库 / 精选

def _img_files(d):
    if not os.path.isdir(d):
        return []
    return [f for f in sorted(os.listdir(d))
            if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".bmp"))
            and not f.startswith("_") and not f.startswith(".")]


def _seed_from_copy(model):
    """文案生成器（copywriter）已抓过的官网图播种进素材库：复制到 素材/<型号>/ 根目录，
    免得用户在文案窗口点过「写文案」后还要重复抓一遍。只补根目录原图，不动成品目录。"""
    d = mdir(model)
    try:
        import copywriter
        cdir = os.path.join(copywriter.copy_root(), copywriter.norm_model(model) or model)
        if not os.path.isdir(cdir):
            return
        have = set(_img_files(d))
        n = 0
        for f in sorted(os.listdir(cdir)):
            if not f.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                continue
            if f.startswith("_") or f.startswith(".") or os.path.isdir(os.path.join(cdir, f)):
                continue
            if f in have:
                continue
            try:
                shutil.copy(os.path.join(cdir, f), os.path.join(d, f))
                n += 1
            except Exception:
                pass
        if n:
            _logf()("从文案图片库播种 %d 张官网图进素材库" % n)
    except Exception:
        pass


def lib(model):
    """图片库分组（peiwen LIB_GROUPS 口径）：成品风格组在前、原图素材垫底。
    文件一律给相对路径（可能带子目录），前端拼 /pubimg/<型号>/<rel>"""
    _seed_from_copy(model)
    d = mdir(model)
    groups = []
    for sub in ("AI6张", "AI6张_聚光", "AI6张_亮白", "AI6张_纯白", "AI6张_实景", "发布图"):
        label = {"AI6张": "发布图 · 深灰影棚", "AI6张_聚光": "发布图 · 暗角聚光",
                 "AI6张_亮白": "发布图 · 亮白影棚", "AI6张_纯白": "发布图 · 纯白",
                 "AI6张_实景": "发布图 · 实景合成", "发布图": "保存精选 / 促销首图"}.get(sub, sub)
        fs = _img_files(os.path.join(d, sub))
        if fs:
            groups.append({"label": label, "sub": sub,
                           "files": ["%s/%s" % (sub, f) for f in fs]})
    raw = _img_files(d)
    if raw:
        groups.append({"label": "原图素材", "sub": "", "files": raw})
    return {"ok": True, "dir": d, "has_info": os.path.isfile(os.path.join(d, "info.json")),
            "groups": groups, "job": job_state()}


def sel_save(model, rels, sell_price=None, version="N"):
    """保存精选：按勾选顺序把选中图复制进 发布图/（N_原名.ext），
    第一张另出促销首图 0_主图_卖点拼版.jpg（peiwen save_selected 同款）"""
    import pei_hero
    info = load_info(model)
    if not info:
        raise RuntimeError("先抓素材再保存精选")
    src = info["dir"]
    sel = os.path.join(src, "发布图")
    os.makedirs(sel, exist_ok=True)
    for f in os.listdir(sel):
        try:
            os.remove(os.path.join(sel, f))
        except Exception:
            pass
    if not rels:
        raise RuntimeError("先勾选要发布的图")
    n = 0
    first = rels[0]
    for rel in rels:
        sp = os.path.join(src, rel.replace("/", os.sep))
        if not os.path.abspath(sp).startswith(os.path.abspath(src)) or not os.path.isfile(sp):
            continue
        if rel == first:
            continue
        ext = os.path.splitext(rel)[1]
        dst = os.path.join(sel, "%d_%s%s" % (n + 1, os.path.splitext(os.path.basename(rel))[0], ext))
        shutil.copy(sp, dst)
        n += 1
    # 首图统一深色促销风（peiwen 2026-09 定稿）
    try:
        cover = os.path.join(sel, "0_主图_卖点拼版.jpg")
        prod_img = os.path.join(src, first.replace("/", os.sep))
        if os.path.exists(prod_img):
            pei_hero.make_hero_promo(prod_img, info, cover, ship=version,
                                     sell_price=sell_price, api_key=_ai_key())
            n += 1
    except Exception as e:
        _logf()("精选首图生成失败（其余已保存）：%s" % e)
    try:
        os.startfile(sel)  # noqa
    except Exception:
        pass
    return {"ok": True, "count": n, "dir": sel}


# ---------------------------------------------------------------- AI 自检 / 自动优化

def qa_run(model):
    """AI 成片自检：对最近一次生成的套图出报告（peiwen：core.qa_render）"""
    last = _last_get(model)
    paths = last.get("paths") or []
    if not paths:
        raise RuntimeError("先点风格按钮生成发布图，再做自检")
    import pei_core
    res = pei_core.qa_render(_ai_key(), paths, load_info(model))
    if res is None:
        raise RuntimeError("视觉模型暂不可用，无法自检")
    return {"ok": True, "issues": res}


def qa_auto(model):
    """自检 + 自动优化（确定性布局类：文字压机器/机器被裁/文字截断，重渲问题槽位）"""
    last = _last_get(model)
    paths, out, style = last.get("paths") or [], last.get("out"), last.get("style")
    if not paths or not out or not style:
        raise RuntimeError("先点风格按钮生成发布图，再做自动优化")
    import pei_core
    import pei_ai9
    res = pei_core.qa_render(_ai_key(), paths, load_info(model))
    if res is None:
        raise RuntimeError("视觉模型暂不可用，无法自检+自动优化")
    info = load_info(model)
    r = pei_ai9.auto_optimize(_ai_key(), info, info["dir"], out, style, res,
                              rounds=2, progress=_job_prog)
    new = pei_core.qa_render(_ai_key(), paths, info)
    return {"ok": True, "issues": (new if new is not None else res), "optimized": r}


# ---------------------------------------------------------------- 导入图片

def import_imgs(model, files):
    """导入本地图片进素材目录（dataURL 上传），命名 I01/I02… 续号"""
    d = mdir(model)
    os.makedirs(d, exist_ok=True)
    existing = len([f for f in os.listdir(d)
                    if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".bmp"))])
    n = 0
    for x in (files or []):
        name = x.get("name") or ("img.jpg")
        b64 = x.get("b64") or ""
        if not b64:
            continue
        if "," in b64:                       # dataURL 前缀剥掉
            b64 = b64.split(",", 1)[1]
        ext = os.path.splitext(name)[1].lower()
        if ext not in (".jpg", ".jpeg", ".png", ".webp", ".bmp"):
            ext = ".jpg"
        try:
            raw = base64.b64decode(b64)
            if not raw:
                continue
            with open(os.path.join(d, "I%02d%s" % (existing + n + 1, ext)), "wb") as f:
                f.write(raw)
            n += 1
        except Exception:
            continue
    return {"ok": True, "count": n}


# ---------------------------------------------------------------- 推 GitHub 私有仓库

def push_gh(model, only_sel=False):
    """把 素材/<型号>/（或仅 发布图/精选）推到 GitHub 私有仓库（pub_gh：建仓+追加提交）"""
    d = mdir(model)
    if not os.path.isdir(d):
        raise RuntimeError("素材目录不存在：%s" % d)
    sel_dir = os.path.join(d, "发布图") if only_sel else d
    info = load_info(model) or {}
    task = {"sel_dir": sel_dir, "model": _safe(model), "version": info.get("_ver", "N")}
    tp = os.path.join(d, "_publish_task.json")
    with open(tp, "w", encoding="utf-8") as f:
        json.dump(task, f, ensure_ascii=False)
    import pub_gh
    url = pub_gh.run(tp)
    return {"ok": True, "url": url}


# ---------------------------------------------------------------- 一键发布到闲鱼（半自动）

def _copy_cache(model):
    """从文案生成器缓存取标题/描述（正品优先，样机兜底）；没有返回 ({}, '')"""
    import copywriter
    nm = copywriter.norm_model(model) or model
    for kk in ("l", "s"):
        p = os.path.join(copywriter.copy_root(), "%s_%s_v4.json" % (nm, kk))
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as f:
                    return json.load(f), kk
            except Exception:
                pass
    return {}, "l"


def pub_xy(model, version="N"):
    """半自动发布到闲鱼（peiwen publish_to_xianyu 同款）：playwright 打开发布页，
    自动填标题/描述/图片，用户最后点发布。依赖本机 pip install playwright + chromium。"""
    _job_begin(model, "一键发布闲鱼")
    try:
        import contextlib
        import io as _io
        info = load_info(model)
        if not info:
            raise RuntimeError("先抓素材再发布")
        sel = os.path.join(mdir(model), "发布图")
        if not os.path.isdir(sel) or not _img_files(sel):
            raise RuntimeError("发布图文件夹是空的——先生成发布图或保存精选")
        cache, _kk = _copy_cache(model)
        titles = cache.get("titles") or []
        desc = (cache.get("desc_rep") if version == "B" else cache.get("desc_nat")) or ""
        if not titles:
            raise RuntimeError("文案缓存里没有标题——先在「文案」页生成文案")
        task = {"sel_dir": sel, "version": version,
                "info": {"model": model, "brand": info.get("brand"), "price": info.get("price")},
                "desc": desc, "titles": titles}
        tp = os.path.join(mdir(model), "_xy_task.json")
        with open(tp, "w", encoding="utf-8") as f:
            json.dump(task, f, ensure_ascii=False)
        import pei_xianyu
        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf):
            pei_xianyu.main_from_task(tp)          # 阻塞到用户在浏览器里发完/关页
        for line in buf.getvalue().splitlines():
            if line.strip():
                _logf()("[发布闲鱼] " + line.strip())
        _job_end(True)
    except Exception as e:
        _logf()("一键发布闲鱼失败：%s" % e)
        _job_end(False, str(e))


def open_dir(model, sub=""):
    d = os.path.join(mdir(model), sub.replace("/", os.sep)) if sub else mdir(model)
    os.makedirs(d, exist_ok=True)
    os.startfile(d)  # noqa
    return {"ok": True}
