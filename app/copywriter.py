# -*- coding: utf-8 -*-
"""copywriter.py — 闲鱼/小红书文案生成（2026-10-12）
链路：官网三站（海尔/卡萨帝/统帅）实抓 → 欣盛商城库兜底 → 都没有才 AI 裸写（禁编参数）。
铁律：文案里出现的数字必须能在抓到的真实数据里找到，找不到整句剔除（防 GLM 漏网编造）。
缓存：copy/<型号>_<l|s>.json，第二次秒出；force=1 才重新调 AI。
图片：copy/<型号>/img_01.jpg...（官网图优先，最多 10 张），由 api_get /copyimg 提供服务。
零第三方依赖：urllib + gzip + re + json + sqlite3。参考实现：haier_scraper/collect.py（接口已验证）。
"""
import gzip
import io
import json
import os
import re
import sqlite3
import time
import urllib.parse
import urllib.request
import zlib

from normalize import norm_model
from paths import wp
from net import urlopen_ssl

API_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
# 2026-10-14 用户拍板：key 打进 exe（子账号机器零配置，放 key 文件不现实）。
# 三级链：环境变量 ML_GLM_KEY → 本机 glm_key.txt（想换新 key 不用重装就改它）→ 内置兜底。
# 内置 key 会随 exe 分发（用户知情接受：单机自用+子账号小圈子，风险换省心）。
_BUILTIN_GLM_KEY = "eedf0a7a971d4a46823185dbabf8b04a.4AraKMykpsOerINf"
def _load_glm_key():
    k = os.environ.get("ML_GLM_KEY") or ""
    if k:
        return k.strip()
    try:
        p = wp("glm_key.txt")
        if os.path.isfile(p):
            with io.open(p, encoding="utf-8-sig") as f:
                t = (f.read() or "").strip()
                if t:
                    return t
    except Exception:
        pass
    return _BUILTIN_GLM_KEY
API_KEY = _load_glm_key()
GLM_MODEL = "glm-4-flash"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 官网三站检索（collect.py 验证过的 code/siteId，2026-10-12 实测仍可用）
SITES = {
    'haier':   {'home': 'https://www.haier.com',     'code': '3e6bff6304c547dba650d9941aa472c0', 'siteId': '2',  'name': '海尔'},
    'casarte': {'home': 'https://www.casarte.com',   'code': '5f8d251ee3664c4ab511ad5d850115ab', 'siteId': '29', 'name': '卡萨帝'},
    'leader':  {'home': 'https://www.leader.com.cn', 'code': '50fc4d3a07844238b4cad0ee303618fa', 'siteId': '20', 'name': '统帅'},
}


def copy_root():
    """文案素材根（2026-10-24 用户定稿：与发布图素材库共用「存储路径」设置）。
    settings.json 设了 data_root → data_root/copy；没设 → work_dir/copy（原行为）。"""
    import paths
    base = paths.data_root()
    d = os.path.join(base, "copy") if base else wp("copy")
    if not os.path.isdir(d):
        try:
            os.makedirs(d)
        except Exception:
            pass
    return d


def _clean(t):
    t = re.sub(r"<[^>]+>", "", str(t or ""))
    t = (t.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
          .replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'"))
    return t.strip()


def fetch(url, extra_headers=None, binary=False, retries=1, timeout=15):
    headers = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9",
               "Accept-Encoding": "gzip, deflate"}
    if extra_headers:
        headers.update(extra_headers)
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urlopen_ssl(req, timeout=timeout) as r:
                raw = r.read()
                enc = r.headers.get("Content-Encoding", "")
                if enc and "gzip" in enc:
                    raw = gzip.decompress(raw)
                elif enc and "deflate" in enc:
                    try:
                        raw = zlib.decompress(raw)
                    except zlib.error:
                        raw = zlib.decompress(raw, -zlib.MAX_WBITS)
                return raw if binary else raw.decode("utf-8", errors="replace")
        except Exception as e:
            last = e
            if i < retries:
                time.sleep(0.8)
    raise last


# ---------------------------------------------------------------- 官网抓取
def site_search(sk, word):
    s = SITES[sk]
    q = urllib.parse.urlencode({
        'code': s['code'], 'siteId': s['siteId'], 'searchWord': word,
        'pageNumber': 1, 'pageSize': 20,
        'searchColumns': 'pcode,specifications,propic,promoteUrls,psale_time,productBigClassName',
        'psale_order': 0, 'promoteUrlCounts': 1})
    d = json.loads(fetch(f"{s['home']}/igs/front/search.jhtml?{q}", extra_headers={
        'Referer': f"{s['home']}/search/", 'X-Requested-With': 'XMLHttpRequest'}))
    return (d.get('page') or {}).get('content') or d.get('rows') or d.get('data') or []


def _strip_font(s):
    return re.sub(r'<[^>]+>', '', str(s or ''))


def match_row(rows, nm):
    for r in rows:
        m = norm_model(_strip_font(r.get('modelno') or r.get('modelno_extra') or ''))
        if m and (m in nm or nm in m):
            return r
    return None


TOKEN_RE = re.compile(r'<span class="name_text">(.*?)</span>|<div class="value">(.*?)</div>', re.S)


def parse_params_haier(html):
    """海尔模板：<div class="params js_params"> 名字→下一个值 顺序配对"""
    groups = []
    for m in re.finditer(r'<div class="params js_params">(.*?)(?=<div class="params js_params">|\Z)', html, re.S):
        block = m.group(1)
        tm = re.search(r'<span class="title_text">(.*?)</span>', block, re.S)
        gname = _clean(tm.group(1)) if tm else "参数"
        pairs, cur = [], None
        for t in TOKEN_RE.finditer(block):
            name_v, value_v = t.group(1), t.group(2)
            if name_v is not None:
                cur = _clean(name_v)
            elif cur is not None:
                v = _clean(value_v)
                if cur and v and v not in ('-', '—'):
                    pairs.append((cur, v))
                cur = None
        if pairs:
            groups.append((gname, pairs))
    return groups


def parse_spec_leader(seg):
    """统帅模板：h3.product_detail_title + product_detail_row 嵌套 item"""
    groups, cur, kv = [], None, []
    for t in re.finditer(
            r'<h3 class="product_detail_title"[^>]*>(.*?)</h3>'
            r'|<div class="product_detail_row">\s*<div class="product_detail_item">(.*?)</div>\s*'
            r'<div class="product_detail_item">(.*?)</div>', seg, re.S):
        h3, k, v = t.group(1), t.group(2), t.group(3)
        if h3 is not None:
            g = _clean(h3)
            if not re.search(r'推荐|相关|猜你|热门', g):
                if cur and kv:
                    groups.append((cur, kv))
                cur, kv = g, []
        elif cur is not None:
            kk, vv = _clean(k), _clean(v)
            if kk and vv and vv not in ('-', '—') and kk != vv:
                kv.append((kk, vv))
    if cur and kv:
        groups.append((cur, kv))
    return groups


def parse_spec_casarte(seg):
    """卡萨帝模板：h3>span 组名 + label/value 对"""
    groups, cur, kv = [], '基本参数', []
    for t in re.finditer(
            r'<h3>\s*<span>(.*?)</span>\s*</h3>'
            r'|<div class="label">(.*?)</div>\s*<div class="value">(.*?)</div>', seg, re.S):
        h3, label, value = t.group(1), t.group(2), t.group(3)
        if h3 is not None:
            g = _clean(h3)
            if not re.search(r'推荐|相关|猜你|热门', g):
                if kv:
                    groups.append((cur, kv))
                cur, kv = g, []
        elif label is not None:
            k, v = _clean(label), _clean(value)
            if k and v and v not in ('-', '—') and k != v:
                kv.append((k, v))
    if kv:
        groups.append((cur, kv))
    return groups


def parse_groups(html):
    """三模板自适应（collect.py 踩过的顺序：海尔 js_params → 统帅 row → 卡萨帝 label/value）"""
    groups = parse_params_haier(html)
    if not groups:
        m = re.search(r'sd_key="specifications"', html)
        if m:
            seg = html[m.start():m.start() + 30000]
            stop = re.search(r'<!--\s*(?:相关推荐|猜你喜欢|服务支持|产品介绍|更多推荐)\s*-->|hope-anchor-value=', seg)
            if stop:
                seg = seg[:stop.start()]
            groups = parse_spec_leader(seg) or parse_spec_casarte(seg)
    return groups


def parse_sellpoints(html):
    m = re.search(r'<div class="sell_point.*?">(.*?)<!--\s*卖点\s*end\s*-->', html, re.S)
    if m:
        pts = [_clean(v) for v in re.findall(r'<span class="value">(.*?)</span>', m.group(1), re.S)]
        pts = [p for p in pts if p]
        if pts:
            return pts
    m = re.search(r'<!--\s*产品基本\s*start\s*-->(.*?)<!--\s*产品基本\s*end\s*-->', html, re.S)
    if m:
        sp = [_clean(x) for x in re.findall(r'<span[^>]*>([^<]{2,12})</span>', m.group(1))]
        return [s for s in sp if re.search(r'[\u4e00-\u9fff]', s) and s != '参考价' and not s.endswith('：')]
    return []


IMG_RE = re.compile(r'<img[^>]+(?:data-src|data-original|lazy-src|src)="([^"]+\.(?:jpg|jpeg|png|webp))"', re.I)
# 2026-10-13 用户定稿「官网有多少张下多少张」：只剔真垃圾（logo/图标/占位/1px），
# 背面/侧面/细节图全要（之前删 back/rear/背面的规则废除）
BAD_IMG = re.compile(r'logo|icon|erweima|qrcode|sprite|placeholder|banner|bitmap|homePage|'
                     r'productDetail_phone|_bg_|nocomment|headSet|mask|template|1x1|pixel'
                     r'|favicon|weibo|weixin|share|avatar', re.I)


def extract_images(html):
    """官网图：域名白名单 + 去装饰 + www 死链域名自动换 image.* 救活（collect.py 经验）"""
    urls, seen = [], set()
    for m in IMG_RE.finditer(html):
        u = m.group(1).strip()
        if u.startswith('//'):
            u = 'https:' + u
        if u not in seen:
            seen.add(u)
            urls.append(u)
    for m in re.finditer(r'srcset="([^"]+)"', html, re.I):
        for part in m.group(1).split(','):
            u = part.strip().split(' ')[0]
            if re.search(r'\.(jpg|jpeg|png|webp)(\?|$)', u, re.I) and u not in seen:
                seen.add(u)
                urls.append(u)
    out = []
    stems = set()
    for u in urls:
        host = urllib.parse.urlparse(u).netloc.lower()
        if not (host.endswith('haier.com') or host.endswith('casarte.com') or host.endswith('leader.com.cn')):
            continue
        if BAD_IMG.search(u):
            continue
        # 同图去重（_1200/_600 双尺寸只留先出现的高清张，collect.py 经验）
        stem = re.sub(r'_(1200|600)\.\w+$', '', os.path.basename(urllib.parse.urlparse(u).path))
        if stem in stems:
            continue
        stems.add(stem)
        out.append(u)
    # www 死链域名 → image. 域名变体（下载时双保险，谁活着用谁）
    fixed = list(out)
    for u in out:
        m = re.match(r'(https://www\.(?:haier|casarte|leader)\.com(?:\.cn)?)/(.+)', u)
        if m:
            alt = (f'https://image.{m.group(1).split("www.")[1]}/{m.group(2)}'
                   if 'leader' not in m.group(1) else f'https://image.leader.com.cn/{m.group(2)}')
            if alt not in seen:
                fixed.append(alt)
    return fixed


def fetch_official(model, log):
    """官网三站顺序找；命中详情页→参数/卖点/产品名/价/图。没命中返回 None。"""
    nm = norm_model(model)
    for sk in ('haier', 'casarte', 'leader'):
        s = SITES[sk]
        row = None
        for w in (model, re.sub(r'[^0-9A-Za-z-]', '', model), nm[:6]):
            try:
                row = match_row(site_search(sk, w), nm)
            except Exception:
                row = None
            if row:
                break
            time.sleep(0.4)
        if not row:
            continue
        pname = _clean(_strip_font(str(row.get('pname') or '')))
        price = _clean(str(row.get('price') or ''))
        url = str(row.get('docPubUrl') or row.get('docPuburl') or row.get('url') or '').strip()
        if url and not url.startswith('http'):
            url = 'https://' + url.lstrip('/')
        if not url:
            continue
        try:
            html = fetch(url, extra_headers={'Referer': s['home'] + '/'})
        except Exception:
            continue
        title = _clean((re.search(r'<title>(.*?)</title>', html, re.S) or [None, ''])[1])
        if nm not in norm_model(title + ' ' + pname):
            continue  # 防串页
        groups = parse_groups(html)
        log("官网[%s]命中：%s / 参数 %d 组" % (s['name'], pname[:30], len(groups)))
        return {'source': '官网', 'brand': s['name'], 'pname': pname, 'price': price or '',
                'url': url, 'groups': [[g, kv] for g, kv in groups],
                'sellpoints': parse_sellpoints(html), 'imgs': extract_images(html)}
    return None


# ---------------------------------------------------------------- 欣盛兜底
def _load_model_code_map():
    """payload.json 里 mo→code 对码表（卡片 YS物料编码 = 欣盛商品编码同一套）"""
    out = {}
    try:
        with io.open(wp("payload.json"), encoding="utf-8") as f:
            for r in (json.load(f).get("rows") or []):
                mo, code = r.get("mo") or "", r.get("code") or ""
                if mo and code:
                    out[norm_model(mo)] = code
    except Exception:
        pass
    return out


TOK_RE = re.compile(r'[A-Z]{1,4}\d[A-Z0-9\-]{4,}')


def fetch_xinsheng(model, log):
    """欣盛商城库（只读）：payload 对码 → 名字抠型号兜底。"""
    p = wp("xinsheng.sqlite3")
    if not os.path.isfile(p):
        return None
    nm = norm_model(model)
    try:
        con = sqlite3.connect("file:%s?mode=ro" % p.replace("\\", "/"), uri=True)
        code = _load_model_code_map().get(nm)
        row = None
        if code:
            row = con.execute("SELECT code,name,sale_price,album_urls FROM products WHERE code=?",
                              (code,)).fetchone()
        if not row:
            for code, name, sp, al in con.execute(
                    "SELECT code,name,sale_price,album_urls FROM products "
                    "WHERE code IS NOT NULL AND code != ''"):
                toks = TOK_RE.findall((name or '').upper())
                if toks:
                    tn = norm_model(max(toks, key=len))
                    if tn and (tn == nm or tn in nm or nm.startswith(tn)):
                        row = (code, name, sp, al)
                        break
        con.close()
    except Exception:
        return None
    if not row:
        return None
    code, name, sp, al = row
    album = []
    try:
        album = [u for u in json.loads(al or "[]") if isinstance(u, str) and u.startswith("http")]
    except Exception:
        pass
    log("欣盛商城命中：%s" % (name or '')[:30])
    return {'source': '欣盛商城', 'brand': '', 'pname': name or model, 'price': str(sp or ''),
            'url': 'https://ys.longyangdq.cn/', 'groups': [], 'sellpoints': [], 'imgs': album}


# ---------------------------------------------------------------- GLM 文案
def _chat(prompt):
    if not API_KEY:
        raise RuntimeError("未配置 GLM key（软件数据目录放 glm_key.txt，或环境变量 ML_GLM_KEY）")
    body = json.dumps({"model": GLM_MODEL, "messages": [{"role": "user", "content": prompt}],
                       "temperature": 0.7}).encode("utf-8")
    req = urllib.request.Request(API_URL, data=body, headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + API_KEY})
    with urlopen_ssl(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))["choices"][0]["message"]["content"]


PROMPT = """你是闲鱼/小红书家电卖家文案写手。产品：{pname}（型号 {model}）。

【铁律】
1) 只能使用【真实数据】里出现的参数、功能和数字，绝对禁止编造数据里没有的参数、功能或数字。{no_params}
2) titles：闲鱼标题三条（三选一）。分段式、段与段之间一个空格；不带价格、不带emoji、不带#话题；每条≤30字；三条开头语序必须各不同；
   每条都必须有：型号全码 + 成色词（正品写「全新未拆封」，样机写「展示样机」）+ 服务词（全国联保/原厂正品联保）。参考版式（品牌/容量/核心卖点/品类词照【真实数据】换）：
   {brand} 226L风冷无霜冰箱 {model} 全新未拆封 家用三门 全国联保
   全新未拆封 {brand} 226L 三门冰箱 风冷无霜 {model} 原厂正品联保
   {brand}三门冰箱 226L 风冷无霜 {model} 全新原封全国联保
   {kk_titles}
3) points：正好12-15条加厚卖点（宁可写满15条，绝不许偷懒只给几条）。每条20-45字，格式＝「核心功能（括号里带具体参数/数字）＋给用户的好处」，一条只讲一个卖点，从【真实数据】的参数表里逐项提炼（总容积/耗电/能效等级/压缩机/制冷方式/变温区/面板材质/尺寸/重量/噪音/冷冻能力/净化除菌/智能功能/开门方式/搁物架/照明……有什么写什么，参数表里每一行都尽量用上）。
   绝对禁止「智能生活新选择」「品质生活从XX开始」「高效XX利器」这类官网空洞口号。
   参考口吻（照这个密度写，具体参数数字必须换成【真实数据】里的）：
   一级双变频（变频压缩机+变频离心风机），省电节能运行平稳降噪
   风冷无霜，不用手动除霜，制冷均匀食材不结冰
   中门全变温区（果蔬/珍品/零度/软冻/冷冻五种模式），一台顶多台
4) xhs：正好5组，每组是 {{"t":"标题","txt":"配文"}} 两个键。t：小红书标题≤20字、可带emoji、但绝对不能带#话题；txt：100-160字真实买家分享口吻，必须分成2-3个自然段（每段以一个emoji开头，如 ✨/🧊/💪），至少引用2个【真实数据】里的具体参数或功能（越具体越像真用过的人），txt 末尾另起一行带3-6个#话题（如 #海尔 #冰箱 #家电分享）——#话题只能出现在 txt 末尾，绝不放进 t；5组必须是5个不同角度（价格惊喜/成色/功能亮点/使用场景/情绪共鸣），禁止同一句换emoji充数，禁止空话套话堆砌。
5) 输出纯 JSON（不要 markdown 代码块）：
{{"titles":["","",""],"points":["","","","","","","","","","","","","","",""],"xhs":[{{"t":"","txt":""}},{{"t":"","txt":""}},{{"t":"","txt":""}},{{"t":"","txt":""}},{{"t":"","txt":""}}]}}

【真实数据】
{facts}"""


def _facts_text(data, model):
    lines = ["型号: " + model]
    if data.get('pname'):
        lines.append("产品名: " + data['pname'])
    if data.get('price'):
        lines.append("价格: " + str(data['price']))
    for g, kv in (data.get('groups') or []):
        lines.append("[%s] %s" % (g, "；".join("%s:%s" % (a, b) for a, b in kv)))
    if data.get('sellpoints'):
        lines.append("官网卖点: " + "、".join(data['sellpoints']))
    return "\n".join(lines)


NUM_RE = re.compile(r"\d+(?:\.\d+)?")
SHIPY_RE = re.compile(r"发货|可发|物流|运费|包邮|偏远|送装|到家")

_FACT_FLOATS = {}


def _fact_floats(facts):
    key = id(facts)
    if key not in _FACT_FLOATS:
        _FACT_FLOATS[key] = [float(x) for x in NUM_RE.findall(facts)]
    return _FACT_FLOATS[key]


def _verify(ai, facts, nums_set):
    """防编造：titles/points 整行剔除；xhs 成对校验（t/txt 任一含未知数字整组剔）。"""
    fl = _fact_floats(facts)

    def ok(line):
        for n in NUM_RE.findall(line):
            if n in facts or n in nums_set:
                continue
            try:
                if float(n) in fl:
                    continue
            except ValueError:
                pass
            return False
        return True

    out = {}
    out['titles'] = [t for t in ai.get('titles') or [] if isinstance(t, str) and ok(t)][:3]
    out['points'] = [p for p in ai.get('points') or [] if isinstance(p, str) and ok(p)][:15]
    xh = []
    for x in (ai.get('xhs') or []):
        if isinstance(x, dict) and isinstance(x.get('t'), str) and isinstance(x.get('txt'), str):
            if ok(x['t']) and ok(x['txt']):
                xh.append({'t': x['t'].strip(), 'txt': x['txt'].strip()})
    out['xhs'] = xh[:5]
    return out


def glm_copy(model, kind, data, log):
    facts = _facts_text(data, model)
    has_params = bool(data.get('groups'))
    no_params = ("（本次没有抓到参数表，只有型号：文案里绝不允许出现任何具体参数和数字，"
                 "只写成色/交易保障/通用卖点）" if not has_params else "")
    brand = (data.get('brand') or '').strip() or '品牌'
    kk_titles = ('注意：这是正品全新机——标题里必须用「全新未拆封/全新原封」，绝不许出现「展示样机/样机」字样。'
                 if kind != 's' else
                 '注意：这是展示样机——标题里必须用「展示样机/功能完好」，绝不许出现「全新未拆封/全新原封/全新」字样。')
    nums_set = set(NUM_RE.findall(norm_model(model)))
    # 2026-10-13 用户反馈「小红书/闲鱼拉胯」整改：质量闸（剔空洞口号/一句话配文）
    # + 质量不够自动重试一次（两次取分高那次），AI 解析失败也不再炸整单
    best, best_score = None, -1
    if not API_KEY:
        log("未配置 GLM key（glm_key.txt）：AI 裸写跳过，只用官网实抓 + 欣盛兜底")
    for attempt in range(2):
        try:
            raw = _chat(PROMPT.format(model=model, pname=data.get('pname') or model, brand=brand,
                                      no_params=no_params, kk_titles=kk_titles, facts=facts))
            m = re.search(r'\{.*\}', raw, re.S)
            ai = json.loads(m.group(0)) if m else {}
        except Exception as e:
            log("AI 第 %d 次生成失败：%s" % (attempt + 1, e))
            ai = {}
        res = _verify(ai, facts, nums_set)
        # 价格尾巴兜底：AI 没听话写出 1649.0元 → 1649元（4.5kg 这类真小数不受影响）
        for _k in ('titles', 'points'):
            res[_k] = [re.sub(r'(\d)\.0+(?=[^\d]|$)', r'\1', s) for s in res[_k]]
        for x in res['xhs']:
            x['t'] = re.sub(r'(\d)\.0+(?=[^\d]|$)', r'\1', x['t'])
            x['txt'] = re.sub(r'(\d)\.0+(?=[^\d]|$)', r'\1', x['txt'])
        # 质量闸：卖点 8-60 字（剔「智能生活新选择」类口水话）；配文 ≥50 字且带#话题
        res['points'] = [p for p in res['points'] if 8 <= len(p) <= 60]
        res['xhs'] = [x for x in res['xhs'] if len(x.get('txt', '')) >= 50 and '#' in x.get('txt', '')]
        score = len(res['xhs']) * 10 + len(res['points']) + len(res['titles'])
        if score > best_score:
            best, best_score = res, score
        if len(res['xhs']) >= 4 and len(res['points']) >= 12:
            break
        log("第 %d 次生成质量不够（配文 %d/5、卖点 %d/15），自动重试" % (attempt + 1, len(res['xhs']), len(res['points'])))
    res = best or {'titles': [], 'points': [], 'xhs': []}
    dropped = ((3 - len(res['titles'])) + (15 - len(res['points'])) + (5 - len(res['xhs'])))
    if dropped > 0:
        log("防编造/质量闸共剔除 %d 条不合格内容" % dropped)
    return res


# ---------------------------------------------------------------- 描述拼装
def build_desc(model, kk, data, pts, ship='nat'):
    """商品描述加厚版式（2026-10-13 用户拿参考文案定稿）：【产品名】型号 / ·卖点×N（全量加厚条）
    / 【参数】【官网价】【交易】 / 结尾话术 + 可发货地区。
    ship='nat' 全国版；'rep' 报备版（深圳/东莞/惠州/广州）。"""
    brand = (data.get('brand') or '').strip() or '原厂'
    nm = str(model).strip()
    head = re.sub(r'\s*' + re.escape(nm) + r'\s*', ' ', str(data.get('pname') or '')).strip()
    head = re.sub(r'\s{2,}', ' ', head) or (brand + '家电')
    L = ['【%s】型号 %s' % (head, nm), '']
    for p in (pts or [])[:15]:
        L.append('·' + p)
    L.append('')
    flat = []
    for _g, kv in (data.get('groups') or []):
        for a, b in kv:
            s = '%s %s' % (a, b)
            if s not in flat:
                flat.append(s)
    if flat:
        L.append('【参数】' + ' | '.join(flat[:10]))
    if str(data.get('price') or '').strip():
        L.append('【官网价】%s （本店售价见标价，%s）'
                 % (data['price'], '全新未拆封' if kk == 'l' else '展示样机'))
    L.append('【交易】先验后签收，破损拒收')
    L.append('')
    if kk == 'l':
        L.append('%s官方正品，全新未拆封，全国联保' % brand)
        L.append('品质保证，放心购买，售后无忧')
    else:
        L.append('%s官方正品，展示样机，功能完好' % brand)
        L.append('外观可能有轻微展示痕迹，介意慎拍')
    L.append('支持专业送货安装，当地售后师傅上门安装')
    L.append('让您买得放心，用得安心！')
    L.append('可发货地区：全国' if ship == 'nat' else '可发货地区：深圳/东莞/惠州/广州（可送装到位）')
    return '\n'.join(L)


# ---------------------------------------------------------------- 图片落盘
def dl_images(pairs, mdir, log):
    """配图落盘（2026-10-13 用户定稿：官网图+欣盛图两路全下，一张不扔）：
    pairs = [(url, 来源), ...]；copy/<型号>/01.jpg、02.jpg… 编号命名，
    返回 [{"f":"01.jpg","src":"官网"}, ...]（前端按来源分组展示）。"""
    os.makedirs(mdir, exist_ok=True)
    for old in os.listdir(mdir):
        try:
            os.remove(os.path.join(mdir, old))
        except Exception:
            pass
    saved = []
    for u, src in pairs:
        ext = os.path.splitext(urllib.parse.urlparse(u).path)[1].lower() or ".jpg"
        ext = ext if ext in ('.jpg', '.jpeg', '.png', '.webp') else '.jpg'
        try:
            # 图片 CDN 要带自身域名的 Referer，否则 403（实测）
            raw = fetch(u, extra_headers={'Referer': urllib.parse.urlparse(u).scheme + '://' +
                                          urllib.parse.urlparse(u).netloc + '/'},
                        binary=True, timeout=20)
            if len(raw) < 3000:
                continue
            name = "%02d%s" % (len(saved) + 1, ext)
            with open(os.path.join(mdir, name), "wb") as f:
                f.write(raw)
            saved.append({"f": name, "src": src or "官网"})
        except Exception:
            continue
    n_off = sum(1 for x in saved if x['src'] == '官网')
    log("配图落盘 %d 张（官网 %d + 欣盛 %d）" % (len(saved), n_off, len(saved) - n_off))
    return saved


def make_featured(mdir, price, log):
    """促销首图（2026-10-13 学用户上货工具）：主图 01.* 居中白底 + 左下价格角标 → 0_主图_卖点拼版.jpg"""
    srcs = [f for f in os.listdir(mdir) if re.match(r'^01\.(jpe?g|png|webp)$', f, re.I)]
    if not srcs:
        return ''
    try:
        from PIL import Image, ImageDraw, ImageFont   # PyInstaller: hiddenimports 已含 PIL（openpyxl 依赖 pillow）
    except Exception:
        log('Pillow 没装，跳过促销首图')
        return ''
    try:
        im = Image.open(os.path.join(mdir, srcs[0])).convert('RGB')
        canvas = Image.new('RGB', (900, 900), (250, 250, 250))
        scale = min(820.0 / im.width, 820.0 / im.height, 1.0)
        im2 = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))))
        canvas.paste(im2, ((900 - im2.width) // 2, (900 - im2.height) // 2))
        p = str(price or '').strip()
        if p:
            try:
                pf = float(p)
                p = str(int(pf)) if pf == int(pf) else str(round(pf, 2))
            except ValueError:
                pass
            font = None
            for fn in ('C:/Windows/Fonts/msyhbd.ttc', 'C:/Windows/Fonts/msyh.ttc', 'C:/Windows/Fonts/simhei.ttf',
                           '/System/Library/Fonts/STHeiti Medium.ttc', '/System/Library/Fonts/PingFang.ttc'):
                try:
                    font = ImageFont.truetype(fn, 46)
                    break
                except Exception:
                    continue
            d = ImageDraw.Draw(canvas)
            if font:
                txt = '+ ' + p
                bb = d.textbbox((0, 0), txt, font=font)
                tw, th = bb[2] - bb[0], bb[3] - bb[1]
                x, y = 56, 900 - 56 - th - 30
                d.rounded_rectangle([x, y, x + tw + 52, y + th + 30], radius=18, fill=(26, 32, 48))
                d.text((x + 26, y + 13), txt, font=font, fill=(255, 255, 255))
        out = os.path.join(mdir, '0_主图_卖点拼版.jpg')
        canvas.save(out, quality=88)
        return os.path.basename(out)
    except Exception as e:
        log('促销首图合成失败：%s' % e)
        return ''


# ---------------------------------------------------------------- 主入口
def generate(model, kind='l', force=0, log=None):
    log = log or (lambda s: None)
    nm = norm_model(model) or model
    kk = 's' if kind == 's' else 'l'
    cache = os.path.join(copy_root(), "%s_%s_v4.json" % (nm, kk))   # v4：官网+欣盛双源全图（旧缓存不读）
    if not force and os.path.isfile(cache):
        try:
            with io.open(cache, encoding="utf-8") as f:
                res = json.load(f)
            res["cached"] = True
            return res
        except Exception:
            pass
    # 2026-10-13 用户定稿：官网图跟欣盛图都下载——两路都查，不再二选一
    data = fetch_official(model, log)
    xs = fetch_xinsheng(model, log)
    if not data and not xs:
        log("官网+欣盛都没命中，AI 只按型号裸写（禁参数）")
        data = {'source': '无数据', 'brand': '', 'pname': model, 'price': '',
                'url': '', 'groups': [], 'sellpoints': [], 'imgs': []}
    elif not data:
        data = xs
        xs = None
    elif xs and not str(data.get('price') or '').strip() and str(xs.get('price') or '').strip():
        data['price'] = xs['price']   # 官网没挂价 → 欣盛价兜底
    # 价格统一整数显示：抓来的 "1649.0" 直接进标题会带小数尾巴（2026-10-12 实锤）
    _pr = str(data.get('price') or '').strip()
    try:
        _f = float(_pr)
        data['price'] = str(int(_f)) if _f == int(_f) else str(round(_f, 2))
    except ValueError:
        pass
    ai = glm_copy(model, kk, data, log)
    mdir = os.path.join(copy_root(), nm)
    # 图片合并：官网图在前、欣盛图在后，URL 去重后全部落盘
    pairs, _seen = [], set()
    for u in (data.get('imgs') or []):
        if u and u not in _seen:
            _seen.add(u)
            pairs.append((u, '官网'))
    if xs:
        for u in (xs.get('imgs') or []):
            if u and u not in _seen:
                _seen.add(u)
                pairs.append((u, '欣盛'))
    images = dl_images(pairs, mdir, log)
    # 2026-10-24 用户令：0_主图_卖点拼版.jpg 所有的都不用做，自动合成已停
    # 卖点：AI 从真实参数提炼的加厚条优先（2026-10-13 用户定稿，官网 sellpoint 是
    # 「智能生活新选择」类空洞口号不要）；AI 全被剔才拿官网卖点兜底
    pts = ai['points'][:15] or [p for p in (data.get('sellpoints') or []) if p][:15]
    res = {"ok": True, "cached": False, "model": model, "kind": kk,
           "source": data['source'], "source_url": data['url'],
           "pname": data['pname'], "price": data['price'],
           "titles": ai['titles'], "points": ai['points'],
           "desc_nat": build_desc(model, kk, data, pts, 'nat'),
           "desc_rep": build_desc(model, kk, data, pts, 'rep'),
           "xhs": ai['xhs'],
           "images": images}
    try:
        with io.open(cache, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False)
    except Exception:
        pass
    return res


# ------------------------------------------------ AI 润色/自检（2026-10-13 对齐闲鱼配文pro）
POLISH_TITLES_PROMPT = """你是闲鱼标题润色师。下面是同一型号的三条备选标题，请在不改变事实的前提下重写润色：
- 只能使用原标题和【真实数据】里出现的参数/数字，禁止编造
- 每条≤30字、分段式（段与段之间一个空格）、不带价格、不带emoji、不带#话题
- 三条开头语序必须各不同；保留型号全码和成色词（{kk_rule}）
- 比原标题更顺口、更有点击欲
输出纯 JSON（不要 markdown）：{{"titles":["","",""]}}

【真实数据】
{facts}

【原标题】
{orig}"""

POLISH_DESC_PROMPT = """你是闲鱼商品描述润色师。请润色下面这段商品描述：
- 只能使用描述原文和【真实数据】里出现的参数/数字，禁止编造任何新参数新数字
- 保留原有版式（【产品名】行、·卖点条目、【参数】【官网价】【交易】、结尾话术、可发货地区都保留）
- 卖点条目可以改写得更顺口更打动人，但条数不许减少
- 不加 emoji，不加 #话题
只输出润色后的完整描述正文，不要任何解释。

【真实数据】
{facts}

【原描述】
{orig}"""

QA_PROMPT = """你是闲鱼文案审核员。对下面的文案做合规自检，逐条挑毛病：
1) 成色口径：{kk_rule}
2) 标题/卖点条目里不许出现人民币价格（如「1899元」「售价」字样）；型号、容量、尺寸这类参数数字不算价格
3) 数字必须能在【真实数据】里找到（防编造）
4) 不许出现「最好」「第一」「顶级」「国家级」「极致」等广告法极限词
5) 不许空话套话（如「智能生活新选择」「品质生活从XX开始」）
只挑真实存在的问题，没问题不要硬挑。输出纯 JSON（不要 markdown）：
{{"issues":[{{"where":"标题A","level":"高","note":"问题一句话说明"}}]}}

【真实数据】
{facts}

【待检文案】
{orig}"""

_AD_LIMIT = re.compile(r'最好|最佳|最强|最优|第一|顶级|国家级|世界级|极致|百分百|100%|全网最|销量第一')


def _cache_path(model, kind):
    nm = norm_model(model) or model
    return os.path.join(copy_root(), "%s_%s_v4.json" % (nm, 's' if kind == 's' else 'l'))


def _load_cache(model, kind):
    try:
        with io.open(_cache_path(model, kind), encoding="utf-8") as f:
            d = json.load(f)
        return d if d.get("ok") else None
    except Exception:
        return None


def _save_cache(model, kind, d):
    try:
        with io.open(_cache_path(model, kind), "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
    except Exception:
        pass


def _facts_of(model, kind):
    """润色/自检用的真实数据：从缓存描述反推不够全，直接重抓一次官网（快，秒级）"""
    d = fetch_official(model, lambda s: None)
    return _facts_text(d or {'pname': model, 'groups': [], 'sellpoints': []}, model) or ("型号: " + model)


def polish(model, kind, target, text, log=None):
    """AI 润色：target='titles'（重写三选一）| 'desc'（润色描述正文）。带防编造校验。"""
    log = log or (lambda s: None)
    kk = 's' if kind == 's' else 'l'
    facts = _facts_of(model, kk)
    nums_set = set(NUM_RE.findall(norm_model(model)))
    fl = [float(x) for x in NUM_RE.findall(facts)]

    def ok_nums(line):
        for n in NUM_RE.findall(line):
            if n in facts or n in nums_set:
                continue
            try:
                if float(n) in fl:
                    continue
            except ValueError:
                pass
            return False
        return True

    if target == 'titles':
        cache_d = _load_cache(model, kk)
        orig = "\n".join((cache_d.get('titles') if cache_d else []) or []) or str(text or '')
        kk_rule = ('成色词必须用「全新未拆封/全新原封」，绝不许出现「样机」字样' if kk != 's'
                   else '成色词必须用「展示样机/功能完好」，绝不许出现「全新」字样')
        raw = _chat(POLISH_TITLES_PROMPT.format(kk_rule=kk_rule, facts=facts, orig=orig))
        m = re.search(r'\{.*\}', raw, re.S)
        titles = [t.strip() for t in (json.loads(m.group(0)).get('titles') or [])
                  if isinstance(t, str) and t.strip()][:3]
        titles = [re.sub(r'(\d)\.0+(?=[^\d]|$)', r'\1', t) for t in titles]
        good = [t for t in titles if ok_nums(t) and not _AD_LIMIT.search(t)]
        if cache_d and good:
            cache_d['titles'] = good
            _save_cache(model, kk, cache_d)
        return {"ok": True, "titles": good, "dropped": len(titles) - len(good)}
    # desc：正文直出，只做数字防编造校验（不合规段落回退原文）
    kk_rule = ('成色词必须用「全新未拆封/全新原封」，绝不许出现「样机」字样' if kk != 's'
               else '成色词必须用「展示样机/功能完好」，绝不许出现「全新」字样')
    new = _chat(POLISH_DESC_PROMPT.format(facts=facts, kk_rule=kk_rule, orig=str(text or ''))).strip()
    new = re.sub(r'^```[a-z]*\s*|\s*```$', '', new).strip()
    # 数字防编造：逐行校验，带未知数字的行回退原文对应行
    if text:
        old_lines = str(text).split('\n')
        out = []
        for i, ln in enumerate(new.split('\n')):
            if ln.strip() and not ok_nums(ln) and i < len(old_lines) and ok_nums(old_lines[i]):
                out.append(old_lines[i])
            else:
                out.append(ln)
        new = '\n'.join(out)
    return {"ok": True, "desc": new}


def qa_copy(model, kind, text, log=None):
    """AI 自检：对标题+卖点+描述整体挑毛病，另附广告法极限词本地硬扫。"""
    log = log or (lambda s: None)
    kk = 's' if kind == 's' else 'l'
    facts = _facts_of(model, kk)
    kk_rule = ('这是正品全新机——文案里必须用「全新未拆封」，绝不许出现「展示样机/样机」字样' if kk != 's'
               else '这是展示样机——文案里必须用「展示样机」，绝不许出现「全新未拆封/全新」字样')
    issues = []
    # 本地硬扫：极限词 + 成色词冲突（不花钱不卡顿，必出）
    for i, ln in enumerate(str(text or '').split('\n')):
        if not ln.strip():
            continue
        mm = _AD_LIMIT.search(ln)
        if mm:
            issues.append({"where": "第%d行" % (i + 1), "level": "高",
                           "note": "广告法极限词「%s」，闲鱼被投诉风险" % mm.group(0)})
        if kk != 's' and re.search(r'样机', ln):
            issues.append({"where": "第%d行" % (i + 1), "level": "高",
                           "note": "正品机文案出现「样机」字样，成色口径冲突"})
        if kk == 's' and re.search(r'全新未拆封|全新原封', ln):
            issues.append({"where": "第%d行" % (i + 1), "level": "高",
                           "note": "样机文案出现「全新」字样，成色口径冲突"})
    # GLM 语义自检
    try:
        raw = _chat(QA_PROMPT.format(kk_rule=kk_rule, facts=facts, orig=str(text or '')))
        m = re.search(r'\{.*\}', raw, re.S)
        for it in (json.loads(m.group(0)).get('issues') or []):
            if isinstance(it, dict) and it.get('note'):
                issues.append({"where": str(it.get('where') or '?'), "level": str(it.get('level') or '中'),
                               "note": str(it['note'])})
    except Exception as e:
        log("AI 自检失败（本地扫描结果仍有效）：%s" % e)
    return {"ok": True, "issues": issues}


def enrich(model, kind, force, style="", log=None):
    """生成+peiwen后处理（2026-10-25 从 api_get /api/copy try 块原样搬入，零逻辑变化）。
    Mac WKWebView fetch ~60s 超时对策：生成走 jobman 后台线程，本函数可被线程调用。"""
    log = log or (lambda s: None)
    res = generate(model, kind, force, log=log)
    _info = None
    try:
        import pubwork as _pw
        import pei_core as _pc
        _info = _pw.load_info(model)
        if not _info or not (_info.get("props") or []):
            try:
                _info = _pw.work(model, prog=lambda p, m: log(m))
            except Exception as _we:
                log("peiwen 抓取链未命中（%s），用内置算法" % _we)
                _info = None
        if _info and (_info.get("props") or []):
            _pts = [str(x).strip() for x in (res.get("points") or [])
                    if str(x).strip()][:6]
            res["points"] = [str(x).strip() for x in (res.get("points") or [])
                             if str(x).strip()][:6]
            res["titles"] = _pc.titles(_info)
            res["desc_nat"] = _pc.desc_text(_info, "N", _pts or None)
            res["desc_rep"] = _pc.desc_text(_info, "B", _pts or None)
            res["desc_src"] = "peiwen"
            res["brand"] = str(_info.get("brand") or "").strip()
    except Exception:
        pass
    if style == "v2":
        try:
            _kk2 = 's' if kind == 's' else 'l'
            if _info and (_info.get("props") or []):
                res["titles"] = _pc.titles_v2(_info, _kk2)
                _v2p = [str(x).strip() for x in (res.get("points") or [])
                        if str(x).strip()][:6]
                _dv2 = _pc.desc_v2(_info, _kk2, _v2p or None)
            else:
                from normalize import norm_model as _nm2
                _pn = str(res.get("pname") or "")
                _brand = ""
                for _b in ("卡萨帝", "统帅", "Leader", "海尔"):
                    if _b in _pn:
                        _brand = _b
                        break
                _fi = {"brand": _brand or "海尔",
                       "model": _nm2(res.get("model") or "") or str(res.get("model") or ""),
                       "title": _pn, "props": [], "cat": ""}
                res["titles"] = _pc.titles_v2(_fi, _kk2)
                _v2p = [str(x).strip() for x in (res.get("points") or [])
                        if str(x).strip()][:6]
                _dv2 = _pc.desc_v2(_fi, _kk2, _v2p or None)
            res["desc_nat"] = _dv2
            res["desc_rep"] = _dv2
            res["copy_style"] = "v2"
        except Exception:
            pass
    return res
