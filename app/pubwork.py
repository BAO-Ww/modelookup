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
from renderlock import serialized

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
    """型号做目录名：去掉文件系统不安全字符。
    2026-11-04 P0-2：还要拦「..」「.」点段和开头结尾的点——os.path.join(root, "..")
    会把后续的清目录/rmtree 顶出图片库根（本地接口无鉴权，型号来自请求参数）。"""
    nm = re.sub(r'[\\/:*?"<>|]+', "", (model or "").strip())
    if not nm or re.fullmatch(r"\.*", nm) or ".." in nm:
        return "未命名"
    return nm


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


def _kitset_info_to_pub(cand, model, d):
    """把蓝底（kitset）抓好的 info.json 搬成本模块素材 info（2026-11-04）。
    搬得动返回 info（已落盘），不合适返回 None——绝不搬出空参数/缺图的数据。"""
    try:
        with open(cand, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    props = [(str(a), str(b)) for a, b in (raw.get("props") or []) if a and b]
    if not props:
        return None
    imgs = []
    for x in (raw.get("imgs") or []):
        f_ = x.get("f") if isinstance(x, dict) else x
        if f_ and os.path.isfile(os.path.join(d, str(f_))):
            _src = (x.get("src") if isinstance(x, dict) else "") or "官网"
            imgs.append({"f": str(f_), "src": _src})
    if not imgs:
        return None
    _cat = str(raw.get("cat") or "")
    if not _cat:
        try:
            import pei_core
            _cat = pei_core.guess_cat(str(raw.get("title") or model))
        except Exception:
            _cat = ""
    info = {"site": raw.get("site") or "kitset", "brand": raw.get("brand") or "",
            "model": raw.get("model") or model, "title": raw.get("title") or model,
            "price": str(raw.get("price") or "0"), "avail": raw.get("avail") or "",
            "props": props, "url": raw.get("url") or "", "cat": _cat,
            "dir": d, "imgs": imgs, "source": "蓝底抓取（复用）"}
    info = _normalize_info(info)
    os.makedirs(d, exist_ok=True)
    _save_info(info)
    return info


def _kitset_imgs_to_pub(model, kd, d, log=None):
    """kitset/<型号>/ 有图但没 info.json → 合成一份素材 info（2026-11-10）。
    R2 云端图库拉的型号只落图片不落参数（JSQ22-12LVS(12T) 实锤：蓝底四张完成、
    聚光却报「素材抓取没成功」——旧复用逻辑只认 info.json，把这批漏了）。
    图硬链进素材目录；标题/品牌取 set.json；参数尽量从云端参数库 par_cloud.json 补，
    补不到就空着（渲染端胶囊行自动退化）——绝不为此再爬一遍官网（用户令：蓝底用
    什么图聚光直接用）。"""
    try:
        import kitset as _ks
    except Exception:
        return None
    imgs, srcmap = [], {}
    for sub in ("官网", "欣盛", "其他"):
        sd = os.path.join(kd, sub)
        if not os.path.isdir(sd):
            continue
        try:
            fns = sorted(fn for fn in os.listdir(sd)
                         if fn.lower().endswith((".jpg", ".jpeg", ".png"))
                         and os.path.isfile(os.path.join(sd, fn)))
        except Exception:
            fns = []
        for fn in fns:
            rel = sub + "/" + fn
            imgs.append({"f": rel, "src": sub})
            srcmap[rel] = sub
    if not imgs:
        return None
    os.makedirs(d, exist_ok=True)
    n = 0
    for x in imgs:
        if _ks._link_or_copy(os.path.join(kd, x["f"]), os.path.join(d, x["f"])):
            n += 1
    if not n:
        return None
    # 标题/品牌：set.json 的 name（文案套）最靠谱，退化用型号名
    title, model_ = "", str(model)
    try:
        with io.open(os.path.join(kd, "set.json"), encoding="utf-8") as f:
            _set = json.load(f)
        title = str(_set.get("name") or "")
        model_ = str(_set.get("model") or model)
    except Exception:
        pass
    brand = next((b for b in ("卡萨帝", "统帅", "海尔") if b in title), "")
    # 参数：云端参数库本地缓存有就带上（没有就算了，不爬官网）
    props = []
    try:
        import paths as _paths
        _pj = os.path.join(_paths.data_base(), "par_cloud.json")
        if os.path.isfile(_pj):
            with io.open(_pj, encoding="utf-8") as f:
                _pars = (json.load(f).get("pars") or {})
            _hit = _pars.get(model) or _pars.get(model_) or {}
            props = [(str(k), str(v)) for k, v in _hit.items() if k and v][:40]
    except Exception:
        props = []
    _cat = ""
    try:
        import pei_core
        _cat = pei_core.guess_cat(title or model_)
    except Exception:
        _cat = ""
    info = {"site": "kitset", "brand": brand, "model": model_,
            "title": title or model_, "price": "0", "avail": "",
            "props": props, "url": "", "cat": _cat,
            "dir": d, "imgs": imgs, "srcmap": srcmap,
            "source": "蓝底图库（复用·云端图源）"}
    info = _normalize_info(info)
    _save_info(info)
    if log:
        log("素材复用蓝底图库：%d 张图（本地无 info.json，参数 %d 条%s）"
            % (n, len(props), "走云端参数库" if props else "空着·胶囊行退化"))
    return info


def _wait_or_reuse_kitset(model, d, log, prog):
    """素材没有时先看蓝底那边抓好的（2026-11-04 用户令「跑一个就够了吧还要两个一起跑爬虫」）：
    宝窗开窗蓝底/聚光/文案三路同时进，同一台机器不该爬两遍——聚光这边先复用
    kitset/<型号>/info.json；蓝底 job 还在跑就等它抓完（最多 90 秒）再复用；
    蓝底没在跑也没现成素材 → 返回 None 照旧自己爬（单独生成聚光的场景）。
    2026-11-10 补：kitset 有图没 info.json（R2 云端图库拉的型号）也直接复用，
    不再自己爬——蓝底 job 在跑就等它落完图再合成。"""
    try:
        import kitset as _ks
    except Exception:
        return None
    try:
        nm = _ks.norm_model(model) or re.sub(r"\s+", "", model)
        kd = os.path.join(_ks.kit_root(), nm)
    except Exception:
        return None
    cands = (os.path.join(kd, "官网", "info.json"), os.path.join(kd, "info.json"))
    deadline = time.time() + 90
    told = False
    while True:
        for cand in cands:
            if os.path.isfile(cand):
                info = _kitset_info_to_pub(cand, model, d)
                if info:
                    if log:
                        log("素材直接复用蓝底抓取结果（不重复爬）")
                    return info
        running = False
        try:
            import jobman
            running = bool(jobman.state("kitset:" + nm).get("running")
                           or jobman.state("kitset:" + model).get("running"))
        except Exception:
            running = False
        if not running or time.time() >= deadline:
            # 蓝底没在跑（或等超时）：有图没 info.json（云端图源型号）也合成复用，不重爬
            info = _kitset_imgs_to_pub(model, kd, d, log)
            if info:
                return info
            return None
        if not told:
            told = True
            if prog:
                prog(8, "蓝底正在抓素材，等它抓完直接用 ...")
            if log:
                log("蓝底抓取进行中，聚光等它抓完复用素材（不重复爬）")
        time.sleep(2)


_R2_OK_SUBS = ("官网", "欣盛")


def _r2_pub_rel(k):
    """云端键是不是「抓取图」（只有 官网/ 欣盛/ 根级 src_* 上云，蓝底/聚光产物永不入云）。
    2026-11-10：跟 api_get 的 /api/kitimg 云端兜底同一口径，边界不能松。"""
    k = str(k or "").replace("\\", "/").strip()
    if not k or k.startswith("/"):
        return False
    parts = [p for p in k.split("/") if p]
    if not parts or any(p in (".", "..") for p in parts):
        return False
    if len(parts) >= 2 and parts[0] in _R2_OK_SUBS:
        return True
    return parts[-1].lower().startswith("src_")


def _r2_grab_to_pub(model, d, log=None, prog=None):
    """本地没图时**先查云端 R2 官网图库**，有就直接拉下来出图（2026-11-10 用户令
    「顺序错了，是先去 R2 云端找没有再去爬」）。命中就落 kitset/<型号>/<相对路径>
    再走 `_kitset_imgs_to_pub` 合成素材 info，跟蓝底复用同一条路。
    云端没这个型号 / 查询失败 → 返回 None，调用方照旧去爬。
    为什么必须这样：leader.com.cn 整站 403（本机家宽、4 个境外节点、真浏览器、
    CF 边缘 522 全试过），官网抓不动时云端图就是唯一活路。"""
    try:
        import kitset as _ks
        import cloud as _cl
    except Exception:
        return None
    try:
        nm = _ks.norm_model(model) or re.sub(r"\s+", "", model)
        kd = os.path.join(_ks.kit_root(), nm)
    except Exception:
        return None
    keys, err = _cl.kit_list_cloud(nm)
    if not keys:
        if log and err:
            log("云端图库查询失败（%s），照常爬取" % err)
        return None
    rels = [k for k in keys if _r2_pub_rel(k)][:40]
    if not rels:
        return None
    if prog:
        prog(6, "云端图库有 %d 张图，正在拉取 ..." % len(rels))
    if log:
        log("云端图库命中 %d 张，拉下来直接用（不爬官网）" % len(rels))
    got = 0
    for rel in rels:
        fp = os.path.join(kd, rel.replace("/", os.sep))
        try:
            if os.path.isfile(fp) and os.path.getsize(fp) > 1024:
                got += 1
                continue
        except Exception:
            pass
        raw, _e = _cl.kit_fetch_cloud(nm + "/" + rel)
        if not raw or len(raw) < 512:
            continue
        try:
            os.makedirs(os.path.dirname(fp), exist_ok=True)
            with open(fp, "wb") as f:
                f.write(raw)
            got += 1
        except Exception as e:
            if log:
                log("云端图落盘失败 %s：%s" % (rel, e))
    if not got:
        return None
    return _kitset_imgs_to_pub(model, kd, d, log)


def _xinsheng_fallback(model, d, log, prog=None):
    """欣盛商城相册兜底（2026-10-23 用户定稿「型号对不上的就不要了」）：
    官网三站+兴达都没有、**或官网页面抓不开**（2026-11-10 leader 403 实锤）时用。
    拿到相册返回 info；没相册返回 None（错误文案由调用方按场景给）。"""
    prog = prog or (lambda p, m: None)
    prog(18, "试欣盛商城 ...")
    try:
        import copywriter
        xs = copywriter.fetch_xinsheng(model, log=log)
    except Exception as e:
        xs = None
        log("欣盛兜底异常：%s" % e)
    album = [u for u in ((xs or {}).get("imgs") or []) if u]
    if not album:
        return None
    _pn = str(xs.get("pname") or "")
    _brand = next((b for b in ("卡萨帝", "统帅", "海尔") if b in _pn), "")
    try:
        _price = str(int(float(str(xs.get("price") or "0"))))   # 欣盛价 "1899.0" 浮点串
    except Exception:
        _price = "0"
    import pei_core as _pc
    _cat = _pc.guess_cat(_pn)   # 品类从商品名猜（空 cat 会让选图/背景风格走不到品类分支）
    # 整目录重来：能走到这说明没有有效 info.json，目录里只可能是上次失败/错型号的残留
    # 2026-11-04 P0-2：删前校验归属——realpath 必须真落在图片库根下才许 rmtree
    import paths as _paths
    if os.path.realpath(d).startswith(os.path.realpath(_paths.data_base()) + os.sep):
        shutil.rmtree(d, ignore_errors=True)
    else:
        raise RuntimeError("目录越界，拒绝删除：" + d)
    os.makedirs(d, exist_ok=True)
    try:
        imgs = copywriter.dl_images([(u, "欣盛") for u in album], d, log)
    except Exception as e:
        log("欣盛图下载失败：%s" % e)
        raise RuntimeError("欣盛商城图片下载失败，稍后再试")
    return {"site": "xinsheng", "brand": _brand, "model": model,
            "title": _pn or model, "price": _price,
            "avail": "", "props": [], "url": str(xs.get("url") or ""),
            "cat": _cat, "dir": d, "imgs": imgs, "source": "欣盛商城"}


def _parse_prop_lines(txt, props, seen):
    """视觉回答 → [(参数名, 值)]。「参数名:值」与商汤爱输出的 markdown 表格
    「| 参数名 | 值 |」都收；营销长句、超长值、重复项丢弃。原地追加。"""
    for ln in (txt or "").splitlines():
        ln = ln.strip()
        if ln.startswith("|") and ln.count("|") >= 2:
            cells = [c.strip() for c in ln.strip("|").split("|")]
            if len(cells) >= 2 and cells[0] and not set(cells[0]) <= set("-: "):
                k, v = cells[0], cells[1]
            else:
                continue    # 表头分隔行 |---|---|
        else:
            ln = ln.strip("-•·").replace("：", ":", 1)
            if ":" not in ln:
                continue
            k, v = ln.split(":", 1)
        k = re.sub(r"^\d+[\.、\)]\s*", "", k).strip()
        v = v.strip().strip("。；;|")
        # 型号 OCR 易抄错（实测 1BE5R↔18ER5），软件本来就知道型号，读了反而添乱
        if k in ("产品型号", "型号", "规格型号") or "型号" in k:
            continue
        if not k or not v or len(k) > 14 or len(v) > 40:
            continue
        if (k, v) not in seen:
            seen.add((k, v))
            props.append((k, v))


def _ai_props_from_images(model, log):
    """官网抓不到参数时，把 kitset 详情图拼一版喂视觉 AI **直接**读参数
    （2026-11-13 用户令「抓取不到数据就拼成一版让AI去读取」+「不能直接在拼好的
    里面直接提取嘛」；JSLQ32-18ER5PRODRTGU1 实锤：官网无参数表，识货详情图
    02.jpg 自带完整规格表，拼图直接读出 8 条）。
    两级：拼图一次调用直接提取 → 读空才逐张原图兜底（前 3 张）。
    返回 [(k, v)]（可能空）；视觉不可用/解析不出返回 []，不拦出图。"""
    try:
        import kitset as _ks
        import pei_core
        from PIL import Image, ImageDraw, ImageFont
        nm = _ks.norm_model(model)
        cands = []
        for sub in ("官网", "欣盛"):
            dd = os.path.join(_ks.kit_root(), nm, sub)
            if os.path.isdir(dd):
                for f in sorted(os.listdir(dd)):
                    if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp")) and not f.startswith("_"):
                        cands.append(os.path.join(dd, f))
            if cands:
                break
        if not cands:
            return []
        cands = cands[:8]
        if log:
            log("AI 读图参数：%d 张详情图参与" % len(cands))
        props, seen = [], set()

        # ---- 第 1 级：拼一版，1 次调用直接提取
        if len(cands) >= 3:
            try:
                tiles = []
                for p in cands:
                    t = _ks._flat_white(Image.open(p)).convert("RGB")
                    t.thumbnail((640, 640), Image.LANCZOS)
                    tiles.append(t)
                COLS = 4
                ROWS = max(1, (len(tiles) + COLS - 1) // COLS)
                cw = max(t.width for t in tiles) + 8
                ch = max(t.height for t in tiles) + 30
                board = Image.new("RGB", (cw * COLS, ch * ROWS), "white")
                dr = ImageDraw.Draw(board)
                try:
                    fnt = ImageFont.truetype(r"C:\Windows\Fonts\arialbd.ttf", 26)
                except Exception:
                    fnt = ImageFont.load_default()
                for i, t in enumerate(tiles):
                    r, c = divmod(i, COLS)
                    x, y = c * cw, r * ch
                    board.paste(t, (x + (cw - t.width) // 2, y + 28 + (ch - 28 - t.height) // 2))
                    dr.rectangle([x, y, x + 46, y + 27], fill=(200, 0, 0))
                    dr.text((x + 12, y + 1), str(i + 1), fill="white", font=fnt)
                buf = io.BytesIO()
                board.save(buf, "JPEG", quality=88)
                du = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
                txt = pei_core._vision_call(_ks._vision_key(), du,
                    "这张大图里有 %d 个红色数字编号的格子，每格是一张家电产品详情图。"
                    "凡印有「产品参数/规格」的格子（参数表格、规格表、成段的参数名+数值文字），"
                    "把其中的参数逐条列出，每行格式严格为「参数名:值」（如「额定热负荷:32.0kW」）。"
                    "多个格子的参数合并连续输出；只输出图中明确印出的参数，不要编造；"
                    "纯产品照片、卖点海报、场景图里的宣传语和大数字（省多少钱、节能百分比）不要输出。"
                    "整张图里没有任何参数就只输出两个字：无"
                    % len(tiles), log=log)
                if txt and txt.strip()[:1] != "无":
                    _parse_prop_lines(txt, props, seen)
                if log:
                    log("AI 读图参数：拼图直接读出 %d 条" % len(props))
            except Exception as e:
                if log:
                    log("AI 读图参数：拼图提取失败（%s），退回逐张读" % e)

        # ---- 第 2 级：拼图没读出 → 逐张原图高清兜底（前 3 张）
        if not props:
            for i in range(min(3, len(cands))):
                try:
                    im = Image.open(cands[i]).convert("RGB")
                    im.thumbnail((1100, 1100), Image.LANCZOS)
                    buf = io.BytesIO()
                    im.save(buf, "JPEG", quality=90)
                    du = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
                    txt = pei_core._vision_call(_ks._vision_key(), du,
                        "这是家电产品详情图。把图中印出的「产品参数/规格」逐条列出，"
                        "每行格式严格为「参数名:值」，例如「额定热负荷:32.0kW」。"
                        "只输出图中明确印出的参数表格/规格文字，不要编造；"
                        "宣传卖点、省多少钱、节能百分比这类营销数字不要输出。"
                        "图中没有参数信息就只输出两个字：无", log=log)
                    if not txt or txt.strip()[:1] == "无":
                        continue
                    _parse_prop_lines(txt, props, seen)
                except Exception as e:
                    if log:
                        log("AI 读图参数：第 %d 张读取失败（%s）" % (i + 1, e))
            if log and props:
                log("AI 读图参数：逐张兜底读出 %d 条" % len(props))
        return props[:14]
    except Exception as e:
        if log:
            log("AI 读图参数不可用（%s）" % e)
        return []


def _backfill_empty_props(model, info, log):
    """空壳 info（props=0）时官网参数表补抓一次（2026-11-12 ES60H-Q3白 实锤）：
    图库复用链给的 info 无 url 无 props，原有补抓条件「props 空 且 有 url」永远
    不成立，聚光参数页只剩兜底一条。fetch_official 的 groups→props 组装同
    kitset._gather_info；抓到就 _save_info 落盘（下次免抓）。
    官网也没有参数 → 拼详情图喂视觉 AI 读参数（2026-11-13 用户令，JSLQ32 实锤）；
    两级都空仍用原数据出图，不拦。"""
    if not isinstance(info, dict) or (info.get("props") or []):
        return info
    _props = []
    try:
        import kitset as _ks0
        _data = _ks0.fetch_official(model, log=log)
        for _src in ((_data or {}).get("groups") or []):
            for _k, _v in ((_src[1] or []) if len(_src) > 1 else []):
                if str(_k).strip() and str(_v).strip() and (str(_k), str(_v)) not in _props:
                    _props.append((str(_k), str(_v)))
        if _props:
            if (_data or {}).get("url") and not info.get("url"):
                info["url"] = _data["url"]
            if (_data or {}).get("title") and not str(info.get("title") or "").strip():
                info["title"] = _data["title"]
            if (_data or {}).get("brand") and not str(info.get("brand") or "").strip():
                info["brand"] = _data["brand"]
            log("空参数补抓：官网 %d 条参数" % len(_props))
        else:
            log("官网参数表没有这个型号的参数，改读详情图")
    except Exception as e:
        log("官网参数补抓失败（%s），改读详情图" % e)
    if not _props:
        try:
            _props = _ai_props_from_images(model, log)
        except Exception as e:
            log("AI 读图参数失败（%s）" % e)
    if _props:
        info["props"] = _props
        try:
            _save_info(info)
            log("空参数兜底：%d 条参数已落 info.json" % len(_props))
        except Exception:
            pass
    else:
        log("详情图也没读出参数，继续用现有数据出图")
    return info


def work(model, prog=None):
    """抓素材：本地有就直接用；没有先看蓝底抓好的能不能复用（等最多 90s）；
    复用不上走 peiwen 抓取链（新爬虫→旧检索→兴达），存 素材/<型号>/"""
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
        if not (info.get("props") or []):
            info = _backfill_empty_props(model, info, log)
        return info
    info = _wait_or_reuse_kitset(model, d, log, prog)
    if info:
        if not (info.get("props") or []):
            info = _backfill_empty_props(model, info, log)
        return info
    # 2026-11-10 用户令「顺序错了，是先去 R2 云端找没有再去爬」——本地没图先问云端图库
    info = _r2_grab_to_pub(model, d, log, prog)
    if info:
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
        log("官网未命中，试欣盛商城 ...")
    else:
        prog(30, "抓取详情页与图片 ...")
        if site == "xdjdy":
            info = pei_xdjdy.fetch_xdjdy(url, d, log=log, progress=prog)
        else:
            info = pei_core.fetch_product(site, url, d, log=log, progress=prog)
            if info:
                info = pei_core.spider_enrich_info(info, site, url, prod, log=log)
        if not info:
            # 2026-11-10 实锤（JSQ22-12LVS(12T)）：旧检索给了 URL 但页面 403/已下架，
            # 旧逻辑一有 URL 就跳过欣盛兜底，直接报「页面抓取失败」白死一回。
            # 抓空也必须继续兜底。
            log("官网页面抓不到（%s），转欣盛兜底 ..." % url)
    if not info:
        info = _xinsheng_fallback(model, d, log, prog)
    if not info:
        if url:
            raise RuntimeError("官网页面打不开（整站拒访或已下架），欣盛与云端图库都没有这个型号，稍后再试")
        raise RuntimeError("没找到这个型号（官网/欣盛/云端图库都没有），检查一下型号是否正确（海尔/卡萨帝/统帅/实体店）")
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


@serialized
def _auto_qa_spot(model, info, src, out, style, paths, prog=None):
    """聚光四件套生成完自动 AI 复检（2026-10-29 用户令「做好的四张让AI过一遍」）。
    商汤免费额度优先；无 key/视觉不可用静默跳过不挡出货。
    压字/出画/截断等确定性布局问题自动微调重渲（复用 qa_render + auto_optimize）。

    2026-10-30 用户实锤「聚光的图一直出不来」：复检原本跑在生成线程里、还占着进度条，
    四张图早落盘了前端却一直「生成中 92%」等到复检跑完（1~3 分钟）。现在复检走独立
    后台线程、prog 传 None = 不碰 JOB 进度（图先亮出来，修好的再静默替换）。"""
    import pei_core
    _prog = prog or (lambda p, m: None)
    key = _ai_key() or pei_core._sf_key()
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
    # 2026-11-10：AI 老报「图片背景颜色与其他图片不一致」。聚光四张底色是深色系
    # 渐变（实测 (32,32,48) vs (0,0,0) 同族，人眼看是统一深色底），先像素实测——
    # 同族就是误报，别让它挤掉真正要修的布局问题、也别占「需人工」的坑。
    try:
        _bad_bg, _ref_bg = pei_core.bg_mismatch_slots(paths)
    except Exception:
        _bad_bg, _ref_bg = [], None
    _res2 = []
    for x in res:
        if x.get("code") == "style_mismatch" and x.get("slot") not in _bad_bg:
            _c = tuple(_ref_bg or (0, 0, 0))
            _logf()("  %s —— 像素实测底色同族（#%02X%02X%02X），判 AI 误报，忽略"
                    % ((x.get("issue"),) + _c))
            continue
        _res2.append(x)
    if not _res2:
        _logf()("AI 复检其余均为误报，无需修复")
        return
    import pei_ai9
    r = pei_ai9.auto_optimize(key, info, src, out, style, _res2, rounds=2,
                              progress=_prog, log=lambda s: _logf()(s),
                              skip_scale_slots=(4,))
    if r.get("fixed"):
        _logf()("AI 自动修复槽位 %s，重渲完成" % r["fixed"])
    for x in (r.get("remaining") or []):
        _logf()("需人工确认：%s（%s）" % (x.get("issue"), x.get("severity")))


@serialized
def gen_style(model, style, sell_price=None, version="N", entry_title=None):
    """生成一套 6 张精修（后台线程跑，JOB 汇报进度）。
    entry_title（2026-11-04 用户令）：软件条目标题，聚光首图大字用它——
    官网抓取链会把 info.title 覆盖成页面标题（HP-35 实锤变成「大通量前置过滤器」），
    前端把卡片上那句（如「海尔净水·厨下机HP-35」）传进来优先。"""
    name = STYLE_NAMES.get(style, style)
    _job_begin(model, "发布图 · " + name)
    info = src = out = None          # 复检线程要用，先占位（异常分支里也不能 NameError）
    paths = []
    try:
        info = load_info(model)
        if not info or not (info.get("props") or []):
            # 2026-11-12 ES60H-Q3白 实锤：本地 info.json 是空壳（props=0 条、标题就是
            # 型号），原先 load_info 有值就跳过抓参数——图库复用得再全参数也一直空着，
            # 聚光参数页只剩兜底一条。空壳也要抓一次；抓不到才用空壳继续（不拦出图）。
            try:
                _w = work(model, prog=_job_prog)
                if _w and (_w.get("props") or []):
                    info = _w
                elif not info:
                    info = _w or {"model": model, "props": []}
            except Exception as e:
                _logf()("参数抓取失败（用现有 info 继续）：%r" % (e,))
                if not info:
                    info = {"model": model, "props": []}
        import pei_ai9
        if style == "spot":
            # 聚光独立体系（2026-10-29 用户令「聚光路径跟着蓝底来，不跟写文案一起」）：
            # 数据源+成品全在 kitset/<型号>/（抓取图进 官网\ 子目录，成品 AI6张_聚光\）。
            # 缺料时把写文案已抓的一次性硬链搬入（ensure_data），都没有才借上面 work()
            # 抓一次——搬完聚光只依赖 kitset，写文案目录删了也不影响聚光。
            import kitset as _ks
            _ks.ensure_data(model, log=lambda s: _logf()(s))
            _kinfo = _ks.load_kinfo(model)
            if not (_kinfo or {}).get("props"):
                _kinfo = None    # 空壳 kinfo（props=0）不许覆盖刚抓到的真参数
            info = dict(_kinfo or info)
            if not _kinfo and (info.get("props") or []):
                # 抓到真参数且 kitset 还是空壳 → 落权威副本（下次免抓）
                try:
                    _kd = _ks.model_dir(model)
                    os.makedirs(_kd, exist_ok=True)
                    _ip = os.path.join(_kd, "info.json")
                    _old = {}
                    if os.path.isfile(_ip):
                        try:
                            with open(_ip, encoding="utf-8") as _f:
                                _old = json.load(_f)
                        except Exception:
                            _old = {}
                    _old.update({k: v for k, v in info.items() if k != "dir"})
                    with open(_ip, "w", encoding="utf-8") as _f:
                        json.dump(_old, _f, ensure_ascii=False, indent=1)
                    _logf()("参数落 kitset 权威副本（%d 条）" % len(info.get("props") or []))
                except Exception:
                    pass
            if entry_title:
                # 2026-11-06 防呆：条目标题里混进表格公式报错值（#N/A 实锤印上聚光首图）
                # → 剔掉报错段再给；剔完为空就不覆盖（退回官网抓取链的标题兜底）
                import re as _re
                _et = _re.sub(r"#\s*(N/A|NA|REF!|VALUE!|NAME\?|DIV/0!|NULL!|NUM!|SPILL!|CALC!)",
                              " ", str(entry_title), flags=_re.I)
                _et = _re.sub(r"\s{2,}", " ", _et).strip()
                if _et:
                    info["title"] = _et
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
    # 2026-11-11 用户令「AI 检查有什么用呢还不如不要」：聚光四件套的 AI 复检整个取消。
    # _auto_qa_spot 函数保留备用，不再有任何调用方。


@serialized
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
    for sub in ("AI6张", SPOT_SUB, "AI6张_亮白", "AI6张_纯白", "AI6张_实景", "白字/蓝底", "白字/紫底", "发布图"):
        label = {"AI6张": "发布图 · 深灰影棚", SPOT_SUB: "发布图 · 暗角聚光",
                 "AI6张_亮白": "发布图 · 亮白影棚", "AI6张_纯白": "发布图 · 纯白",
                 "AI6张_实景": "发布图 · 实景合成",
                 "白字/蓝底": "婷（蓝底）", "白字/紫底": "婷（紫底）",
                 "发布图": "保存精选 / 促销首图"}.get(sub, sub)
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
