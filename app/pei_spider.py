# -*- coding: utf-8 -*-
"""海尔 / 卡萨帝 / 统帅 官网参数爬虫（独立单文件版，不依赖本项目其它模块）

来源：家电文案生成器的 sources.py（抓取）+ locate.py（兜底定位），把 config.py 里的
站点常量内联进来，所以可以单独拷到任何地方跑。

用法：
    python official_spider.py BCD-500WGHFDB5XAU1                # 自动三站依次搜
    python official_spider.py C1 D10L7ELU1 --brand casarte      # 指定站点
    python official_spider.py 型号 --json out.json              # 顺手存 JSON
    python official_spider.py --url https://www.haier.com/...   # 直接解析某个详情页
    python official_spider.py --search 冰箱                      # 只搜列表、不抓详情

三条抓取路径（按顺序兜底）：
    ① 官网检索接口（/igs/front/search.jhtml）—— 只收录「在架在售」型号
    ② 命中后抓详情页 HTML —— 参数表 + 产品介绍原文 + 说明书 pdf + 参考价
    ③ 检索搜不到（下架/老款）→ Bing 站内搜索定位详情页 URL，再走 ②
       ⚠️ ③ 必须校验「详情页 <title> 里有目标型号」，否则宁可留空，
          绝不把别的型号的参数安到用户头上。

全程只读，不写任何文件（除你显式指定的 --json）。
"""
import argparse
import gzip
import html as _html
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import urlparse

# ==========================================================================
# 站点常量（原 config.py 的那部分，已内联）
# ==========================================================================
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
FETCH_TIMEOUT = 25
SEARCH_PAGE_SIZE = 20
SEARCH_COLUMNS = "pcode,specifications,propic,promoteUrls,psale_time,productBigClassName"
SEARCH_EXTRA = {"psale_order": "0", "promoteUrlCounts": "1"}
BING = "https://www.bing.com/search"

SOURCES = {
    "haier": {
        "name": "海尔官网",
        "api": "https://www.haier.com/igs/front/search.jhtml",
        "code": "3e6bff6304c547dba650d9941aa472c0",
        "site_id": "2",
        "site_ids": ["2"],
        "referer": "https://www.haier.com/search/",
        "home": "https://www.haier.com",
        "model_key": "modelno_extra",   # 该库为干净型号
        "brand": "海尔",
        "spec_inline": False,           # 该库搜索结果不含参数，需再抓详情页
    },
    "casarte": {
        "name": "卡萨帝官网",
        "api": "https://www.casarte.com/igs/front/search.jhtml",
        "code": "5f8d251ee3664c4ab511ad5d850115ab",
        "site_id": "29",
        "site_ids": ["29"],
        "referer": "https://www.casarte.com/search/",
        "home": "https://www.casarte.com",
        "model_key": "modelno",         # 该库型号含高亮标签，取用时清洗
        "brand": "卡萨帝",
        "spec_inline": False,
    },
    "leader": {
        "name": "统帅官网",
        "api": "https://www.leader.com.cn/igs/front/search.jhtml",
        "code": "50fc4d3a07844238b4cad0ee303618fa",
        "site_id": "20",                # 官网自带 searchSiteId
        "site_ids": ["20", "29"],       # 20 失败则回退 29（实测可用）
        "referer": "https://www.leader.com.cn/search/",
        "home": "https://www.leader.com.cn",
        "model_key": "modelno",
        "brand": "统帅",
        "spec_inline": False,
    },
}

_TAG = re.compile(r"<[^>]+>")
_CJK = re.compile(r"[\u4e00-\u9fff]")
_LINK = re.compile(r"<link>\s*(https?://[^<\s]+)\s*</link>")
_GROUP_TITLE = re.compile(
    r"^\s*(规格参数|规格&参数|基本参数|基本信息|功能参数|主要参数|技术参数|"
    r"包装清单|其他参数|性能参数|安装参数|参数)$")
_BRAND_TOKENS = ("Casarte", "casarte", "CASARTE", "Haier", "haier", "HAIER",
                 "Leader", "leader", "LEADER", "卡萨帝", "海尔", "统帅")

# 三站证书都能正常校验；这里保留「关校验」是为了兼容某些站点证书链不全的情况。
# 想严格一点就把这两行删掉。
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


# ==========================================================================
# 基础工具
# ==========================================================================
def clean_text(s):
    """去标签 + 反转义 + 去零宽字符"""
    if s is None:
        return ""
    s = _TAG.sub("", str(s))
    s = _html.unescape(s)
    s = s.replace("\u200b", "").replace("\xa0", " ").replace("\u3000", " ")
    return re.sub(r"\s+", " ", s).strip()


def norm_model(s):
    """型号归一化：只留字母数字并大写，用于精确比对"""
    return re.sub(r"[^0-9A-Za-z]", "", str(s or "")).upper()


def _http(url, referer=None, ajax=False, timeout=None):
    h = {
        "User-Agent": USER_AGENT,
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if referer:
        h["Referer"] = referer
    if ajax:
        h["Accept"] = "application/json, text/javascript, */*; q=0.01"
        h["X-Requested-With"] = "XMLHttpRequest"
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout or FETCH_TIMEOUT, context=_CTX) as r:
        raw = r.read()
        # 手动解 gzip：urllib 不认站点返回的「Content-Encoding: gzip」时不会自己解
        if r.headers.get("Content-Encoding") == "gzip":
            try:
                raw = gzip.decompress(raw)
            except Exception:
                pass
        for enc in ("utf-8", "gbk", "gb18030"):
            try:
                return raw.decode(enc)
            except Exception:
                continue
        return raw.decode("utf-8", "replace")


def _abs_url(u, home):
    if not u:
        return ""
    u = u.strip()
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("/"):
        return home + u
    return u


# ==========================================================================
# ① 官网检索接口
# ==========================================================================
def search(keyword, source_key, page_size=None):
    """调官网检索接口，返回候选列表（真实官网数据）"""
    cfg = SOURCES[source_key]
    params = {
        "code": cfg["code"],
        "searchWord": keyword,
        "pageNumber": 1,
        "pageSize": page_size or SEARCH_PAGE_SIZE,
        "searchColumns": SEARCH_COLUMNS,
    }
    params.update(SEARCH_EXTRA)

    last_err = None
    for sid in cfg.get("site_ids") or [cfg["site_id"]]:
        params["siteId"] = sid
        url = cfg["api"] + "?" + urllib.parse.urlencode(params)
        try:
            data = json.loads(_http(url, cfg["referer"], ajax=True))
        except Exception as e:              # 网络/接口异常 -> 换下一个 siteId
            last_err = e
            continue
        items = (data.get("page") or {}).get("content") or []
        if items:
            return [_to_candidate(it, source_key, cfg) for it in items]
        last_err = last_err or "接口返回 0 条"
    if last_err:
        raise RuntimeError("【%s】检索失败：%s" % (cfg["name"], last_err))
    return []


def _to_candidate(item, source_key, cfg):
    home = cfg["home"]
    model = clean_text(item.get(cfg["model_key"]) or item.get("modelNo")
                       or item.get("modelno_extra") or item.get("modelno"))
    url = ""
    for k in ("docPubUrl", "docPuburl", "url"):
        if item.get(k):
            url = _abs_url(item[k], home)
            break
    price = clean_text(item.get("price"))
    try:
        if price and float(price) == 0:     # 接口对无价商品返回 0，别当成「免费」
            price = ""
    except Exception:
        pass
    return {
        "source": source_key,
        "source_name": cfg["name"],
        "brand": cfg["brand"],
        "model": model,
        "pname": clean_text(item.get("pname") or item.get("productClassName")),
        "price": price,
        "url": url,
        "spec": apply_specs(item.get("specifications")) if cfg.get("spec_inline") else [],
        "cate": clean_text(item.get("productBigClassName") or item.get("productClassName")),
        "selling": parse_sale_points(item.get("proSalePoint") or item.get("salePoints")),
        "labels": parse_labels(item.get("productShowLabel"),
                               item.get("filterSearch") or item.get("filterSesrch")),
        "manual": _abs_url(item.get("bookUrl") or "", home),
        "raw": item,
    }


# ==========================================================================
# 参数 / 卖点 / 标签 解析
# ==========================================================================
def apply_specs(spec_raw):
    """解析接口内联的 specifications（GROUP / Field 结构）"""
    if not spec_raw:
        return []
    try:
        obj = json.loads(spec_raw) if isinstance(spec_raw, str) else spec_raw
    except Exception:
        return []
    groups = []
    for g in obj.get("GROUP") or []:
        name = clean_text(g.get("name") or "参数")
        kv = []
        for f in g.get("Field") or []:
            if not isinstance(f, dict):
                continue
            for k, v in f.items():
                k, v = clean_text(k), clean_text(v)
                if k and v:
                    kv.append((k, v))
        if kv:
            groups.append((name, kv))
    return groups


def _looks_like_prose(txt):
    """判断抓到的文字是不是真·人话（防止把整块图片区 / HTML 残渣当介绍）"""
    if not txt or len(txt) < 12:
        return False
    if "<" in txt or "class=" in txt or "src=" in txt or "&" in txt:
        return False
    cjk = len(_CJK.findall(txt))
    return cjk / max(1, len(txt)) >= 0.35


def parse_sale_points(raw):
    """官网卖点（proSalePoint / salePoints 的 maintitle 列表）"""
    if not raw:
        return []
    try:
        obj = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return []
    if not isinstance(obj, list):
        return []
    out = []
    for it in obj:
        if not isinstance(it, dict):
            continue
        t = clean_text(it.get("maintitle"))
        if t and t != "卖点标题" and t not in out:   # 「卖点标题」是模板占位，不是真卖点
            out.append(t)
    return out[:12]


def parse_labels(show_label, filter_tags):
    """官网标签：productShowLabel（双喷淋|270）+ filterSearch（银/灰色,变频,...）"""
    out = []
    for part in str(show_label or "").split("|"):
        p = clean_text(part)
        if p.isdigit() or not p or p in out:
            continue
        out.append(p)
    for part in re.split(r"[,，]", str(filter_tags or "")):
        p = clean_text(part)
        if p and not p.isdigit() and p not in out:
            out.append(p)
    return out[:16]


def parse_spec_html(html_text):
    """从详情页 HTML 抓参数表。三站结构不同，逐个兼容：

    海尔   : <div class="params js_params"> -> title_text + name_text / value
    卡萨帝 : <ul class="sd_spec" sd_key="specifications"> + <li>label / value</li>
             分组标题 <h3><span>xx</span></h3>
    统帅   : <div class="sd_spec" sd_key="specifications">
             <h3 class="product_detail_title">xx</h3>
             <div class="product_detail_row">两个 product_detail_item</div>
    """
    # ---- 海尔结构
    groups = []
    for blk in html_text.split('<div class="params js_params">')[1:]:
        t = re.search(r'<span class="title_text">(.*?)</span>', blk, re.S)
        gname = clean_text(t.group(1)) if t else "参数"
        kv = []
        for k, v in re.findall(
                r'<span class="name_text">(.*?)</span>.*?<div class="value">(.*?)</div>',
                blk, re.S):
            k, v = clean_text(k), clean_text(v)
            if k and v and v not in ("-", "—", "--"):
                kv.append((k, v))
        if kv:
            groups.append((gname, kv))
    if groups:
        return groups

    # ---- 卡萨帝 / 统帅结构
    idx = html_text.find('sd_key="specifications"')
    if idx < 0:
        return []
    segment = html_text[idx:idx + 30000]
    # 参数块后面会跟「相关推荐 / 猜你喜欢」，必须切掉，否则把推荐位的词也当参数
    stop = re.search(r'<!--\s*(相关推荐|猜你喜欢|服务支持|产品介绍)|hope-anchor-value="'
                     r'(?!parameter)', segment)
    if stop:
        segment = segment[:stop.start()]
    return _walk_spec(segment)


def _walk_spec(segment):
    """按文档顺序扫三种标记（分组标题 / label+value 对 / product_detail_item 对）"""
    tok = re.compile(
        r"<h3[^>]*>\s*(?:<span>)?(?P<g>[^<>]{1,24}?)(?:</span>)?\s*</h3>"
        r"|<div class=\"label\">(?P<k1>.*?)</div>\s*<div class=\"value\">(?P<v1>.*?)</div>"
        r"|<div class=\"product_detail_item\">(?P<k2>.*?)</div>\s*"
        r"<div class=\"product_detail_item\">(?P<v2>.*?)</div>",
        re.S)
    groups, cur_name, cur_kv = [], "基本参数", []

    def flush():
        nonlocal cur_kv
        if cur_kv:
            groups.append((cur_name, cur_kv))
        cur_kv = []

    for m in tok.finditer(segment):
        if m.group("g") is not None:
            g = clean_text(m.group("g"))
            if not g or any(x in g for x in ("推荐", "相关", "猜你", "热门")):
                continue
            flush()
            cur_name = g
            continue
        if m.group("k1") is not None:
            k, v = clean_text(m.group("k1")), clean_text(m.group("v1"))
        else:
            k, v = clean_text(m.group("k2")), clean_text(m.group("v2"))
        if k and v and v not in ("-", "—", "--") and k != v:
            cur_kv.append((k, v))
    flush()
    # 合并同名分组
    merged = []
    for g, kv in groups:
        for mg, mkv in merged:
            if mg == g:
                mkv.extend(kv)
                break
        else:
            merged.append((g, list(kv)))
    return merged


def parse_intro_html(html_text, limit=600):
    """抓详情页「产品介绍 / 产品特点」官网原文。抓不到人话就返回空，绝不硬凑。"""
    candidates = []
    m = re.search(r"<!--\s*产品介绍 start\s*-->(.*?)<!--\s*产品介绍 end\s*-->",
                  html_text, re.S)
    if m:
        candidates.append(m.group(1))
    i = html_text.find('sd_key="feature"')
    if i >= 0:
        gt = html_text.find(">", i)
        if gt > 0:
            seg = html_text[gt + 1:gt + 9000]
            nxt = seg.find('sd_key="')
            if nxt > 200:
                seg = seg[:nxt]
            candidates.append(seg)
    m = re.search(r'<meta\s+name="description"\s+content="([^"]{15,})"', html_text, re.I)
    if m:
        candidates.append(m.group(1))

    for body in candidates:
        body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", body, flags=re.S | re.I)
        txt = clean_text(body)
        txt = re.sub(r"<[^<>]*$", "", txt).strip()
        if _looks_like_prose(txt):
            return txt[:limit]
    return ""


def fetch_detail(url, source_key):
    """抓详情页，返回 (spec_groups, intro, title, 说明书url, 参考价)"""
    cfg = SOURCES[source_key]
    html_text = _http(url, cfg["referer"], ajax=False)
    title = ""
    m = re.search(r"<title>(.*?)</title>", html_text, re.S)
    if m:
        title = clean_text(m.group(1))
    book = ""
    m = re.search(r'bookAppFile["\']?\s*[:=]\s*["\']([^"\']+\.pdf)', html_text, re.I)
    if m:
        book = _abs_url(m.group(1), cfg["home"])
        if not book.endswith(".pdf"):
            book = cfg["home"] + m.group(1)
    # 参考价形如 <span class="label">参考价</span> … <span class="js_text">7999</span>，
    # 中间隔着几十个标签字符，所以先按窗口去标签、再取数字
    price = ""
    i = html_text.find("参考价")
    if i >= 0:
        seg = _TAG.sub(" ", html_text[i:i + 400])
        m = re.search(r"参考价\s*[¥￥]?\s*([\d][\d,]*\.?\d*)", seg)
        if m:
            price = m.group(1).replace(",", "").strip(".")
    return (parse_spec_html(html_text), parse_intro_html(html_text),
            title, book, price)


# ==========================================================================
# ③ 兜底：Bing 站内搜索定位详情页
# ==========================================================================
def host_of(source_key):
    return SOURCES[source_key]["home"].split("//")[-1].strip("/")


def _keep(u, host):
    """只留该官网自己的内容页；Bing 自身的 /search 链接里也含目标域名，必须按 netloc 排除"""
    try:
        p = urlparse(u)
    except Exception:
        return False
    netloc = (p.netloc or "").split("@")[-1].split(":")[0].lower()
    if not (netloc == host or netloc.endswith("." + host)):
        return False
    if "/search" in (p.path or "").lower():
        return False
    return len((p.path or "").strip("/")) > 0


def web_locate(keyword, source_key, limit=6):
    """Bing 站内搜索定位官网详情页 URL（RSS 输出，纯 XML、无验证码，比抓 HTML 稳）"""
    host = host_of(source_key)
    found = []
    for q in ('site:%s "%s"' % (host, keyword), "site:%s %s" % (host, keyword)):
        url = BING + "?" + urllib.parse.urlencode({"q": q, "format": "rss"})
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/rss+xml, application/xml, text/xml, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Accept-Encoding": "gzip, deflate",
            })
            with urllib.request.urlopen(req, timeout=20, context=_CTX) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    try:
                        raw = gzip.decompress(raw)
                    except Exception:
                        pass
                xml = raw.decode("utf-8", "replace")
        except Exception:
            continue
        for u in _LINK.findall(xml):
            u = u.strip()
            if _keep(u, host) and u not in found:
                found.append(u)
        if found:
            break
    return found[:limit]


# ==========================================================================
# 顶层：型号 -> 完整产品
# ==========================================================================
def _build(source_key, cand):
    """把候选补全为完整产品：官网检索库不含参数，需再抓详情页"""
    cfg = SOURCES[source_key]
    groups, intro, title, book, price = [], "", "", "", ""
    if not cand["spec"] and cand["url"]:
        time.sleep(0.6)                     # 限速，别高频打官网
        try:
            groups, intro, title, book, price = fetch_detail(cand["url"], source_key)
        except Exception:
            groups, intro, title, book, price = [], "", "", "", ""
    else:
        groups = cand["spec"]
    return {
        "model": cand["model"],
        "brand": cand["brand"],
        "pname": cand["pname"] or title,
        "price": cand["price"] or price,
        "params": groups,
        "intro": intro,
        "selling": cand.get("selling") or [],
        "labels": cand.get("labels") or [],
        "manual_url": book or cand.get("manual") or "",
        "source": cand["source_name"],
        "source_url": cand["url"],
        "cate": cand.get("cate", ""),
    }


def _build_from_url(url, source_key, target, keyword, cands):
    """兜底：从详情页 URL 直接构建产品。

    ⚠️ 防串页：必须校验「详情页 <title> 里含有目标型号」，否则一律丢弃。
    Bing 可能返回别的型号的页面，宁可留空，也绝不把别的型号参数安到用户头上。
    """
    cfg = SOURCES[source_key]
    time.sleep(0.5)
    try:
        groups, intro, title, book, price = fetch_detail(url, source_key)
    except Exception:
        return None
    if not groups or not target or target not in norm_model(title):
        return None

    name = re.split(r"[_|]", title or "")[0].strip("【】[] 　")
    for tok in _BRAND_TOKENS:
        name = name.replace(tok, "")
    name = clean_text(name) or (keyword or "").strip()

    cands.insert(0, {
        "source": source_key, "source_name": cfg["name"] + "·产品详情页",
        "brand": cfg["brand"], "model": (keyword or "").strip(), "pname": name,
        "price": price, "url": url, "spec": [], "cate": "",
        "selling": [], "labels": [], "manual": book, "raw": {},
    })
    return {
        "model": (keyword or "").strip(),
        "brand": cfg["brand"], "pname": name, "price": price,
        "params": groups, "intro": intro, "selling": [], "labels": [],
        "manual_url": book, "source": cfg["name"] + "·产品详情页",
        "source_url": url, "cate": "",
        "via": "详情页兜底（该型号不在官网检索索引里）",
    }


def resolve(keyword, source_keys=None):
    """输入型号/关键词 -> (完整产品 | None, 候选列表)

    官网检索索引只收录「在架在售」型号，下架 / 老款型号搜不到但详情页还在，
    ⇒ 这时用 Bing 站内搜索定位官网详情页，抓到的仍是官网原文参数。
    """
    keys = source_keys or list(SOURCES.keys())
    target = norm_model(keyword)
    all_cands = []
    for k in keys:
        try:
            cands = search(keyword, k)
        except Exception:
            continue
        all_cands.extend(cands)
        for c in cands:                     # 精确型号命中
            if target and norm_model(c["model"]) == target:
                return _build(k, c), all_cands

    if target:                              # ---- 检索索引里没有 -> 站内搜索定位
        for k in keys:
            try:
                urls = web_locate(keyword, k)
            except Exception:
                urls = []
            for u in urls:
                prod = _build_from_url(u, k, target, keyword, all_cands)
                if prod:
                    return prod, all_cands
    return None, all_cands


def params_to_text(prod):
    """产品 -> 给大模型的结构化参数文本（全部官网原文）"""
    lines = []
    if prod.get("brand"):
        lines.append("品牌：%s" % prod["brand"])
    if prod.get("pname"):
        lines.append("产品名称：%s" % prod["pname"])
    if prod.get("model"):
        lines.append("型号：%s" % prod["model"])
    if prod.get("cate"):
        lines.append("品类：%s" % prod["cate"])
    lines.append("官方指导价：%s" % (prod.get("price") or "官网未标注"))
    if prod.get("source_url"):
        lines.append("数据来源：%s（%s）" % (prod.get("source", "官网"), prod["source_url"]))
    for gname, kv in prod.get("params") or []:
        lines.append("")
        lines.append("—— %s ——" % gname)
        for k, v in kv:
            lines.append("%s：%s" % (k, v))
    if prod.get("selling"):
        lines.append("")
        lines.append("—— 官网卖点（原文关键词）——")
        lines.append("、".join(prod["selling"]))
    if prod.get("labels"):
        lines.append("")
        lines.append("—— 官网标签 ——")
        lines.append("、".join(prod["labels"]))
    if prod.get("intro"):
        lines.append("")
        lines.append("—— 官网产品介绍（原文节选）——")
        lines.append(prod["intro"])
    if not (prod.get("params") or prod.get("intro") or prod.get("selling")):
        lines.append("")
        lines.append("（官网未返回参数明细，仅使用上方已知信息，缺失字段不得编造）")
    return "\n".join(lines)


# ==========================================================================
# CLI
# ==========================================================================
def _print_prod(prod, cands):
    if not prod:
        print("❌ 没抓到。官网检索没命中、站内搜索也没定位到该型号的详情页。")
        if cands:
            print("\n官网检索到的相近型号（供核对型号有没有写错）：")
            for c in cands[:10]:
                print("  %-28s %-8s %s" % (c["model"], c["source_name"], c["pname"][:34]))
        return 1
    p = prod
    print("=" * 78)
    print("品牌     %s" % p.get("brand", ""))
    print("型号     %s" % p.get("model", ""))
    print("品名     %s" % p.get("pname", ""))
    print("品类     %s" % p.get("cate", ""))
    print("指导价   %s" % (p.get("price") or "官网未标注"))
    print("来源     %s" % p.get("source", ""))
    print("详情页   %s" % p.get("source_url", ""))
    if p.get("manual_url"):
        print("说明书   %s" % p["manual_url"])
    if p.get("via"):
        print("兜底说明 %s" % p["via"])
    n_kv = sum(len(kv) for _g, kv in (p.get("params") or []))
    print("=" * 78)
    print("参数 %d 条 / 卖点 %d 条 / 标签 %d 条 / 产品介绍 %d 字"
          % (n_kv, len(p.get("selling") or []), len(p.get("labels") or []),
             len(p.get("intro") or "")))
    print("-" * 78)
    for gname, kv in p.get("params") or []:
        print("【%s】" % gname)
        for k, v in kv:
            print("   %-22s %s" % (k, v))
    if p.get("selling"):
        print("【官网卖点】")
        for s in p["selling"]:
            print("   · %s" % s)
    if p.get("labels"):
        print("【官网标签】 %s" % "、".join(p["labels"]))
    if p.get("intro"):
        print("【产品介绍原文】")
        print("   %s" % p["intro"])
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="海尔/卡萨帝/统帅 官网参数爬虫")
    ap.add_argument("keyword", nargs="?", help="型号或关键词，如 BCD-500WGHFDB5XAU1")
    ap.add_argument("--brand", default="auto",
                    choices=["auto", "haier", "casarte", "leader"],
                    help="指定站点（默认三站依次搜）")
    ap.add_argument("--url", help="不搜，直接解析这个详情页 URL")
    ap.add_argument("--search", action="store_true", help="只搜列表、不抓详情页")
    ap.add_argument("--json", metavar="FILE", help="把结果存成 JSON")
    ap.add_argument("--text", action="store_true", help="按「给大模型的结构化文本」打印")
    args = ap.parse_args(argv)

    keys = list(SOURCES.keys()) if args.brand == "auto" else [args.brand]

    # ---- 直接解析 URL
    if args.url:
        sk = args.brand if args.brand != "auto" else (
            "haier" if "haier.com" in args.url else
            "casarte" if "casarte.com" in args.url else
            "leader" if "leader.com.cn" in args.url else "haier")
        try:
            groups, intro, title, book, price = fetch_detail(args.url, sk)
        except Exception as e:
            print("❌ 这个详情页抓不到：%s" % e)
            return 1
        prod = {"model": args.keyword or "", "brand": SOURCES[sk]["brand"],
                "pname": title, "price": price, "params": groups, "intro": intro,
                "selling": [], "labels": [], "manual_url": book,
                "source": SOURCES[sk]["name"], "source_url": args.url, "cate": ""}
        if args.json:
            with open(args.json, "w", encoding="utf-8") as f:
                json.dump(prod, f, ensure_ascii=False, indent=2)
            print("已存 %s" % args.json)
        if args.text:
            print(params_to_text(prod))
            return 0
        return _print_prod(prod, [])

    if not args.keyword:
        ap.print_help()
        return 2

    # ---- 只搜列表
    if args.search:
        rc = 1
        for k in keys:
            try:
                cands = search(args.keyword, k)
            except Exception as e:
                print("【%s】%s" % (SOURCES[k]["name"], e))
                continue
            print("【%s】%d 条" % (SOURCES[k]["name"], len(cands)))
            for c in cands[:15]:
                print("  %-28s %-34s %s" % (c["model"], c["pname"][:32], c["price"] or "-"))
            rc = 0
        return rc

    # ---- 完整流程
    t0 = time.time()
    prod, cands = resolve(args.keyword, keys)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"product": prod, "candidates": cands}, f,
                      ensure_ascii=False, indent=2, default=str)
        print("已存 %s" % args.json)
    if args.text and prod:
        print(params_to_text(prod))
        return 0
    rc = _print_prod(prod, cands)
    print("\n(耗时 %.1fs)" % (time.time() - t0))
    return rc


if __name__ == "__main__":
    sys.exit(main())
