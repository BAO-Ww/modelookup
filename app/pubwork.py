# -*- coding: utf-8 -*-
"""pubwork.py — 发布图工厂编排层（2026-10-17，XianyuPeiwen 整体并入型号查询）

把闲鱼配文 Pro 的全部生成能力接到型号查询的 HTTP 服务上：
  素材抓取（官网三站+兴达+参数图识别）→ 5 风格 AI 套图（pei_ai9）→ 促销首图（pei_hero）
  → 图片库分组 → 保存精选（按勾选发布顺序）→ AI 成片自检/自动优化（2026-10-26 用户令：推 GitHub 归档功能整体删除）

铁律（沿用 peiwen 定稿）：
  · 机器像素不动：官方透明图直接裁 bbox 用（pei_ai9.load_cut），不重抠
  · 背景 = CogView 实时生成，按机型+风格缓存到 素材/<型号>/AI背景/
  · 素材根 = wp("素材")（可写目录，源码/exe 通用）
  · 线程模型：HTTP 线程只入队，重活在 daemon 线程跑，前端轮询 /api/pubjob
"""
import base64
import io
import json
import os
import re
import shutil
import threading
import time

from paths import reveal

# ---------------------------------------------------------------- 素材根与型号目录

_ROOT = [None]


def sroot():
    """素材存储根（2026-10-24 可在界面改；2026-10-31 起默认跟软件安装目录走）。
    图片总根 data_base()/peiwork；总根 = exe 目录\图片库（打包版），settings.json
    设了 data_root → data_root/peiwork。改路径时老数据由 /api/pubrootset 搬运。"""
    if not _ROOT[0]:
        import paths
        d = os.path.join(paths.data_base(), "peiwork")
        os.makedirs(d, exist_ok=True)
        _ROOT[0] = d
    return _ROOT[0]


def reset_root():
    """改存储路径后清缓存，下次 sroot() 读新值"""
    _ROOT[0] = None


_SIZE_CACHE = {"at": 0.0, "base": "", "bytes": -1}


def _dir_bytes(root, limit_files=60000):
    """目录总字节（2026-10-31 图片库占用显示用）：不跟随符号链接、限 6 万文件防卡死。"""
    total = 0
    n = 0
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_size
                            n += 1
                            if n >= limit_files:
                                return total, True
                    except Exception:
                        continue
        except Exception:
            continue
    return total, False


def root_status():
    """存储路径现状（2026-10-31 扩到三个库：peiwork + copy + kitset 一个总根）。
    另带占用/剩余/可写状态，供「图片库路径失效提示 + 占用提示」用（2026-10-31 用户令）。"""
    import paths
    base = paths.data_base()
    try:
        import kitset as _ks
        kdir = _ks.kit_root()
    except Exception:
        kdir = ""
    exists = bool(base) and os.path.isdir(base)
    writable = False
    free = -1
    used = -1
    if exists:
        try:
            probe = os.path.join(base, ".ml_probe")
            with io.open(probe, "w", encoding="utf-8") as f:
                f.write("1")
            os.remove(probe)
            writable = True
        except Exception:
            writable = False
        try:
            import shutil as _sh
            free = int(_sh.disk_usage(base).free)
        except Exception:
            free = -1
        # 占用走缓存（5 分钟），别每次开面板都扫几万个文件
        now = time.time()
        if _SIZE_CACHE["base"] == base and (now - _SIZE_CACHE["at"]) < 300:
            used = _SIZE_CACHE["bytes"]
        else:
            try:
                used, _trunc = _dir_bytes(base)
            except Exception:
                used = -1
            _SIZE_CACHE.update({"at": now, "base": base, "bytes": used})
    return {"ok": True, "base": base, "override": paths.data_root() or "",
            "default": paths.default_base(), "peiwork": sroot(), "kitset": kdir,
            "exists": exists, "writable": writable,
            "used": used, "free": free}


def root_move(new_base):
    """更改图片总根并把老数据搬过去（peiwork / copy / kitset 三个库整体迁移）。
    空串 = 恢复默认（exe 目录\图片库），数据留在原地不往回搬——搬回去反而吓人。"""
    import paths
    new_base = str(new_base or "").strip().strip('"')
    old_base = paths.data_base()
    if not new_base:
        paths.set_data_root(None)
        reset_root()
        return root_status()
    if not os.path.isabs(new_base):
        return {"ok": False, "msg": "请填绝对路径（如 D:\\图片库）"}
    if os.path.abspath(new_base) == os.path.abspath(old_base):
        return root_status()               # 填的就是当前目录：无事可做
    # 新根落定后各库根缓存全部失效
    try:
        import kitset as _ks
        _olds = {"peiwork": sroot(), "copy": __import__("copywriter").copy_root(),
                 "kitset": _ks.kit_root()}
    except Exception:
        _olds = {}
    try:
        os.makedirs(new_base, exist_ok=True)
        for name in ("peiwork", "copy", "kitset"):
            src = _olds.get(name) or os.path.join(old_base, name)
            dst = os.path.join(new_base, name)
            if not os.path.isdir(src):
                continue
            if os.path.isdir(dst):
                if os.listdir(dst):
                    return {"ok": False, "msg": "目标 %s 已有内容，换个空文件夹或先清掉" % dst}
                os.rmdir(dst)          # 空壳（sroot 可能预创建过）拆掉再整目录搬
            shutil.move(src, dst)
    except Exception as e:
        return {"ok": False, "msg": "搬运失败：%s" % e}
    paths.set_data_root(new_base)
    reset_root()
    st = root_status()
    _logf()("图片存储根已迁移 -> %s" % st["base"])
    return st


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
    if not ok:
        # 2026-10-29 Mac 实锤：job 失败只进 job 状态，前端 throw_() 又把 err 吞了，
        # app.log 里啥都没有——远程完全没法查。失败必记日志 + 立即上报诊断（异步不挡）。
        try:
            _logf()("套图任务失败：%s" % (err or "未知错误"))
        except Exception:
            pass
        try:
            import cloud
            threading.Thread(target=cloud.client_log_upload,
                             args=("job:fail " + str(err)[:80],), daemon=True).start()
        except Exception:
            pass


def job_state():
    with _JLOCK:
        return dict(JOB)


def busy_msg():
    """撞锁提示用（2026-10-29 用户令「要知道哪个在跑」）：把在跑任务的
    型号/干什么/进度拼成人话，弹窗直接显示，不再只回死话「已有任务在跑」。"""
    j = job_state()
    if not j.get("running"):
        return "其他任务"
    bits = []
    if j.get("model"):
        bits.append(str(j["model"]))
    if j.get("what"):
        bits.append(str(j["what"]))
    s = "的".join(bits) if bits else "后台任务"
    if j.get("pct"):
        s += "（%d%%）" % int(j["pct"])
    return s


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
    info = None
    if not url:
        # 2026-10-23 用户定稿「型号对不上的就不要了」：官网对不上宁可不抓，绝不抓错型号——
        # 欣盛商城相册兜底（写文案 Pro 同款源）。XQG100-28E 实锤：海尔/统帅官网、兴达都没有，
        # 旧逻辑 LCS 前缀误命中统帅 LHD559WU1 白机页，发布图全成别家机器。
        prog(18, "官网未命中，试欣盛商城 ...")
        log("官网未命中，试欣盛商城 ...")
        try:
            import copywriter
            xs = copywriter.fetch_xinsheng(model, log=log)
        except Exception as e:
            xs = None
            log("欣盛兜底异常：%s" % e)
        album = [u for u in ((xs or {}).get("imgs") or []) if u]
        if not album:
            raise RuntimeError("没找到这个型号，检查一下型号是否正确（海尔/卡萨帝/统帅/实体店）")
        _pn = str(xs.get("pname") or "")
        _brand = next((b for b in ("卡萨帝", "统帅", "海尔") if b in _pn), "")
        try:
            _price = str(int(float(str(xs.get("price") or "0"))))   # 欣盛价 "1899.0" 浮点串
        except Exception:
            _price = "0"
        import pei_core as _pc
        _cat = _pc.guess_cat(_pn)   # 品类从商品名猜（空 cat 会让选图/背景风格走不到品类分支）
        # 整目录重来：能走到这说明没有有效 info.json，目录里只可能是上次失败/错型号的残留
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)
        try:
            imgs = copywriter.dl_images([(u, "欣盛") for u in album], d, log)
        except Exception as e:
            log("欣盛图下载失败：%s" % e)
            raise RuntimeError("欣盛商城图片下载失败，稍后再试")
        info = {"site": "xinsheng", "brand": _brand, "model": model,
                "title": _pn or model, "price": _price,
                "avail": "", "props": [], "url": str(xs.get("url") or ""),
                "cat": _cat, "dir": d, "imgs": imgs, "source": "欣盛商城"}
    else:
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
    # 逐图来源记录（2026-10-24 用户定稿：原图素材按来源分组 + 官网>欣盛>其他用图优先级）
    if not info.get("srcmap"):
        _srcmap = {}
        for _x in (info.get("imgs") or []):   # 欣盛兜底：dl_images 返回 [{f, src}]
            if isinstance(_x, dict) and _x.get("f"):
                _srcmap[str(_x["f"])] = str(_x.get("src") or "欣盛")
        if not _srcmap:
            _st = str(info.get("site") or site or "")
            _src = "欣盛" if _st == "xinsheng" else ("兴达" if _st == "xdjdy" else "官网")
            _srcmap = {f: _src for f in _img_files(d)}
        info["srcmap"] = _srcmap
    info["dir"] = d
    _enrich_specs(model, info, prog)      # 先补参数图（还会往根目录落兴达参数图）
    _organize_sources(d, info)            # 再统一按来源归文件夹（参数图落「其他」）
    _save_info(info)
    return info


_SRC_SUBS = ("官网", "欣盛", "其他")


def _src_sub_of(s):
    """来源名 → 子文件夹名（兴达/文案库/未知来源 → 其他）"""
    s = str(s or "")
    return s if s in ("官网", "欣盛") else "其他"


def _organize_sources(d, info):
    """新抓的图按来源落子文件夹 官网/欣盛/其他（2026-10-24 用户定稿 A 方案，老型号平铺不动）。
    把根目录原图移进子目录；srcmap 键改相对路径（官网/xx.jpg）；info.imgs 里的本地文件名同步改。"""
    srcmap = info.get("srcmap") if isinstance(info.get("srcmap"), dict) else {}
    flat = _img_files(d)
    if not flat:
        return
    newmap = {k: v for k, v in srcmap.items() if "/" in str(k)}   # 已是相对路径的保留
    moved = 0
    for f in flat:
        sub = _src_sub_of(srcmap.get(f))
        sd = os.path.join(d, sub)
        try:
            os.makedirs(sd, exist_ok=True)
            shutil.move(os.path.join(d, f), os.path.join(sd, f))
            moved += 1
            newmap[sub + "/" + f] = str(srcmap.get(f) or sub)
        except Exception:
            newmap[f] = str(srcmap.get(f) or sub)   # 没搬成的留根目录，来源照记
    if not moved:
        return
    info["srcmap"] = newmap
    # 欣盛流程 info.imgs=[{f,src}]：本地文件名同步改成相对路径（官网图抓取流程 imgs 存的是 URL，不受影响）
    if isinstance(info.get("imgs"), list):
        _bnames = {os.path.basename(rel): rel for rel in newmap if "/" in str(rel)}
        for _x in info["imgs"]:
            if isinstance(_x, dict) and _x.get("f") and str(_x["f"]) in _bnames:
                _x["f"] = _bnames[str(_x["f"])]


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
STYLE_DIRS = {"studio": "AI6张", "spot": "聚光", "white": "AI6张_亮白",
              "pure": "AI6张_纯白", "scene": "AI6张_实景"}   # 2026-10-31 用户令：聚光文件夹就叫「聚光」
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


def _auto_qa_spot(model, info, src, out, style, paths, prog=None):
    """聚光四件套生成完自动 AI 复检（2026-10-29 用户令「做好的四张让AI过一遍」）。
    商汤免费额度优先；无 key/视觉不可用静默跳过不挡出货。
    压字/出画/截断等确定性布局问题自动微调重渲（复用 qa_render + auto_optimize）。

    2026-10-30 用户实锤「聚光的图一直出不来」：复检原本跑在生成线程里、还占着进度条，
    四张图早落盘了前端却一直「生成中 92%」等到复检跑完（1~3 分钟）。现在复检走独立
    后台线程、prog 传 None = 不碰 JOB 进度（图先亮出来，修好的再静默替换）。"""
    import pei_core
    _prog = prog or (lambda p, m: None)
    key = _ai_key() or pei_core._sn_key()
    if not key:
        _logf()("AI 复检跳过：未配置视觉 key")
        return
    _prog(92, "AI 复检成片 ...")
    res = pei_core.qa_render(key, paths, info)
    if res is None:
        _logf()("AI 复检不可用（视觉模型无应答），跳过")
        return
    if not res:
        _logf()("AI 复检通过：%d 张成片无问题" % len(paths))
        return
    _logf()("AI 复检发现 %d 处问题：%s" % (
        len(res), "；".join("%s(%s)" % (x["issue"], x["severity"]) for x in res)))
    import pei_ai9
    r = pei_ai9.auto_optimize(key, info, src, out, style, res, rounds=2,
                              progress=_prog, log=lambda s: _logf()(s))
    if r.get("fixed"):
        _logf()("AI 自动修复槽位 %s，重渲完成" % r["fixed"])
    for x in (r.get("remaining") or []):
        _logf()("需人工确认：%s（%s）" % (x.get("issue"), x.get("severity")))


def gen_style(model, style, sell_price=None, version="N"):
    """生成一套 6 张精修（后台线程跑，JOB 汇报进度）"""
    name = STYLE_NAMES.get(style, style)
    _job_begin(model, "发布图 · " + name)
    info = src = out = None          # 复检线程要用，先占位（异常分支里也不能 NameError）
    paths = []
    try:
        info = load_info(model) or work(model, prog=_job_prog)
        import pei_ai9
        if style == "spot":
            # 聚光独立体系（2026-10-29 用户令「聚光路径跟着蓝底来，不跟写文案一起」）：
            # 数据源+成品全在 kitset/<型号>/（抓取图进 官网\ 子目录，成品 AI6张_聚光\）。
            # 缺料时把写文案已抓的一次性硬链搬入（ensure_data），都没有才借上面 work()
            # 抓一次——搬完聚光只依赖 kitset，写文案目录删了也不影响聚光。
            import kitset as _ks
            _ks.ensure_data(model, log=lambda s: _logf()(s))
            _kinfo = _ks.load_kinfo(model) or info
            info = dict(_kinfo)
            info["dir"] = _ks.model_dir(model)
            src = os.path.join(info["dir"], "官网")
            os.makedirs(src, exist_ok=True)
            out = _ks.spot_dir(model)
        else:
            src = info["dir"]
            out = os.path.join(src, STYLE_DIRS.get(style, style))
        paths = pei_ai9.generate(info, src, out, version=version, sell_price=sell_price,
                                 api_key=_ai_key(), progress=_job_prog, style=style,
                                 slots=SLOTS6, overview=False)
        _last_set(model, paths, out, style)
        _logf()("发布图·%s 生成完成：%d 张 -> %s" % (name, len(paths), out))
        # 2026-10-30 用户实锤「图一直出不来」：先收尾让前端立刻亮图，复检挪后台线程。
        # 复检最慢 1~3 分钟，压在收尾前就会看到「100% 卡住 + 左边还没有图」。
        _job_end(True)
    except Exception as e:
        _logf()("发布图·%s 生成失败：%s" % (name, e))
        _job_end(False, str(e))
        return
    if style == "spot" and paths:
        # 聚光四件套出完后台让 AI 过一遍（不占进度条；修过的图静默替换，复检失败绝不影响已生成图）
        def _qa_bg():
            try:
                if job_state().get("running"):
                    _logf()("AI 复检跳过：已有新任务在跑，避免覆盖新成片")
                    return
                _auto_qa_spot(model, info, src, out, style, paths)
                _logf()("AI 复检结束（若修过图，重开这套即可看到）")
            except Exception as e:
                _logf()("AI 复检异常（不影响成片）：%s" % e)
        try:
            threading.Thread(target=_qa_bg, daemon=True).start()
        except Exception as e:
            _logf()("AI 复检线程起不来（不影响成片）：%s" % e)


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
    免得用户在文案窗口点过「写文案」后还要重复抓一遍。只补根目录原图，不动成品目录。
    返回本次实际复制进来的文件名列表（lib 会把它们记进 srcmap 的「文案库」来源）。"""
    d = mdir(model)
    copied = []
    try:
        import copywriter
        cdir = os.path.join(copywriter.copy_root(), copywriter.norm_model(model) or model)
        if not os.path.isdir(cdir):
            return copied
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
                copied.append(f)
                n += 1
            except Exception:
                pass
        if n:
            _logf()("从文案图片库播种 %d 张官网图进素材库" % n)
    except Exception:
        pass
    return copied


def _img_files_rec(d):
    """原图素材相对路径清单：根目录 + 官网/欣盛/其他 子文件夹（2026-10-24 A 方案）。
    成品目录（AI6张*/发布图）不在这扫——lib 另有分组。返回相对路径（根目录文件不带前缀）。"""
    if not os.path.isdir(d):
        return []
    out = list(_img_files(d))
    for sub in _SRC_SUBS:
        sd = os.path.join(d, sub)
        for f in _img_files(sd):
            out.append(sub + "/" + f)
    return out


def lib(model):
    """图片库分组（peiwen LIB_GROUPS 口径）：成品风格组在前、原图垫底。
    2026-10-24 用户定稿：原图素材不再混一堆——按来源拆「原图 · 官网 / 欣盛 / 其他」
    （优先级同序：官网 > 欣盛 > 其他）；文件一律给相对路径（可能带子目录），
    前端拼 /pubimg/<型号>/<rel>"""
    seeded = _seed_from_copy(model)
    d = mdir(model)
    groups = []
    # 2026-10-30：聚光读侧改走 kitset 后，老型号的历史聚光图还躺在旧 peiwork 根——
    # kitset 这边没图就先补一次本地硬链搬家（ensure_data 纯本地、幂等、不联网不重跑）。
    try:
        import kitset as _ks
        if not _img_files(_ks.spot_dir(model)):
            _ks.ensure_data(model, log=lambda s: _logf()(s))
    except Exception as e:
        _logf()("聚光老图搬入失败（不影响其它）：%s" % e)
    for sub in ("AI6张", SPOT_SUB, "AI6张_亮白", "AI6张_纯白", "AI6张_实景", "发布图"):
        label = {"AI6张": "发布图 · 深灰影棚", SPOT_SUB: "发布图 · 暗角聚光",
                 "AI6张_亮白": "发布图 · 亮白影棚", "AI6张_纯白": "发布图 · 纯白",
                 "AI6张_实景": "发布图 · 实景合成", "发布图": "保存精选 / 促销首图"}.get(sub, sub)
        fs = _img_files(dir_of(model, sub))   # 聚光走 kitset（2026-10-30），其余走 peiwork
        if fs:
            groups.append({"label": label, "sub": sub,
                           "files": ["%s/%s" % (sub, f) for f in fs]})
    raw = _img_files_rec(d)
    if raw:
        info = load_info(model) or {}
        srcmap = info.get("srcmap") if isinstance(info.get("srcmap"), dict) else {}
        if seeded and info.get("model"):
            # 文案库播种的图记进 srcmap（来源「文案库」→ 落「其他」组），写回 info.json
            for f in seeded:
                srcmap.setdefault(f, "文案库")
            info["srcmap"] = srcmap
            _save_info(info)
        _st = str(info.get("site") or "")
        _base = {"haier": "官网", "casarte": "官网", "leader": "官网",
                 "xinsheng": "欣盛"}.get(_st, "其他")
        buckets = {"官网": [], "欣盛": [], "其他": []}
        for rel in raw:
            parts = str(rel).split("/", 1)
            if len(parts) == 2 and parts[0] in buckets:
                s = parts[0]                          # 新抓的：子文件夹就是来源
            else:
                s = str(srcmap.get(rel) or srcmap.get(os.path.basename(rel)) or _base)
            buckets[s if s in buckets else "其他"].append(rel)
        for key in ("官网", "欣盛", "其他"):
            if buckets[key]:
                groups.append({"label": "原图 · " + key, "sub": "", "files": buckets[key]})
    return {"ok": True, "dir": d, "has_info": os.path.isfile(os.path.join(d, "info.json")),
            "groups": groups, "job": job_state()}


def sel_save(model, rels, sell_price=None, version="N"):
    """保存精选：按勾选顺序把选中图复制进 发布图/（N_原名.ext）。
    2026-10-24 用户令：0_主图_卖点拼版.jpg 所有的都不用做——只存勾选原图，不再自动拼首图"""
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
    for rel in rels:
        sp = os.path.join(src, rel.replace("/", os.sep))
        if not os.path.abspath(sp).startswith(os.path.abspath(src)) or not os.path.isfile(sp):
            continue
        ext = os.path.splitext(rel)[1]
        dst = os.path.join(sel, "%d_%s%s" % (n + 1, os.path.splitext(os.path.basename(rel))[0], ext))
        shutil.copy(sp, dst)
        n += 1
    try:
        reveal(sel)
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
    if style == "spot":
        # 聚光独立体系：自动优化重渲的图源/成品都在 kitset（2026-10-29 用户令）
        import kitset as _ks
        _ks.ensure_data(model)
        _kinfo = _ks.load_kinfo(model) or info
        info = dict(_kinfo)
        info["dir"] = _ks.model_dir(model)
        src = os.path.join(info["dir"], "官网")
    else:
        src = info["dir"]
    r = pei_ai9.auto_optimize(_ai_key(), info, src, out, style, res,
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


SPOT_SUB = "聚光"   # 2026-10-31 用户令：去「AI6张_」前缀（写入端 = kitset.SPOT_SUB，两边必须一致）


def dir_of(model, sub=""):
    """型号内子目录解析（2026-10-30 修「聚光的图一直出不来」实锤）。

    聚光四件套 2026-10-29 已从 peiwork 迁到 kitset/<型号>/AI6张_聚光（写入端 = kitset.spot_dir），
    但读侧三处（打开文件夹 / 取图 / 列库）还挂在旧 peiwork 根 → 点开文件夹是空的、页面显示没图。
    这里统一收口：聚光一律走 kitset，其余风格照旧走素材根 peiwork。"""
    sub = (sub or "").replace("/", os.sep).strip(os.sep)
    # 2026-10-31：聚光一律走 kitset（新版文件夹叫「聚光」；旧名 AI6张_聚光 兼容收进来）
    if sub and sub.split(os.sep)[0] in (SPOT_SUB, "AI6张_聚光"):
        import kitset
        return os.path.join(kitset.model_dir(model), sub)
    return os.path.join(mdir(model), sub) if sub else mdir(model)


def open_dir(model, sub=""):
    reveal(dir_of(model, sub))
    return {"ok": True}
