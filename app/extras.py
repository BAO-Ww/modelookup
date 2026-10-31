# -*- coding: utf-8 -*-
"""extras.py — 2026-10-31 用户令「剩下的全改」一揽子增强的数据层。

一份本机 JSON（work_dir/extras.json，跟机器走、不上云），装这些：
  alias  型号别名（搜型号时一起认）      tags   自定义标签
  hist   改价历史（可一键退回）          pq     价格快照（任何变动都提醒）
  sq     库存快照（突变提醒）            copies 文案多版本
  links  闲鱼商品链接回填                water  水印设置
  chg    变更日志（今天干了啥）           quota  云端写入配额计数

另有若干只读业务：备份包 / 诊断包 / 图片墙 / 参数对照 / 价格嫌疑 / 发布前三查 / 任务队列。
原则：所有写操作都过 _LOCK + 原子替换；任何一项挂了都不许影响主流程（调用方 try 包住）。
"""
import io
import json
import os
import shutil
import threading
import time
import zipfile

import paths

_LOCK = threading.RLock()
_CACHE = [None]
_FILE = "extras.json"
MAX_CHG = 1500


# ---------------------------------------------------------------- 基础读写
def _fp():
    return paths.wp(_FILE)


def _blank():
    return {"alias": {}, "tags": {}, "hist": {}, "pq": {}, "sq": {},
            "copies": {}, "links": {}, "water": {"on": False, "text": "", "pos": "br",
                                                 "size": 0.032, "color": "#ffffff", "alpha": 0.55},
            "chg": [], "quota": {"d": "", "n": 0}}


def _load():
    if _CACHE[0] is not None:
        return _CACHE[0]
    d = _blank()
    try:
        with io.open(_fp(), encoding="utf-8") as f:
            v = json.load(f)
        if isinstance(v, dict):
            for k, dv in _blank().items():
                if k in v and isinstance(v[k], type(dv)):
                    d[k] = v[k]
    except Exception:
        pass
    _CACHE[0] = d
    return d


def _save():
    d = _load()
    try:
        tmp = _fp() + ".tmp"
        with io.open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, _fp())
    except Exception:
        pass


def _now():
    return int(time.time())


def log(kind, model, text):
    """记一条变更（自动带上时间）。kind: price/img/copy/pub/tag/alias/link/misc"""
    with _LOCK:
        d = _load()
        d["chg"].insert(0, {"t": _now(), "k": str(kind or "misc"), "m": str(model or ""),
                            "x": str(text or "")[:200]})
        if len(d["chg"]) > MAX_CHG:
            del d["chg"][MAX_CHG:]
        _save()


# ---------------------------------------------------------------- 读取（前端一次拿全）
def get_all():
    d = _load()
    return {"ok": True,
            "alias": d["alias"], "tags": d["tags"], "hist": d["hist"],
            "links": d["links"], "water": d["water"],
            "copies": {k: len(v or []) for k, v in (d["copies"] or {}).items()},
            "chg": (d["chg"] or [])[:300], "quota": d["quota"],
            "pq_n": len(d["pq"] or {}), "sq_n": len(d["sq"] or {})}


# ---------------------------------------------------------------- 别名 / 标签
def set_alias(model, names):
    model = str(model or "").strip()
    if not model:
        return {"ok": False, "msg": "缺型号"}
    if isinstance(names, str):
        names = [x.strip() for x in names.replace("，", ",").split(",")]
    names = [str(x).strip() for x in (names or []) if str(x).strip()][:8]
    with _LOCK:
        d = _load()
        if names:
            d["alias"][model] = names
        else:
            d["alias"].pop(model, None)
        _save()
    log("alias", model, "别名改了：" + ("、".join(names) if names else "（清空）"))
    return {"ok": True, "alias": d["alias"].get(model, [])}


def add_tag(model, tag):
    model, tag = str(model or "").strip(), str(tag or "").strip()[:12]
    if not (model and tag):
        return {"ok": False, "msg": "缺型号或标签"}
    with _LOCK:
        d = _load()
        lst = d["tags"].setdefault(model, [])
        if tag not in lst:
            lst.append(tag)
            lst[:] = lst[:10]
            log("tag", model, "加标签：" + tag)
            _save()
        return {"ok": True, "tags": lst}


def del_tag(model, tag):
    model, tag = str(model or "").strip(), str(tag or "").strip()
    with _LOCK:
        d = _load()
        lst = d["tags"].get(model) or []
        if tag in lst:
            lst.remove(tag)
            if lst:
                d["tags"][model] = lst
            else:
                d["tags"].pop(model, None)
            log("tag", model, "删标签：" + tag)
            _save()
        return {"ok": True, "tags": d["tags"].get(model) or []}


# ---------------------------------------------------------------- 改价历史 / 退回
def add_hist(model, xy, old, new, by=""):
    model = str(model or "").strip()
    try:
        old = float(old) if old not in (None, "") else None
    except Exception:
        old = None
    try:
        new = float(new) if new not in (None, "") else None
    except Exception:
        new = None
    with _LOCK:
        d = _load()
        lst = d["hist"].setdefault(model, [])
        lst.insert(0, {"t": _now(), "xy": str(xy or ""), "o": old, "n": new, "by": str(by or "")})
        del lst[40:]
        _save()
    log("price", model, "%s 改价 %s → %s" % (xy or "", old if old is not None else "—",
                                              new if new is not None else "—"))
    return {"ok": True, "hist": lst}


def undo_hist(model, xy=""):
    """一键退回：把该型号（该号）最后一次改价的上一个价还给你，并把这条历史标记作废。"""
    model = str(model or "").strip()
    with _LOCK:
        d = _load()
        lst = d["hist"].get(model) or []
        for it in lst:
            if it.get("undone"):
                continue
            if xy and it.get("xy") and it.get("xy") != xy:
                continue
            if it.get("o") is None:
                return {"ok": False, "msg": "这条没有上一个价，退不回去"}
            it["undone"] = 1
            _save()
            log("price", model, "退回改价：%s → %s" % (it.get("n"), it.get("o")))
            return {"ok": True, "price": it["o"], "xy": it.get("xy") or "", "hist": lst}
    return {"ok": False, "msg": "没有可退回的改价记录"}


def hist_of(model):
    return {"ok": True, "hist": (_load()["hist"].get(str(model or "").strip()) or [])}


# ---------------------------------------------------------------- 快照：价格 / 库存变动提醒
def snap(kind, rows, threshold=0):
    """rows = {型号: 数字}。返回本轮的变动清单（首次只建基线，不提醒）。
    kind=p 价格（任何变动都提醒，用户 2026-10-31 定稿不设阈值）；kind=s 库存（突变提醒）。"""
    rows = rows if isinstance(rows, dict) else {}
    key = "pq" if kind == "p" else "sq"
    out = []
    now = _now()
    with _LOCK:
        d = _load()
        snapd = d[key]
        first = not snapd
        for m, v in rows.items():
            try:
                v = float(v)
            except Exception:
                continue
            old = snapd.get(m)
            if old is not None:
                try:
                    ov = float(old.get("v"))
                except Exception:
                    ov = None
                if ov is not None and abs(ov - v) > 0.001:
                    if kind == "p":
                        out.append({"m": m, "o": ov, "n": v, "pct": (v - ov) / ov * 100 if ov else 0})
                    else:
                        # 库存：减少过半 或 增减 ≥ 10 台才算「突变」，0 台单独提（卖光了）
                        big = (abs(v - ov) >= 10) or (ov > 0 and abs(v - ov) / ov >= 0.5)
                        if v == 0 and ov > 0:
                            big = True
                        if big:
                            out.append({"m": m, "o": ov, "n": v})
            snapd[m] = {"v": v, "t": now}
        d[key] = snapd
        _save()
    if first:
        return {"ok": True, "first": True, "changed": []}
    if out:
        srt = sorted(out, key=lambda x: -abs((x.get("pct") or 0) if kind == "p" else (x["n"] - x["o"])))
        for x in srt[:12]:
            if kind == "p":
                log("price", x["m"], "价格变动 %s → %s（%+.1f%%）" % (x["o"], x["n"], x.get("pct") or 0))
            else:
                log("stock", x["m"], "库存突变 %s → %s" % (x["o"], x["n"]))
    return {"ok": True, "changed": out[:60]}


# ---------------------------------------------------------------- 文案多版本
def save_copy(model, title, body, kind="z", tag=""):
    model = str(model or "").strip()
    if not model:
        return {"ok": False, "msg": "缺型号"}
    with _LOCK:
        d = _load()
        lst = d["copies"].setdefault(model, [])
        lst.insert(0, {"t": _now(), "ti": str(title or "")[:200], "bd": str(body or "")[:4000],
                       "k": str(kind or "z"), "tag": str(tag or "")[:20]})
        del lst[8:]
        _save()
    log("copy", model, "存了一版文案" + ("（" + tag + "）" if tag else ""))
    return {"ok": True, "n": len(lst)}


def list_copy(model):
    return {"ok": True, "list": (_load()["copies"].get(str(model or "").strip()) or [])}


def del_copy(model, ts):
    model = str(model or "").strip()
    with _LOCK:
        d = _load()
        lst = [x for x in (d["copies"].get(model) or []) if str(x.get("t")) != str(ts)]
        if lst:
            d["copies"][model] = lst
        else:
            d["copies"].pop(model, None)
        _save()
    return {"ok": True, "list": lst}


# ---------------------------------------------------------------- 闲鱼链接回填
def set_link(model, xid="", url="", price=None, xy=""):
    model = str(model or "").strip()
    if not model:
        return {"ok": False, "msg": "缺型号"}
    with _LOCK:
        d = _load()
        d["links"][model] = {"xid": str(xid or "").strip(), "url": str(url or "").strip(),
                             "p": price, "xy": str(xy or ""), "t": _now()}
        _save()
    log("link", model, "回填链接：" + (str(url or "")[:80] or "（清空）"))
    return {"ok": True, "link": d["links"][model]}


# ---------------------------------------------------------------- 水印
def set_water(**kw):
    with _LOCK:
        d = _load()
        w = d["water"]
        for k in ("on", "text", "pos", "size", "color", "alpha"):
            if k in kw:
                w[k] = kw[k]
        d["water"] = w
        _save()
    return {"ok": True, "water": w}


def water_cfg():
    return _load()["water"]


# ---------------------------------------------------------------- 云端写入配额
def quota_bump(n=1):
    d = _load()
    today = time.strftime("%Y-%m-%d")
    q = d["quota"]
    if q.get("d") != today:
        q = {"d": today, "n": 0}
    q["n"] = int(q.get("n") or 0) + int(n)
    d["quota"] = q
    _save()
    return q


def quota_info():
    d = _load()
    q = d["quota"]
    today = time.strftime("%Y-%m-%d")
    n = int(q.get("n") or 0) if q.get("d") == today else 0
    return {"ok": True, "day": today, "n": n, "cap": 1000, "left": max(0, 1000 - n)}


# ---------------------------------------------------------------- 价格嫌疑清单
def suspects(rows):
    """rows: [{m, name, pt, lpz, spz, lp, cost, xp}]（前端把当前卡片的口径传上来）。
    规则（不猜内部数据，只看能看见的数）：
      ① 清理价 ≤0 或 明显低于成本价（< 成本 × 0.7）
      ② 清理价 比 零售指导价 还高（倒挂）
      ③ 价格跳变（跟上次快照比 >30%）
      ④ 同一个型号多货源/多口径价差 > 40%"""
    d = _load()
    pq = d["pq"]
    out = []
    for r in (rows or []):
        m = str(r.get("m") or "").strip()
        if not m:
            continue
        ps = []
        for k in ("pt", "lpz", "spz"):
            v = r.get(k)
            try:
                v = float(v)
            except Exception:
                v = None
            if v and v > 0:
                ps.append((k, v))
        lp = r.get("lp")
        try:
            lp = float(lp)
        except Exception:
            lp = None
        cost = r.get("cost")
        try:
            cost = float(cost)
        except Exception:
            cost = None
        why = []
        # ① 低于成本七成（强信号：填错位数 / 清仓价填成了几十块）
        for k, v in ps:
            if cost and v < cost * 0.7:
                why.append("%s %s 低于成本 %.0f 的七成"
                           % ({"pt": "含税", "lpz": "滞销", "spz": "样机"}[k], v, cost))
        # ② 清理价比零售指导价还高（倒挂：滞销/样机卖得比全新指导价贵，多半填错）
        for k in ("lpz", "spz"):
            v = r.get(k)
            try:
                v = float(v)
            except Exception:
                v = None
            if v and lp and v > lp * 1.05:
                why.append("清理价 %s 比零售指导价 %.0f 还高（倒挂）" % (v, lp))
        # ③ 个位数/几十块的价（十有八九漏了位数）
        for k, v in ps:
            if 0 < v < 100:
                why.append("%s %s 低得可疑（漏位数？）" % ({"pt": "含税", "lpz": "滞销", "spz": "样机"}[k], v))
        # ④ 最近一次跳变超 30%（跟快照比）
        sn = pq.get(m)
        if sn and ps:
            try:
                ov = float(sn.get("v"))
                nv = ps[0][1]
                if ov and abs(nv - ov) / ov > 0.3:
                    why.append("最近一次跳变 %s → %s" % (int(ov), int(nv)))
            except Exception:
                pass
        if why:
            out.append({"m": m, "n": r.get("name") or "", "why": why})
    return {"ok": True, "list": out[:300], "n": len(out)}


# ---------------------------------------------------------------- 图片墙
def wall(groups=None, limit=400):
    """遍历 kitset 根下所有型号，列出各子目录的成片（URL 直接走 /api/kitimg）。"""
    import kitset
    root = kitset.kit_root()
    out = []
    try:
        names = sorted(os.listdir(root))
    except Exception:
        return {"ok": True, "list": [], "root": root}
    subs = ("蓝底", "聚光")
    for nm in names:
        if len(out) >= limit:
            break
        d = os.path.join(root, nm)
        if not os.path.isdir(d) or nm.startswith("_"):
            continue
        item = {"m": nm, "subs": {}}
        for sub in subs:
            sd = os.path.join(d, sub)
            if not os.path.isdir(sd):
                continue
            imgs = []
            try:
                for f in sorted(os.listdir(sd)):
                    if f.lower().endswith((".jpg", ".jpeg", ".png")):
                        imgs.append({"f": f, "u": "/api/kitimg/" + nm + "/" + sub + "/" + f,
                                     "sz": os.path.getsize(os.path.join(sd, f))})
            except Exception:
                pass
            if imgs:
                item["subs"][sub] = imgs
        if item["subs"]:
            out.append(item)
    return {"ok": True, "list": out, "root": root}


# ---------------------------------------------------------------- 参数对照（官网抓到的原始参数）
def params_of(model):
    import kitset
    mdir = kitset.model_dir(model)
    out = {"ok": True, "model": model, "params": [], "titles": [], "points": [],
           "aftersale": "", "src": "", "info": {}}
    p = os.path.join(mdir, "set.json")
    if os.path.isfile(p):
        try:
            with io.open(p, encoding="utf-8") as f:
                j = json.load(f)
            pr = j.get("params") or {}
            out["params"] = [[str(k), str(v)] for k, v in (pr.items() if isinstance(pr, dict) else pr)]
            out["titles"] = j.get("titles") or []
            out["points"] = j.get("points") or []
            out["aftersale"] = j.get("aftersale") or ""
            out["src"] = j.get("src") or ""
            out["name"] = j.get("name") or ""
            try:
                out["blur"] = kitset.blur_warn(os.path.join(mdir, kitset.BLUE_SUB))
            except Exception:
                out["blur"] = {}
        except Exception as e:
            out["msg"] = rep(e)
    ip = os.path.join(mdir, "info.json")
    if os.path.isfile(ip):
        try:
            with io.open(ip, encoding="utf-8") as f:
                out["info"] = json.load(f)
        except Exception:
            pass
    return out


# ---------------------------------------------------------------- 发布前三查
def precheck(model, price=None, link=None):
    import kitset
    import copywriter
    model = str(model or "").strip()
    nm = copywriter.norm_model(model) or model
    mdir = kitset.model_dir(model)
    bd = os.path.join(mdir, kitset.BLUE_SUB)
    imgs = []
    for n in (1, 2, 3, 4):
        fp = os.path.join(bd, "img_%d.jpg" % n)
        if os.path.isfile(fp):
            imgs.append({"n": n, "kb": round(os.path.getsize(fp) / 1024.0)})
    cop = []
    for k in ("l", "s"):
        fp = os.path.join(copywriter.copy_root(), "%s_%s_v4.json" % (nm, k))
        if os.path.isfile(fp):
            cop.append(k)
    d = _load()
    lk = d["links"].get(model) or {}
    pv = price
    try:
        pv = float(price)
    except Exception:
        pv = None
    items = [
        {"k": "图", "ok": len(imgs) >= 4, "x": ("蓝底四张齐" if len(imgs) >= 4
                                              else "蓝底只有 %d 张" % len(imgs))},
        {"k": "主图", "ok": os.path.isfile(os.path.join(mdir, "product.png")),
         "x": "主图在" if os.path.isfile(os.path.join(mdir, "product.png")) else "没有主图（蓝底出图用的那张）"},
        {"k": "文案", "ok": bool(cop), "x": ("有文案：%s" % "/".join(cop)) if cop else "还没生成文案"},
        {"k": "定价", "ok": bool(pv), "x": ("闲鱼价 %s" % pv) if pv else "还没填闲鱼价"},
        {"k": "链接", "ok": bool(lk.get("url") or lk.get("xid")),
         "x": ("已回填链接" if (lk.get("url") or lk.get("xid")) else "发布后把链接贴回来，以后一点就开")},
    ]
    return {"ok": True, "model": model, "items": items,
            "pass": all(i["ok"] for i in items[:4]), "warn": [i["k"] for i in items if not i["ok"]]}


# ---------------------------------------------------------------- 备份 / 诊断包
def _zipdir(zf, src, arc):
    for base, _dirs, files in os.walk(src):
        for f in files:
            fp = os.path.join(base, f)
            rel = os.path.relpath(fp, src)
            try:
                zf.write(fp, os.path.join(arc, rel))
            except Exception:
                pass


def backup():
    """把所有本机数据（三个图库 + extras + settings）打成一个 zip，存 图片库/_backup/。"""
    base = paths.data_base()
    outdir = os.path.join(base, "_backup")
    os.makedirs(outdir, exist_ok=True)
    name = "backup-%s.zip" % time.strftime("%Y%m%d-%H%M%S")
    fp = os.path.join(outdir, name)
    n = 0
    with zipfile.ZipFile(fp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for lib in ("kitset", "peiwork", "copy"):
            d = os.path.join(base, lib)
            if os.path.isdir(d):
                before = len(zf.namelist())
                _zipdir(zf, d, lib)
                n += len(zf.namelist()) - before
        for f in ("extras.json", "settings.json", "payload.json"):
            p = paths.wp(f)
            if os.path.isfile(p):
                try:
                    zf.write(p, "_cfg/" + f)
                    n += 1
                except Exception:
                    pass
    log("misc", "", "备份包已生成：" + fp)
    return {"ok": True, "path": fp, "files": n, "mb": round(os.path.getsize(fp) / 1048576.0, 1)}


def diag(tail=400):
    """崩溃/异常诊断包：日志尾巴 + 状态 + 环境 + extras。给远程排查用。"""
    import platform
    base = paths.wp("_diag")
    os.makedirs(base, exist_ok=True)
    fp = os.path.join(base, "diag-%s.zip" % time.strftime("%Y%m%d-%H%M%S"))
    env = {"platform": platform.platform(), "python": platform.python_version(),
           "frozen": paths.is_frozen(), "data_base": paths.data_base(),
           "work_dir": paths.work_dir(), "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        import state
        env["log"] = list(state.LOG)[-tail:] if hasattr(state.LOG, "__iter__") else []
    except Exception:
        pass
    try:
        import config
        env["version"] = getattr(config, "APP_VERSION", "")
        try:
            from _buildver import BUILD_VERSION
            env["build"] = BUILD_VERSION
        except Exception:
            pass
    except Exception:
        pass
    with zipfile.ZipFile(fp, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("env.json", json.dumps(env, ensure_ascii=False, indent=1, default=str))
        for f in ("app.log", "extras.json", "settings.json"):
            p = paths.wp(f)
            if os.path.isfile(p):
                try:
                    zf.write(p, f)
                except Exception:
                    pass
        for f in ("main.py", "_buildver.py"):
            p = os.path.join(os.path.dirname(os.path.abspath(__file__)), f)
            if os.path.isfile(p):
                try:
                    zf.write(p, "code/" + f)
                except Exception:
                    pass
    return {"ok": True, "path": fp, "mb": round(os.path.getsize(fp) / 1024.0, 1)}


# ---------------------------------------------------------------- 任务队列
def jobs():
    try:
        import jobman
        out = []
        for k, v in list(getattr(jobman, "_JOBS", {}).items()):
            out.append({"k": k, "running": bool(v.get("running")), "done": bool(v.get("done")),
                        "ok": v.get("ok"), "prog": v.get("prog") or "", "err": v.get("err") or ""})
        return {"ok": True, "list": out}
    except Exception as e:
        return {"ok": False, "msg": rep(e), "list": []}


# ---------------------------------------------------------------- 更新日志
CHANGELOG = [
    ("3.0.29", "2026-10-31", [
        "搜索：一个框搜一切（型号/品牌/机器名/品类/内部码/备注/闲鱼号备注），空格=同时满足；搜索不吃浏览筛选，全盘搜",
        "卡片：可卖角标（有货有价 / 缺一头 / 三无）；数据超过 3 小时标「偏旧」",
        "图片库：跟软件安装目录走；蓝底/聚光/官网/欣盛/中关村各自独立文件夹；占用与失效提示",
        "定价：输入要挣多少 → 反推标价（金额或百分比两种），砍价余地默认 0",
        "新增：型号别名、自定义标签、改价历史与一键退回、价格嫌疑清单、多号价格对比",
        "新增：文案多版本、闲鱼链接回填、变更日志、图片墙、参数对照、表格视图",
        "新增：出图前挑主图、只重出单张、失败原因分类与自动重试、图糊预警、出图排队看板",
        "新增：发布前三查、价格/库存变动提醒、云端写入配额、本地备份包、崩溃诊断包、水印",
        "新增：搜索高亮、打字候选、应用内快捷键与全局热键唤出、托盘收进后台",
    ]),
    ("3.0.28", "2026-10-31", [
        "图片存储：所有生成/下载的图片统一到一个总根（默认软件安装目录\\图片库）",
        "目录重组：蓝底 / 聚光 / 官网 / 欣盛 / 中关村 / 其他，各回各家",
        "菜单「显示」→「设置」，新增「图片存储位置」",
    ]),
    ("3.0.27", "2026-10-30", ["聚光套图上线；出图进度实时到步骤；失败必留日志"]),
]


def changelog():
    return {"ok": True, "list": [{"v": v, "d": d, "items": it} for v, d, it in CHANGELOG]}


# ---------------------------------------------------------------- 出图前挑主图
def candidates(model):
    """列出这台机器的全部候选机图（官网/欣盛/中关村/其他 + 当前主图），让用户先挑再出图。"""
    import kitset
    mdir = kitset.model_dir(model)
    out = []
    cur = os.path.join(mdir, "product.png")
    for sub in ("官网", "欣盛", "中关村", "其他", ""):
        d = os.path.join(mdir, sub) if sub else mdir
        if not os.path.isdir(d):
            continue
        try:
            fs = sorted(os.listdir(d))
        except Exception:
            continue
        for f in fs:
            if not f.lower().endswith((".jpg", ".jpeg", ".png")):
                continue
            if not sub and f not in ("product.png", "sizeimg.jpg"):
                continue
            fp = os.path.join(d, f)
            rel = (sub + "/" if sub else "") + f
            try:
                sz = os.path.getsize(fp)
            except Exception:
                sz = 0
            out.append({"rel": rel, "kb": round(sz / 1024.0), "sub": sub or "根",
                        "cur": os.path.abspath(fp) == os.path.abspath(cur),
                        "u": "/api/kitimg/" + str(model) + "/" + rel})
    out.sort(key=lambda x: (not x["cur"], -x["kb"]))
    return {"ok": True, "list": out, "dir": mdir}


def set_main(model, rel):
    """把候选图设为蓝底出图用的主图（product.png）。rel 来自 candidates()。"""
    import kitset
    mdir = kitset.model_dir(model)
    rel = str(rel or "").replace("\\", "/").strip("/")
    if not rel or ".." in rel:
        return {"ok": False, "msg": "路径不合法"}
    src = os.path.abspath(os.path.join(mdir, rel))
    if not src.startswith(os.path.abspath(mdir)) or not os.path.isfile(src):
        return {"ok": False, "msg": "图不在了"}
    if os.path.basename(src) == "product.png":
        return {"ok": True, "msg": "本来就是主图"}
    bak = os.path.join(mdir, "_raw_prod.png")
    cur = os.path.join(mdir, "product.png")
    try:
        if os.path.isfile(cur) and not os.path.isfile(bak):
            shutil.copy2(cur, bak)
        shutil.copy2(src, cur)
    except Exception as e:
        return {"ok": False, "msg": rep(e)}
    log("img", model, "换了主图：" + rel)
    return {"ok": True, "msg": "主图换成 " + rel + "（重出一张/整套才生效）"}


# ---------------------------------------------------------------- 错误归类
def rep(e):
    """异常 → 一句话人话原因（出图/文案失败都走这里，2026-10-31「失败说清原因」）。"""
    s = str(e) or e.__class__.__name__
    low = s.lower()
    if "timeout" in low or "timed out" in low or "connection" in low or "max retries" in low:
        return "网络没通（接口超时/连不上）：%s" % s[:120]
    if "没抓到可用机图" in s or "机图" in s:
        return "图源没货：官网/欣盛/中关村都没抓到这台机器的图"
    if "缺系统字体" in s:
        return "缺字体文件 msyh.ttc（安装包损坏，重装一次）"
    if "key" in low and ("invalid" in low or "401" in low):
        return "AI 接口 key 失效/额度用完：%s" % s[:120]
    if "permission" in low or "拒绝访问" in s or "winerror 5" in low:
        return "没写权限（图片库目录被占/只读）：%s" % s[:120]
    if "no space" in low or "磁盘" in s:
        return "磁盘满了：%s" % s[:120]
    return s[:200]


def classify(e):
    s = str(e).lower()
    if "timeout" in s or "connection" in s or "timed out" in s or "max retries" in s:
        return "net"
    if "没抓到可用机图" in str(e) or "机图" in str(e):
        return "noimg"
    if "缺系统字体" in str(e):
        return "font"
    if "401" in s or "invalid" in s or "quota" in s:
        return "ai"
    if "permission" in s or "winerror 5" in s:
        return "perm"
    return "other"
