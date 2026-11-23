# 第三套文案（2026-11-17 用户定稿）：卖家干货直叙体——像真闲鱼卖家随手打的，
# 口语、长短错落、一条塞足干货；禁广告腔（拉满/正合适/拿得出手）、禁个人感受、
# 禁承诺回复时效。标题=品牌+品类+容量+型号+全新，不堆卖点。
STYLE3_X = """

【本单特殊要求——第三套文案（卖家干货体）】
这一单是同一台机器的第三套文案，写法必须跟「参数直给型」「场景型」都拉开距离：
- 身份：你就是卖机器的卖家，写给买家看的干货介绍——不是广告词，不是使用感想；
  「我觉得/真省心/我家实测」这类第一人称感受一律不写；「看到就回/随时在线」这类
  回复时效承诺一律不写；「拉满/正合适/拿得出手」这类空话一律不写。
- 句式：口语直叙、长短错落，像随手打出来的，别每条一个模子、别对仗工整。
- 内容：每条 = 具体功能 + 具体细节（参数/数字/用法），一条顶一条，不许空洞。
- 标题三条：品牌 + 品类 + 容量 + 型号全码 + 全新机为主，不堆卖点，每条≤30字，
  成色词用「全新机」，服务词保留一条联保类。
- 卖点 5-8 条，每条仍锚定【真实数据】的真实参数，数字一个不许编。
"""


# -*- coding: utf-8 -*-
"""copywriter.py — 闲鱼/小红书文案生成（2026-10-12）
链路：官网三站（海尔/卡萨帝/统帅）实抓 → 欣盛商城库兜底 → 都没有才 AI 裸写（禁编参数）。
铁律：文案里出现的数字必须能在抓到的真实数据里找到，找不到整句剔除（防 GLM 漏网编造）。
缓存：copy/<型号>_<l|s>.json，第二次秒出；force=1 才重新调 AI。
图片：copy/<型号>/img_01.jpg...（官网图优先，最多 10 张），由 api_get /copyimg 提供服务。
零第三方依赖：urllib + gzip + re + json + sqlite3。参考实现：haier_scraper/collect.py（接口已验证）。
"""
import gzip
import html as _html_lib
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


def _safe_dir(nm):
    """目录名拦点段（2026-11-04 P0-2）：model='..' 会把清目录/rmtree 顶出 copy 根。
    型号名正常不含 '..'，含了就当未命名。"""
    nm = str(nm or "").strip()
    if not nm or ".." in nm:
        return "未命名"
    return nm
from paths import wp
from net import urlopen_ssl

API_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
# 2026-11-12 安全整改：内置明文 key 已删除。
# 起因：key 硬编码在公开仓库，被爬虫抓走跑 DeepSeek 烧钱（硅基流动账单实锤）。
# 现在统一走 aikeys：env ML_GLM_KEY → 本机 glm_key.txt → 本机缓存/云端 /aikey 下发。
def _load_glm_key():
    try:
        import aikeys
        return (aikeys.glm_key() or "").strip()
    except Exception:
        return ""


def _api_key():
    """惰性取 key（启动时没登录也能在登录后拿到云端下发的那份）。"""
    return _load_glm_key()
# 2026-10-29 用户令「那个好用不花钱就用那个」：glm-4-flash → glm-4.5-flash（免费新旗舰，
# 实测同 key 直接可用）。4.5 系默认带深度思考（reasoning_content 先烧 max_tokens 再出正文，
# 小 max_tokens 会只吐思考空正文），写文案场景统一 thinking disabled 直出。
GLM_MODEL = "glm-4.5-flash"
THINK_OFF = {"type": "disabled"}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 官网三站检索（collect.py 验证过的 code/siteId，2026-10-12 实测仍可用）
SITES = {
    'haier':   {'home': 'https://www.haier.com',     'code': '3e6bff6304c547dba650d9941aa472c0', 'siteId': '2',  'name': '海尔'},
    'casarte': {'home': 'https://www.casarte.com',   'code': '5f8d251ee3664c4ab511ad5d850115ab', 'siteId': '29', 'name': '卡萨帝'},
    'leader':  {'home': 'https://www.leader.com.cn', 'code': '50fc4d3a07844238b4cad0ee303618fa', 'siteId': '20', 'name': '统帅'},
}


def copy_root():
    """文案素材根（2026-10-24 与发布图素材库共用「存储路径」设置；2026-10-31 起跟图片总根走：
    总根 = exe 目录\图片库（打包版默认），settings.json 设了 data_root → data_root/copy）。"""
    import paths
    d = os.path.join(paths.data_base(), "copy")
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
                        raw = zlib.decompress(raw, -zlib.MAX_WBITS)
                    except zlib.error:
                        pass
                return raw if binary else raw.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            # 2026-11-11：统帅站 leader.com.cn 挂 WAF，本机非浏览器 TLS 指纹全 403。
            # 兜底走 CF Worker /osfetch 代抓（Cloudflare 出口指纹能过盾）。只对海尔系域名生效。
            if getattr(e, "code", 0) in (403, 404, 503) and _OS_HOST_RE.match(url or ""):
                try:
                    return _relay_fetch(url, headers, binary=binary, timeout=timeout)
                except Exception:
                    pass
            last = e
            if i < retries:
                time.sleep(0.8)
        except Exception as e:
            # 2026-11-10：image.haier.com 对本机出口是 TCP RST（不是 403），网络层异常
            # 同样可能是 IP 封禁——海尔系域名一律试一次 CF 代抓再认输。
            if _OS_HOST_RE.match(url or ""):
                try:
                    return _relay_fetch(url, headers, binary=binary, timeout=timeout)
                except Exception:
                    pass
            last = e
            if i < retries:
                time.sleep(0.8)
    raise last


# 海尔系官网域名（代抓兜底只对这些生效）
_OS_HOST_RE = re.compile(r"^https?://[^/]*(?:haier\.com|casarte\.com|leader\.com\.cn)/", re.I)

# 2026-11-21 用户令「IP 都被限制成爬虫了，解决下」：海尔 WAF 按 IP 封（casarte/haier 全站
# 403，真 Chrome 也一样 → 纯 IP 黑名单）。原逻辑每条请求都先直连挨一记 403 才走代抓——
# 白挨打还容易把封禁升级。加 403 冷却表：某域名吃一次 403 后 30 分钟内直连跳过、
# 直接走 CF 代抓（osfetch 实测 casarte 200）；冷却期内代抓也不成功才回直连试。
_RELAY_COOLDOWN = {}          # host -> 冷却截止 time.time()
_RELAY_COOLDOWN_SECS = 1800   # 30 分钟


def _relay_hot(url):
    """该 URL 的域名是否在 403 冷却期内（期内直连没意义，直接代抓）。"""
    if not _OS_HOST_RE.match(url or ""):
        return False
    host = urllib.parse.urlparse(url).netloc.lower()
    return time.time() < _RELAY_COOLDOWN.get(host, 0)


def _relay_mark(url):
    """记一笔 403：该域名进入 30 分钟冷却。"""
    if _OS_HOST_RE.match(url or ""):
        host = urllib.parse.urlparse(url).netloc.lower()
        _RELAY_COOLDOWN[host] = time.time() + _RELAY_COOLDOWN_SECS


def _relay_fetch(url, headers=None, binary=False, timeout=30):
    """经 CF Worker /osfetch 代抓（2026-11-11 统帅 WAF 兜底）。要登录令牌；失败抛原异常。"""
    from cloud import CLOUD_HOST, _CLOUD_UA, _cloud_token
    tok = _cloud_token()
    if not tok:
        raise RuntimeError("无云端令牌，代抓不可用")
    relay = CLOUD_HOST + "/osfetch?u=" + urllib.parse.quote(url, safe="")
    req = urllib.request.Request(relay)
    req.add_header("X-Token", tok)
    req.add_header("User-Agent", _CLOUD_UA)
    with urlopen_ssl(req, timeout=timeout) as r:
        raw = r.read()
        return raw if binary else raw.decode("utf-8", errors="replace")


def fetch(url, extra_headers=None, binary=False, retries=1, timeout=15):
    headers = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9",
               "Accept-Encoding": "gzip, deflate"}
    if extra_headers:
        headers.update(extra_headers)
    last = None
    for i in range(retries + 1):
        # 2026-11-21：冷却期内先试 CF 代抓，成则直接回（省一记 403，也少给 WAF 送指纹）
        if _relay_hot(url):
            try:
                return _relay_fetch(url, headers, binary=binary, timeout=timeout)
            except Exception as _re:
                last = _re    # 代抓也不成才落回直连
        try:
            req = urllib.request.Request(url, headers=headers)
            with urlopen_ssl(req, timeout=timeout) as r:
                raw = r.read()
                enc = r.headers.get("Content-Encoding", "")
                if enc and "gzip" in enc:
                    raw = gzip.decompress(raw)
                elif enc and "deflate" in enc:
                    try:
                        raw = zlib.decompress(raw, -zlib.MAX_WBITS)
                    except zlib.error:
                        pass
                return raw if binary else raw.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            # 2026-11-11：统帅站 leader.com.cn 挂 WAF，本机非浏览器 TLS 指纹全 403。
            # 兜底走 CF Worker /osfetch 代抓（Cloudflare 出口指纹能过盾）。只对海尔系域名生效。
            if getattr(e, "code", 0) in (403, 404, 503) and _OS_HOST_RE.match(url or ""):
                _relay_mark(url)   # 2026-11-21：吃 403 就进冷却，后续请求直接代抓
                try:
                    return _relay_fetch(url, headers, binary=binary, timeout=timeout)
                except Exception:
                    pass
            last = e
            if i < retries:
                time.sleep(0.8)
        except Exception as e:
            # 2026-11-10：image.haier.com 对本机出口是 TCP RST（不是 403），网络层异常
            # 同样可能是 IP 封禁——海尔系域名一律试一次 CF 代抓再认输。
            if _OS_HOST_RE.match(url or ""):
                try:
                    return _relay_fetch(url, headers, binary=binary, timeout=timeout)
                except Exception:
                    pass
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


def extract_images(html, page_url=""):
    """官网图：域名白名单 + 去装饰 + www 死链域名自动换 image.* 救活（collect.py 经验）。

    2026-11-16 用户令「尺寸图都没抓取到」：leader 详情页的长图和尺寸线稿不在 <img src> 里，
    而是塞在 sd_data="[{&quot;src&quot;:&quot;RESOURCEDOMAINW020...jpg&quot;}]" 这种 JSON 属性中，
    占位符 RESOURCEDOMAIN 要换成 image 域名 + 页面所在目录。老逻辑只认 <img> → 这批图全漏。
    """
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
    # sd_data 等内联 JSON 里的图（leader 详情长图 / 尺寸线稿走这条）
    if page_url and "leader.com.cn" in page_url:
        _d = os.path.dirname(urllib.parse.urlparse(page_url).path).strip("/")
        _pfx = "https://image.leader.com.cn/" + (_d + "/" if _d else "")
        for m in re.finditer(r'sd_data="([^"]+)"', html):
            raw = _html_lib.unescape(m.group(1))
            for s in re.findall(r'"src"\s*:\s*"([^"]+)"', raw):
                s = s.strip()
                if not re.search(r'\.(jpg|jpeg|png|webp)$', s, re.I):
                    continue
                u = s.replace("RESOURCEDOMAIN", _pfx)
                if u not in seen:
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


_PAR_LOCAL = {"at": 0.0, "pars": None}

def _par_local():
    """云端参数库本地副本（par_cloud.json，cloud.par_pull_cloud 拉取落盘）。
    按 mtime 惰性重载；读不到/没同步过返回空 dict——文案回源爬官网，行为跟以前一样。"""
    try:
        p = wp("par_cloud.json")
        mt = os.path.getmtime(p)
        if _PAR_LOCAL["pars"] is None or mt != _PAR_LOCAL["at"]:
            with io.open(p, encoding="utf-8") as f:
                _PAR_LOCAL["pars"] = (json.load(f).get("pars") or {})
            _PAR_LOCAL["at"] = mt
    except Exception:
        if _PAR_LOCAL["pars"] is None:
            _PAR_LOCAL["pars"] = {}
    return _PAR_LOCAL["pars"]

def fetch_official(model, log, need_imgs=False):
    """官网三站顺序找；命中详情页→参数/卖点/产品名/价/图。没命中返回 None。

    2026-11-10 用户令「写文案是不是不需要再抓取数据就能写」：入口先查云端参数库
    （par_cloud.json），型号命中（≥3 条参数）直接组 data 返回——完全不打官网，
    秒级出参数、防反爬；参数库里没有 sellpoints/图，价格靠欣盛兜底（原逻辑不变）。

    need_imgs=True（2026-11-16 用户令「所有的都抓取放到 R2 里面」）：参数库命中也不短路，
    照样打官网拿图/官网直链/卖点；官网没抓到再退回参数库那份（参数照旧、图空），两不误。"""
    _kv = None
    try:
        _kv = _par_local().get(norm_model(model))
    except Exception:
        _kv = None
    if _kv and len(_kv) >= 3:
        if not need_imgs:
            log("参数库命中（%d 条参数），跳过官网抓取" % len(_kv))
            # 2026-11-10 深夜修：groups 必须 [[组名,[[k,v]...]]] 两层——之前直接给扁平
            # [[k,v]...]，_facts_text/build_desc 里 for a,b in kv 拿字符串拆就炸
            # （XQGL120-L732EH 实锤 not enough values to unpack (expected 2, got 1)）
            return {'source': '云端参数库', 'brand': '', 'pname': model, 'price': '',
                    'url': '', 'groups': [["参数", [[k, v] for k, v in _kv.items()]]],
                    'sellpoints': [], 'imgs': []}
        log("参数库命中（%d 条），但图库要图，继续打官网" % len(_kv))
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
        # 2026-11-19 型号自报闸门（BCD-335WLDPGU1 实锤）：串页详情页 title/pname 可能都过不了
        # 严检但参数表里白纸黑字写着别的型号（页面标 BCD-342WLLFDEDW9U1 ≠ 请求 BCD-335WLDPGU1），
        # 参数/卖点/图全是别家机器的。页面自己报的「型号」跟请求对不上 → 整页弃用换下一候选。
        _pagem = ''
        for _g, _kv in groups:
            for _k, _v in (_kv or []):
                _ks = str(_k).strip().lstrip('：: ')
                if _ks in ('型号', '产品型号', '商品型号') and str(_v).strip():
                    _pagem = str(_v).strip().lstrip('：: ')
                    break
            if _pagem:
                break
        if _pagem and norm_model(_pagem) != nm:
            log("官网[%s]型号对不上（页面标 %s ≠ %s），弃用换下一个" % (s['name'], _pagem[:30], model))
            continue
        if not _pagem and ('leader.com.cn' in url or 'haier.com' in url or 'casarte.com' in url):
            # 2026-11-20 补：官网通用页连「型号」行都没有（335WLLFDEDBXU1 实锤串 342 页），拒
            log("官网[%s]页面没标型号（通用页嫌疑），弃用换下一个" % s['name'])
            continue
        log("官网[%s]命中：%s / 参数 %d 组" % (s['name'], pname[:30], len(groups)))
        return {'source': '官网', 'brand': s['name'], 'pname': pname, 'price': price or '',
                'url': url, 'groups': [[g, kv] for g, kv in groups],
                'sellpoints': parse_sellpoints(html), 'imgs': extract_images(html, url)}
    if _kv and len(_kv) >= 3:
        log("官网没抓到可用详情页，退回参数库（%d 条参数、无图）" % len(_kv))
        return {'source': '云端参数库', 'brand': '', 'pname': model, 'price': '',
                'url': '', 'groups': [["参数", [[k, v] for k, v in _kv.items()]]],
                'sellpoints': [], 'imgs': []}
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
            row = con.execute("SELECT code,name,sale_price,album_urls,pid FROM products WHERE code=?",
                              (code,)).fetchone()
        if not row:
            for code, name, sp, al, pid in con.execute(
                    "SELECT code,name,sale_price,album_urls,pid FROM products "
                    "WHERE code IS NOT NULL AND code != ''"):
                toks = TOK_RE.findall((name or '').upper())
                if toks:
                    tn = norm_model(max(toks, key=len))
                    if tn and (tn == nm or tn in nm or nm.startswith(tn)):
                        row = (code, name, sp, al, pid)
                        break
        con.close()
    except Exception:
        return None
    if not row:
        return None
    code, name, sp, al, pid = row
    album = []
    try:
        album = [u for u in json.loads(al or "[]") if isinstance(u, str) and u.startswith("http")]
    except Exception:
        pass
    log("欣盛商城命中：%s" % (name or '')[:30])
    return {'source': '欣盛商城', 'brand': '', 'pname': name or model, 'price': str(sp or ''),
            'url': 'https://ys.longyangdq.cn/', 'groups': [], 'sellpoints': [], 'imgs': album,
            'pid': pid}   # pid 带出（2026-11-21：详情长图读参数要拿它请求 Products 接口）


# ---------------------------------------------------------------- 欣盛详情长图读参数
_XS_API_URL = ("https://ys.longyangdq.cn/client/Products/index?iShopId=&terminaltype=PC&token=&id=%s")
_XS_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/131.0.0.0 Safari/537.36")
# 参数表图通常在详情长图末尾（CWC8-B57GHU1 实锤：9 张里倒数第 2 张是「产品参数」表）——
# 从后往前问，命中即停，最多看 4 张省视觉调用。
_XS_IMG_PROBE_MAX = 4
_XS_PARAM_PROMPT = (
    "这是家电产品详情长图。请判断它是不是「规格/产品参数表」（含电压/功率/容量/尺寸等"
    "两列或多列键值表格）。只输出纯JSON不要解释："
    '{"is_params": true或false, "rows": [["参数名","参数值"], ...]}。'
    "是参数表时 rows 逐行完整读出所有键值；不是时 is_params=false、rows=[]。")


# ---------------------------------------------------------------- 海尔电子说明书站参数
# 2026-11-21 用户令「参数也可以去 hzy.haier.com/cn/vipcode/eleInstruction 找」：
# 链路 型号 → getProductBrandNameByProductCode（拿平台码 productCode/propertyExist）
#      → getProDescription（拿说明书 H5 页 URL，washerh5.haier.net）
#      → 同目录 prod.json「技术参数介绍」table_heng_2lie 结构化表（纯接口、秒级、零视觉）。
# 实锤：hzy.haier.com/washerh5.haier.net 都没被 WAF 封（被封的是 www.casarte/www.haier 主站）；
# CWC8-B57GHU1 13 行参数与欣盛详情图 8 条完全吻合。
_HZY_TABLE_CODES = {
    "rated_voltage": "额定电压",
    "rated_frequency": "额定频率",
    "rated_power": "额定输入功率",
    "water_pressure": "水压",
    "tableware_set_num": "餐具装载容量",
    "water_efficiency_level": "水效等级",
    "installation_method": "安装方式",
    "overall_width_depth_height": "整机外形尺寸(宽*深*高mm)",
    "openpore_width_depth_height": "最小开孔尺寸(宽*深*高mm)",
    "countertop_width_depth": "台面尺寸(宽*深mm)",
    "without_countertop_width_depth_height": "无台面尺寸(宽*深*高mm)",
    "door_body_occupancy_width_mm": "门体占用宽度(mm)",
}


# ---------------------------------------------------------------- 电子说明书站全量抓（宝窗口新链第二层）
# 2026-11-23 用户令「宝窗口找图跟数据全改：优先官网，官网没有的直接去说明书站，
# 把型号所有图片跟参数、使用说明下载下来再扔给 AI 写文案」：
#   ① rows      「技术数据/技术规格」节点 tr 表格参数
#   ② imgs      全树 SMO 页内 image/*.png|jpg（带节点名；装饰 artwork/ 与 svg/gif 排除）
#   ③ size_imgs 「尺寸/安装要求」节点页内图（尺寸线稿）
#   ④ manual    使用说明文字（安全/安装/保养…，给 AI 当真料）
# 实测（2026-11-23）：C1 HD10L5CLU1（pcode CEAAH9004_C）正面 3885×3829 透明底 PNG、
# LES50H-AC5（GA0SZR00P_A）外观图 3524×2215，全部原图尺寸可直进图池。
_BOOK_PARAM_NAMES = ("技术数据", "技术规格", "技术参数", "规格参数", "产品规格", "产品参数")
_BOOK_MANUAL_NAMES = ("安全注意", "注意事", "警告", "使用方法", "安装方法", "清洗保养",
                      "维护", "产品特色", "功能介绍", "洗涤注意", "使用方面", "疑难解答")
_BOOK_ROW_HEADS = ("项目", "内容", "项目名称", "参数名称", "名称", "条目")
_BOOK_TEXT_MAX = 24


def _book_find_entry(_post, model, nm, log):
    """型号 → 说明书站产品条目（防串机 + 模糊重试）。
    2026-11-23 用户令「说明书你搜索型号我不管是什么格式的全部下载下来」：
    站内机型登记常带空格（C8 U12G3 实锤，整码搜索 total=0，站内搜索是子串匹配
    『U12G3』才命中）；且收录标记不可信（CEABA2017 manualExist=0 但 PDF 真实存在，
    getProDescription 才是唯一的真话）——整码搜不到就逐字裁前缀重试，
    按归一化型号比对选回正确那台。命中返回 entry dict，没命中返回 {}。"""
    try:
        hits = _post("product/getProductBrandNameByProductCode",
                     {"productCode": model}).get("data") or []
    except Exception:
        hits = []
    e = next((x for x in hits
              if norm_model(x.get("productModel") or "") == nm), {})
    if not e:
        q = str(model)
        for _ in range(4):
            q = q[1:]
            if len(q) < 4:
                break
            try:
                hits = _post("product/getProductBrandNameByProductCode",
                             {"productCode": q}).get("data") or []
            except Exception:
                hits = []
            e = next((x for x in hits
                      if norm_model(x.get("productModel") or "") == nm), {})
            if e:
                log("说明书站模糊命中：%s（码 %s）"
                    % (e.get("productModel"), e.get("productCode")))
                break
    return e or {}


def _book_pdf(model, url, pcode, log):
    """PDF 整本说明书（2026-11-23 用户令「不管什么格式的全部下载下来」）：
    下载存 kitset/<型号>/说明书/电子说明书.pdf（下过不再重下，限速礼貌抓），
    PyMuPDF 解析——参数页抠「名称 值」行，内嵌位图全导出，含「尺寸」+毫米数页
    整页 2x 渲染当尺寸线稿（矢量线稿抠不出位图只能整页渲）。"""
    try:
        import kitset as _ks
        mdir = os.path.join(_ks.model_dir(model), "说明书")
    except Exception:
        mdir = os.path.join("kitset", norm_model(model), "说明书")
    os.makedirs(mdir, exist_ok=True)
    pdf_fp = os.path.join(mdir, "电子说明书.pdf")
    if not os.path.isfile(pdf_fp):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": _XS_UA, "Referer": "https://hzy.haier.com/"})
            with urlopen_ssl(req, timeout=60) as r:
                raw = r.read()
            if len(raw) < 3000 or b"%PDF" not in raw[:300]:
                log("说明书 PDF 下载异常（%d 字节），放弃" % len(raw))
                return {}
            with io.open(pdf_fp, "wb") as f:
                f.write(raw)
            log("说明书 PDF 已下载（%.1f MB）→ %s" % (len(raw) / 1048576.0, pdf_fp))
        except Exception as ex:
            log("说明书 PDF 下载失败：%s" % str(ex)[:80])
            return {}
    try:
        import pymupdf as fitz  # PyMuPDF 新名（fitz 旧名已弃用）
    except Exception:
        try:
            import fitz
        except Exception:
            log("说明书 PDF 已存盘但缺解析库（requirements 里 PyMuPDF），参数图先不解析")
            return {}
    try:
        doc = fitz.open(pdf_fp)
    except Exception as ex:
        log("说明书 PDF 打不开：%s" % str(ex)[:80])
        return {}
    rows, manual, imgs, size_imgs, dts = [], [], [], [], []
    seen_r = set()
    # 先探有没有文字层：扫描版 PDF（C8U12G3 实锤 32 页 14 字符）没有文本，
    # 只能整页渲染当图——参数靠现有视觉链认，机图/尺寸线稿靠 _pick_prod 启发式+AI
    _tot_txt = 0
    try:
        for pi in range(doc.page_count):
            _tot_txt += len((doc.load_page(pi).get_text() or "").strip())
    except Exception:
        pass
    _scanned = _tot_txt < 200
    if _scanned:
        log("说明书 PDF 是扫描版（全 32 页无文字层），整页渲染 %d 张图进图池" % doc.page_count)
    try:
        for pi in range(doc.page_count):
            if _scanned:
                try:
                    pix = doc.load_page(pi).get_pixmap(matrix=fitz.Matrix(2, 2))
                    fp = os.path.join(mdir, "pdf_p%02d.png" % (pi + 1))
                    pix.save(fp)
                    imgs.append(("说明书PDF 第%d页" % (pi + 1), fp))
                    pix = None
                except Exception:
                    pass
                continue
            page = doc.load_page(pi)
            # 内嵌位图全导出（小图/装饰在 _pick_prod 有 250px 尺寸闸，不用这层挑）
            try:
                for im in page.get_images(full=True):
                    try:
                        pix = fitz.Pixmap(doc, im[0])
                        if pix.width < 300 or pix.height < 300:
                            pix = None
                            continue
                        if pix.colorspace and pix.colorspace.n > 3:
                            pix = fitz.Pixmap(fitz.csRGB, pix)
                        fp = os.path.join(mdir, "pdf_p%d_%d.png" % (pi + 1, im[0]))
                        pix.save(fp)
                        imgs.append(("说明书PDF 第%d页" % (pi + 1), fp))
                        pix = None
                    except Exception:
                        continue
            except Exception:
                pass
            try:
                txt = page.get_text() or ""
            except Exception:
                txt = ""
            if not txt:
                continue
            # 参数行：「名称 值」两段式（中文/字母名称 2~14 字），只在参数页抠防噪音
            if any(w in txt for w in _BOOK_PARAM_NAMES) or "规格" in txt:
                for ln in txt.splitlines():
                    t = " ".join(ln.split())
                    m = re.match(r"^([\u4e00-\u9fa5A-Za-z／/（）()·\*x×]{2,14}?)\s+(.{1,40})$", t)
                    if not m:
                        continue
                    k, v = m.group(1).strip(), m.group(2).strip()
                    if k in seen_r or k in _BOOK_ROW_HEADS or not v \
                            or re.fullmatch(r"\d+", v):
                        continue
                    seen_r.add(k)
                    rows.append((k, v))
            # 说明要点（给 AI 当真料）
            if any(w in txt for w in _BOOK_MANUAL_NAMES):
                for ln in txt.splitlines():
                    t = " ".join(ln.split())
                    if 8 <= len(t) <= 100 \
                            and not t.lower().startswith(("http", "www")) \
                            and t not in manual:
                        manual.append(t)
                    if len(manual) >= _BOOK_TEXT_MAX:
                        break
            # 尺寸页：整页渲染当尺寸线稿（同页 a=825mm 式数值抠成 dim_table，图注合成用）
            _mm = re.findall(r"([a-dA-D])\s*[（(]?\s*mm\s*[）)]?\s*[=：:]?\s*(\d{3,4})", txt)
            if "尺寸" in txt:
                try:
                    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
                    fp = os.path.join(mdir, "pdf_size_p%d.png" % (pi + 1))
                    pix.save(fp)
                    imgs.append(("说明书PDF 尺寸图 第%d页" % (pi + 1), fp))
                    size_imgs.append(("说明书PDF 尺寸图 第%d页" % (pi + 1), fp))
                except Exception:
                    pass
                if len(_mm) >= 2:
                    dts.append({"node": "说明书PDF 第%d页" % (pi + 1),
                                "labels": [a.upper() for a, _v in _mm],
                                "vals": {model: [int(v) for _a, v in _mm]}})
                    log("说明书 PDF 尺寸页数值：%s"
                        % "；".join("%s=%s" % (a, v) for a, v in _mm))
    finally:
        doc.close()
    log("说明书 PDF 解析：参数 %d 条 / 图 %d 张（尺寸页 %d）/ 说明要点 %d 条"
        % (len(rows), len(imgs), len(size_imgs), len(manual)))
    return {"rows": rows, "imgs": imgs, "size_imgs": size_imgs,
            "manual": manual, "dim_table": dts, "url": url, "pcode": pcode,
            "brand": "", "root": "电子说明书PDF", "scanned": _scanned}


def _book_fetch(model, log):
    """电子说明书站全量抓。防串机：树根 name 必须含请求型号（norm 归一比对）。
    命中返回 dict（见上注释），没命中/失败返回 {}。"""
    nm = norm_model(model)
    hdrs = {"User-Agent": _XS_UA,
            "Referer": "https://hzy.haier.com/cn/vipcode/eleInstruction"}

    def _post(path, data):
        u = ("https://hzy.haier.com/vipCode/%s?vipCode_domainName=&vipCode_ssotoken="
             "&vipCode_userId=" % path)
        req = urllib.request.Request(u, data=urllib.parse.urlencode(data).encode(),
                                     headers={**hdrs,
                                              "Content-Type": "application/x-www-form-urlencoded",
                                              "X-Requested-With": "XMLHttpRequest"})
        with urlopen_ssl(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    # 1. 型号 → 产品码（防串机 + 模糊重试，_book_find_entry 里有 11-23 实锤注释）
    try:
        e = _book_find_entry(_post, model, nm, log)
        if not e:
            log("说明书站没收录该型号（或对不上），跳过")
            return {}
        pcode = str(e.get("productCode") or "")
        if not pcode:
            return {}
    except Exception as ex:
        log("说明书站查询失败：%s" % str(ex)[:80])
        return {}
    # 2. 产品码 → 说明书地址（type=1 H5 电子书 / type=2 PDF 整本，11-23 用户令
    # 「不管什么格式的全部下载下来」——PDF 走 _book_pdf 下载整本+解析）
    try:
        d2 = _post("product/getProDescription", {"code": pcode})
        url = str(d2.get("resultMsg") or "")
        _btype = str(d2.get("type") or "")
        if "http" not in url:
            return {}
    except Exception as ex:
        log("说明书 H5 页查询失败：%s" % str(ex)[:80])
        return {}
    if _btype == "2" or url.lower().split("?")[0].endswith(".pdf"):
        return _book_pdf(model, url, pcode, log)
    # 3. SUMEBookMap 树
    pr = urllib.parse.urlparse(url)
    segs = [s for s in pr.path.split("/") if s]
    ind = ""
    brand_seg = ""
    for i, s in enumerate(segs):
        if s == "app" and i + 2 < len(segs) and segs[i + 1] in ("casarte", "haier", "leader"):
            brand_seg = segs[i + 1]     # /app/{品牌}/{行业码}/…
            ind = segs[i + 2]
            break
    pc = (urllib.parse.parse_qs(pr.query).get("productCode") or [""])[0]
    if not ind or not pc:
        log("说明书页不是 SUMEBookMap 形态（%s），跳过" % pr.netloc)
        return {}
    base = "%s://%s/app/api/%s/casarteSUMEBookMap/%s/static/html-casarte/" % (
        pr.scheme, pr.netloc, ind, urllib.parse.quote(pc))
    try:
        req = urllib.request.Request(base + "casarteSUMEBookMap.json", headers=hdrs)
        with urlopen_ssl(req, timeout=20) as r:
            tree = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as ex:
        log("说明书树拉取失败：%s" % str(ex)[:80])
        return {}
    # 2026-11-23 放宽：根标常带颜色/卖点前缀（星蕴银拉丝BCD-400WLLTDEDS9U1 实锤被
    # 全等比对误拒、整链失效）——改为根标 norm 后【包含】请求型号即放行；
    # 必须含完整型号才过，根标是别的型号时依然拦得住，防串机不放松。
    _root_nm = norm_model(str(tree.get("name") or "").replace("说明书", "").replace("使用说明书", ""))
    if not (_root_nm and nm in _root_nm):
        log("说明书型号对不上（根标 %s 不含 %s），弃用" % (tree.get("name"), model))
        return {}
    # 全树收节点（递归所有键——各品牌树键名不统一，probe 实锤 WAS/WAT 两形态都能收全）
    nodes = []

    def _walk(node):
        if isinstance(node, dict):
            h = str(node.get("href") or "").strip()
            n = str(node.get("name") or "").strip()
            if h and n and h.lower().endswith(".html"):
                nodes.append((n, h))
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    _walk(tree)
    nodes = nodes[:80]
    # 并发拉 SMO 页（67 页顺序拉 ~20s，8 并发 ~3s）
    pages = {}
    _hrefs = [h for _n, h in nodes]

    def _get(h):
        u = h if h.startswith("http") else base + h.lstrip("./")
        try:
            req = urllib.request.Request(u, headers=hdrs)
            with urlopen_ssl(req, timeout=15) as r:
                return r.read().decode("utf-8", "replace")
        except Exception:
            return ""

    try:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=8) as ex:
            for (n, h), html in zip(nodes, ex.map(_get, _hrefs)):
                pages[h] = html
    except Exception:
        for n, h in nodes:
            pages[h] = _get(h)
    # 逐页解析：参数行 / 图 / 说明文字 / 尺寸数值表
    imgs, size_imgs, rows, manual, dim_tables = [], [], [], [], []
    seen_i, seen_r = set(), set()
    for n, h in nodes:
        html = pages.get(h) or ""
        if not html:
            continue
        if any(w in n for w in _BOOK_PARAM_NAMES):
            for tr in re.findall(r"<tr[\s\S]*?</tr>", html, re.I):
                cells = [re.sub(r"<[^>]+>", "", c).strip()
                         for c in re.findall(r"<t[dh][^>]*>([\s\S]*?)</t[dh]>", tr, re.I)]
                cells = [c for c in cells if c]
                if len(cells) >= 2 and cells[0] not in seen_r \
                        and cells[0] not in _BOOK_ROW_HEADS:
                    seen_r.add(cells[0])
                    rows.append((cells[0], cells[1]))
        # 尺寸数值表（2026-11-23 LES50H-AC5 实锤：说明书线稿只标字母 a1/b1/d1，
        # 毫米数在同页表格里「型号 a（mm） b（mm）…」——图上没数字，_nums_gate 必挂，
        # 表格抠出来跟线稿合成才有真尺寸图）。表头行=「型号」开头且带「字母（mm）」列。
        _trs = [([re.sub(r"<[^>]+>", "", c).strip()
                  for c in re.findall(r"<t[dh][^>]*>([\s\S]*?)</t[dh]>", tr, re.I)])
                for tr in re.findall(r"<tr[\s\S]*?</tr>", html, re.I)]
        _trs = [[c for c in r if c] for r in _trs]
        _i = 0
        while _i < len(_trs):
            r0 = _trs[_i]
            if len(r0) >= 2 and "型号" in r0[0] \
                    and sum(1 for c in r0[1:] if re.fullmatch(r"[a-zA-Z]{1,3}\d?（mm）", c)) >= max(1, len(r0) - 2):
                labels = [re.sub(r"（mm）$", "", c) for c in r0[1:]]
                _i += 1
                vals = {}
                while _i < len(_trs):
                    r1 = _trs[_i]
                    if len(r1) == len(r0) and re.match(r"^[A-Za-z][A-Za-z0-9\- ]{3,}$", r1[0]) \
                            and all(re.fullmatch(r"\d+(\.\d+)?", c) for c in r1[1:]):
                        vals[r1[0]] = [float(v) if "." in v else int(v) for v in r1[1:]]
                        _i += 1
                    else:
                        break
                if vals:
                    dim_tables.append({"node": n, "labels": labels, "vals": vals})
                    log("说明书尺寸数值表（%s）：%s" % (n, "；".join(
                        "%s=%s" % (m, "/".join(str(x) for x in v)) for m, v in vals.items())))
                continue
            _i += 1
        for src in re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', html, re.I):
            sl = src.lower()
            if "artwork/" in sl or sl.endswith((".svg", ".gif")):
                continue
            u2 = urllib.parse.urljoin(base, src)
            if not re.search(r"\.(png|jpe?g|webp)(\?|$)", u2, re.I) or u2 in seen_i:
                continue
            seen_i.add(u2)
            if any(w in n for w in ("尺寸", "安装要求", "安装方法")):
                size_imgs.append((n, u2))
            imgs.append((n, u2))
        if any(w in n for w in _BOOK_MANUAL_NAMES):
            body = re.sub(r"<(script|style)[\s\S]*?</\1>", "", html, flags=re.I)
            cnt = 0
            for m in re.findall(r"<(?:li|p)[^>]*>([\s\S]*?)</(?:li|p)>", body, re.I):
                t = re.sub(r"<[^>]+>", "", m)
                t = re.sub(r"\s+", " ", t).strip()
                if 6 <= len(t) <= 100 and not t.lower().startswith(("http", "www")) \
                        and t not in manual:
                    manual.append(t)
                    cnt += 1
                if cnt >= 8 or len(manual) >= _BOOK_TEXT_MAX:
                    break
    brand = {"casarte": "卡萨帝", "leader": "统帅", "haier": "海尔"}.get(brand_seg, "")
    # 图排序（C1 HD10L5CLU1 全树 192 张实锤）：机器形态/尺寸类节点排前，
    # 程序表/注意事项/APP 等操作截图垫后——视觉选图先吃高价值图，落盘编号也在前。
    _PRI = ("正面", "背面", "外观", "部件", "附件", "尺寸", "安装要求", "安装方法",
            "控制面板", "电气原理", "产品介绍", "品牌介绍")

    def _prio(it):
        n = it[0]
        return 0 if any(w in n for w in _PRI) else 1

    imgs.sort(key=_prio)
    size_imgs.sort(key=_prio)
    # 数值表所在节点的图就是带字母标注的结构线稿（LES50H「外观及部件介绍」实锤：
    # 节点名不含「尺寸/安装」进不了 size_imgs，合成图注会配到挂板安装图上）——
    # 插到队首（数值表跟线稿同源，字母对得上；稳定排序会把它排到第 5 位取不到）
    _dt_nodes = {t.get("node") for t in dim_tables}
    if _dt_nodes:
        _dt_imgs = [(_n, _u) for _n, _u in imgs
                    if _n in _dt_nodes and (_n, _u) not in size_imgs]
        size_imgs[:0] = _dt_imgs
        size_imgs.sort(key=_prio)
    # 参数防串机（官方一本多型号共用参数页实锤：LES50H/LES60H 同一本手册，
    # 参数页只标 LES50H-AC5 → 60L 机型照抄会写出 50L 错参数，CWC8 教训照守）。
    # 2026-11-23 用户令「LES60H 这种就是两个容量而已」：同系列变体放行——
    # 剥掉数字后的字母骨架一致（LES-H-AC 同）= 同平台双容量 → 剥掉型号行与容量行，
    # 其余通用参数（功率/能效/水压/温度…）保留；骨架不一致 → 整组弃用。
    _pm = dict(rows).get("产品型号") or dict(rows).get("产品系列") or ""

    def _skel(s):
        return re.sub(r"[0-9.\-]", "", norm_model(s) or "")

    if _pm and norm_model(_pm) != nm:
        if _skel(_pm) and _skel(_pm) == _skel(model):
            _n0 = len(rows)
            rows = [(k, v) for k, v in rows
                    if str(k) not in ("产品型号", "产品系列") and "容量" not in str(k)]
            log("说明书参数页标 %s ≠ 请求 %s：同系列变体（就是容量差），剥型号/容量行放行 %d 条通用参数"
                % (_pm, model, len(rows)))
        else:
            log("说明书参数页标 %s ≠ 请求 %s（非同系列），参数弃用防串机" % (_pm, model))
            rows = []
    log("说明书全量：参数 %d 条 / 图 %d 张（尺寸线稿 %d）/ 说明要点 %d 条"
        % (len(rows), len(imgs), len(size_imgs), len(manual)))
    return {"rows": rows, "imgs": imgs, "size_imgs": size_imgs,
            "manual": manual, "dim_table": dim_tables, "url": url, "pcode": pcode,
            "brand": brand, "root": str(tree.get("name") or "")}


def _hzy_params(model, log):
    """海尔电子说明书站参数。成功返回 [(中文名, 值)...]，没命中/失败返回 []。
    product_model 行只用来防串机（平台码对应型号≠请求型号 → 整包弃用），不进参数。"""
    nm = norm_model(model)
    hdrs = {"User-Agent": _XS_UA, "Content-Type": "application/x-www-form-urlencoded",
            "Referer": "https://hzy.haier.com/cn/vipcode/eleInstruction",
            "X-Requested-With": "XMLHttpRequest"}

    def _post(path, data):
        u = ("https://hzy.haier.com/vipCode/%s?vipCode_domainName=&vipCode_ssotoken="
             "&vipCode_userId=" % path)
        req = urllib.request.Request(u, data=urllib.parse.urlencode(data).encode(),
                                     headers=hdrs)
        with urlopen_ssl(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    # 1. 型号 → 产品码（productModel 必须等于请求型号，防串机；模糊重试同 _book_fetch）
    try:
        e = _book_find_entry(_post, model, nm, log)
        if not e:
            log("电子说明书没收录该型号（或对不上），跳过")
            return []
        pcode = str(e.get("productCode") or "")
        if not pcode:
            return []
    except Exception as ex:
        log("电子说明书查询失败：%s" % str(ex)[:80])
        return []
    # 2. 平台码 → 说明书 H5 页 → 同目录 prod.json；拉不到 JSON（返回 SPA 兜底页）
    # 时走 SUMEBookMap 接口链（2026-11-22 CWC16-B07BK 实锤：WAS 型说明书数据在
    # /app/api/{行业码}/casarteSUMEBookMap/{产品码}/static/html-casarte/ 下，
    # 树里「技术参数」节点 href 指向 SMO*.html，tr 表格 8 条真参数）。
    url = ""
    try:
        d2 = _post("product/getProDescription", {"code": pcode})
        url = str(d2.get("resultMsg") or "")
        if "http" not in url:
            return []
    except Exception as ex:
        log("电子说明书 H5 页查询失败：%s" % str(ex)[:80])
        return []

    def _sumebook_rows(u):
        """SUMEBookMap 型电子说明书：树找「技术参数」→ SMO*.html → tr 表格行。"""
        try:
            pr = urllib.parse.urlparse(u)
            segs = [s for s in pr.path.split("/") if s]
            ind = ""
            for i, s in enumerate(segs):
                if s == "app" and i + 2 < len(segs) and segs[i + 1] in ("casarte", "haier", "leader"):
                    ind = segs[i + 2]
            pc = (urllib.parse.parse_qs(pr.query).get("productCode") or [""])[0]
            if not ind or not pc:
                return []
            base = "%s://%s/app/api/%s/casarteSUMEBookMap/%s/static/html-casarte/" % (
                pr.scheme, pr.netloc, ind, urllib.parse.quote(pc))
            req = urllib.request.Request(base + "casarteSUMEBookMap.json",
                                         headers={"User-Agent": _XS_UA, "Referer": u})
            with urlopen_ssl(req, timeout=20) as r:
                tree = json.loads(r.read().decode("utf-8", "replace"))
            # 防串机：根 name 必须含请求型号（2026-11-23 放宽为包含比对——根标带
            # 颜色/卖点前缀时全等比对误拒，星蕴银拉丝BCD-400WLLTDEDS9U1 实锤）
            _root_nm = norm_model(str(tree.get("name") or ""))
            if not (_root_nm and nm in _root_nm):
                log("SUMEBookMap 型号对不上（根标 %s 不含 %s），弃用" % (tree.get("name"), model))
                return []
            targets = []

            def _walk2(node):
                if str(node.get("name") or "").strip() in ("技术参数", "规格参数", "产品规格"):
                    h = str(node.get("href") or "").strip()
                    if h:
                        targets.append(h)
                for k in ("casarteSUMEBookMap", "PartGroup", "PartNode"):
                    v = node.get(k)
                    if isinstance(v, list):
                        for c in v:
                            _walk2(c)
                    elif isinstance(v, dict):
                        _walk2(v)

            _walk2(tree)
            rows2, seen2 = [], set()
            for h in targets[:3]:
                try:
                    req2 = urllib.request.Request(
                        base + h.lstrip("./") if not h.startswith("http") else h,
                        headers={"User-Agent": _XS_UA, "Referer": u})
                    with urlopen_ssl(req2, timeout=20) as r2:
                        html2 = r2.read().decode("utf-8", "replace")
                except Exception:
                    continue
                for tr in re.findall(r"<tr[\s\S]*?</tr>", html2, re.I):
                    cells = [re.sub(r"<[^>]+>", "", c).strip()
                             for c in re.findall(r"<t[dh][^>]*>([\s\S]*?)</t[dh]>", tr, re.I)]
                    cells = [c for c in cells if c]
                    if len(cells) >= 2 and cells[0] not in seen2:
                        seen2.add(cells[0])
                        rows2.append((cells[0], cells[1]))
            if rows2 and log:
                log("SUMEBookMap 技术参数页命中 %d 条" % len(rows2))
            return rows2
        except Exception as ex:
            log("SUMEBookMap 链失败：%s" % str(ex)[:80])
            return []

    try:
        purl = url.split("?")[0].rsplit("/", 1)[0] + "/prod.json"
        req = urllib.request.Request(purl, headers={"User-Agent": _XS_UA,
                                                    "Referer": url})
        with urlopen_ssl(req, timeout=20) as r:
            raw = r.read().decode("utf-8", "replace")
        if raw.lstrip().startswith(("{", "[")):
            prod = json.loads(raw)
        else:
            prod = None
            if log:
                log("prod.json 是 SPA 兜底页，走 SUMEBookMap 接口链")
    except Exception as ex:
        prod = None
        log("prod.json 拉取失败：%s" % str(ex)[:80])
    if prod is None:
        _srows = _sumebook_rows(url)
        if not _srows:
            return []
        rows = list(_srows)
        pm = ""     # 根 name 已在 _sumebook_rows 里防串机
        out = []
        _CODE2CN = dict(_HZY_TABLE_CODES) if _HZY_TABLE_CODES else {}
        for k, v in rows:
            out.append((k, v))
        return out
    # 3. 递归找「技术参数介绍」表格节点
    rows = []

    def _collect(t):
        for r in (t.get("material_text") or []):
            if not isinstance(r, dict):
                continue
            code = str(r.get("code") or "")
            val = r.get("value")
            txt = ""
            if isinstance(val, dict):
                txt = str(val.get("material_text") or "")
                if not txt:
                    for c in (val.get("content") or [] if isinstance(val.get("content"), list) else []):
                        if isinstance(c, dict) and c.get("material_text"):
                            txt = str(c["material_text"])
                            break
            elif val is not None:
                txt = str(val)
            txt = txt.strip()
            if code and txt:
                rows.append((code, txt))

    def _walk(node):
        if isinstance(node, dict):
            if str(node.get("material_type") or "").startswith("table") \
                    and node.get("material_lable") == "技术参数介绍":
                _collect(node)
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    _walk(prod)
    # 防串机：product_model 行必须等于请求型号
    pm = dict(rows).get("product_model") or ""
    if pm and norm_model(pm) != nm:
        log("电子说明书型号对不上（prod.json 标 %s ≠ %s），弃用" % (pm[:30], model))
        return []
    out = []
    for code, txt in rows:
        if code == "product_model":
            continue
        cn = _HZY_TABLE_CODES.get(code)
        if cn and txt:
            out.append((cn, txt))
    if out:
        log("电子说明书命中参数 %d 条（平台码 %s）" % (len(out), pcode))
    return out


def _xs_api_via_browser(pid, log):
    """Playwright 拉欣盛商品接口 JSON（2026-11-21 实锤：阿里云盾只认真浏览器 TLS，
    纯 urllib/curl/CF 中继全 404；页面里框架带 XSRF 请求才 200）。没装 playwright
    /chromium 或超时返回 None（静默降级，不挡主链）。"""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        log("playwright 未安装，欣盛详情参数跳过")
        return None
    data = None
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True, args=["--no-proxy-server"])
            pg = b.new_context(user_agent=_XS_UA).new_page()
            got = {}

            def _on_resp(r):
                if "Products/index" in r.url:
                    try:
                        got["d"] = json.loads(r.text())
                    except Exception:
                        pass

            pg.on("response", _on_resp)
            pg.goto("https://ys.longyangdq.cn/detail?goods_id=%s" % pid,
                    wait_until="domcontentloaded", timeout=45000)
            pg.wait_for_timeout(5000)
            b.close()
        data = ((got.get("d") or {}).get("data") or {})
    except Exception as e:
        log("欣盛接口浏览器拉取失败：%s" % str(e)[:80])
        return None
    return data or None


def _xs_img_params(model, xs, log):
    """欣盛详情长图读参数（2026-11-21 用户令「读取欣盛图片的参数」）：
    Products 接口 oProductSub.mobileDescription 里的详情长图通常带「产品参数」表，
    视觉链（Qwen→GLM-4V→商汤）读出来落 groups。结构化 lsProductParameters 常为空
    （CWC8-B57GHU1 实锤），图片是唯一参数源。成功返回 [(k,v)...]，失败返回 []。"""
    pid = (xs or {}).get("pid") if isinstance(xs, dict) else None
    if not pid:
        return []
    data = _xs_api_via_browser(pid, log)
    if not data:
        return []
    sub = data.get("oProductSub") or {}
    md = str(sub.get("mobileDescription") or "")
    urls = [u for u in re.findall(r'src="([^"]+)"', md) if u.startswith("http")]
    if not urls:
        log("欣盛详情长图 0 张，图片读参跳过")
        return []
    log("欣盛详情长图 %d 张，开始视觉找参数表（从后往前最多 %d 张）"
        % (len(urls), _XS_IMG_PROBE_MAX))
    import base64 as _b64
    rows = []
    probed = 0
    for u in reversed(urls):
        if probed >= _XS_IMG_PROBE_MAX:
            break
        probed += 1
        try:
            req = urllib.request.Request(u, headers={"User-Agent": _XS_UA,
                                                     "Referer": "https://ys.longyangdq.cn/"})
            raw = urlopen_ssl(req, timeout=25).read()
        except Exception as e:
            log("详情图下载失败：%s" % str(e)[:60])
            continue
        du = "data:image/jpeg;base64," + _b64.b64encode(raw).decode("ascii")
        try:
            txt = _vision_call_auto(du, _XS_PARAM_PROMPT, log)
        except Exception as e:
            log("视觉读图失败：%s" % str(e)[:80])
            continue
        if not txt:
            continue
        try:
            j = json.loads(re.search(r"\{.*\}", txt, re.S).group(0))
        except Exception:
            continue
        if not j.get("is_params"):
            continue
        for kv in (j.get("rows") or []):
            if isinstance(kv, (list, tuple)) and len(kv) >= 2:
                k, v = str(kv[0]).strip(), str(kv[1]).strip()
                if k and v and len(k) <= 20:
                    rows.append((k, v))
        if rows:
            log("欣盛详情图读出参数 %d 条（第 %d 张）" % (len(rows), len(urls) - probed + 1))
            break
    return rows


def _vision_call_auto(data_url, prompt, log):
    """视觉链统一入口：优先 kitset._vision_key（Mac 零配置兜底），失败再裸调。"""
    try:
        from pei_core import _vision_call as _vc
    except Exception:
        return None
    key = None
    try:
        import kitset as _ks
        key = _ks._vision_key()
    except Exception:
        key = None
    return _vc(key, data_url, prompt, log=log)


# ---------------------------------------------------------------- GLM 文案
def _chat(prompt):
    k = _api_key()
    if not k:
        raise RuntimeError("没拿到 GLM key（云端没配 / 没登录 / 本机无 glm_key.txt）")
    body = json.dumps({"model": GLM_MODEL, "messages": [{"role": "user", "content": prompt}],
                       "temperature": 0.7, "thinking": THINK_OFF}).encode("utf-8")
    req = urllib.request.Request(API_URL, data=body, headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + k})
    with urlopen_ssl(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))["choices"][0]["message"]["content"]


PROMPT = """你是闲鱼家电卖家文案写手。产品：{pname}（型号 {model}）。

【铁律】
1) 只能使用【真实数据】里出现的参数、功能和数字，绝对禁止编造数据里没有的参数、功能或数字。{no_params}
2) titles：闲鱼标题三条（三选一）。分段式、段与段之间一个空格；不带价格、不带emoji、不带#话题；每条≤30字；三条开头语序必须各不同；
   品类/能源词必须照【真实数据】参数写（有「加热方式：加热管/储水式/横式」就是电热水器，绝不许写「燃气」；燃气热水器参数必有「燃气类/气源」字样）——
   每条都必须有：型号全码 + 成色词（正品写「全新机」，样机写「展示样机」）+ 服务词（全国联保/原厂正品联保）。参考版式（品牌/容量/核心卖点/品类词照【真实数据】换）：
   {brand} 226L风冷无霜冰箱 {model} 全新机 家用三门 全国联保
   全新机 {brand} 226L 三门冰箱 风冷无霜 {model} 原厂正品联保
   {brand}三门冰箱 226L 风冷无霜 {model} 全新机 家用节能 全国联保
   {kk_titles}
3) points：正好5-8条卖点（聚光用）。【真实数据】是你唯一的事实来源：每条必须锚定至少一个真实参数（数字或参数名/参数值），数字一个都不许编、参数表里没有的维度一个字都不许写，每条≤36字。
   写法（2026-11-20 用户令「把参数塞给你、全部按参数来写」，实锤「周末采购一周食材」）：每条卖点就是一条参数事实的行家表述——把【真实数据】参数表里的参数逐条写成卖点（参数名+参数值进句子），语序/措辞每条换着来、别一个模子；但只许写参数本身和它对应的机器事实，绝不许写生活场景、使用画面、心理感受和效果承诺（「周末采购一周食材」「装满不拥挤」「百搭又耐看」「摆哪都显质感」这类一句都不许出现）。
   依然禁止：编造【真实数据】里没有的数字或功能；把尺寸/外形/净重当卖点；极限词；括号标注参数名。
   差异化硬要求（2026-11-14 用户令「卖点全是大同小异的要AI干嘛」）：先从【真实数据】里找出这台机器跟同品类其他型号**最不一样**的
   2-3 个参数（容量档位、功率、能效等级、核心技术、特色功能、适用场景数字），把它们写成前 2-3 条；
   所有同品类机型都一样的基础参数（如普通保温、常规安全防护）最多写 1 条；五条卖点放在一起必须能看出「这是这一台，不是随便哪台」。
   维度按下面【品类档案】的顺序挑，参数表里没有的维度一个字都不许写。
4) 输出纯 JSON（不要 markdown 代码块）：
{{"titles":["","",""],"points":["","","","",""]}}

【品类档案】
{cat_block}

【真实数据】
{facts}"""


def _kk(kind):
    """kind 归一：l=聚光全新机 / s=样机 / x=第三种文案（2026-11-17 文案三列）。"""
    return 's' if kind == 's' else ('x' if kind == 'x' else 'l')


# 第三种文案（2026-11-17 用户令「文案三也是第三种文案」）：家庭贴心风，
# 跟聚光款（参数直给）错开——追加到 PROMPT 尾部，铁律照旧。
STYLE3_X = """

【本单特殊要求——第三种文案（卖家干货体）】
这一单是同一台机器的第三套文案，写法必须跟「参数直给型」拉开距离：
- 写法：像懂行卖家给买家列的干货清单——每条=一个真实参数（参数名+参数值进句子），一条顶一条；挑参数的角度跟常规型错开（常规型先写主参数，这单先写差异化参数），措辞和语序完全不同；
- 禁止：生活场景、使用画面、心理感受、效果承诺（「周末采购一周食材」「装满不拥挤」「百搭耐看」这类一句不许出现）；「我觉得/真省心/我家实测」这类第一人称感受一律不写；「看到就回/随时在线」这类回复时效承诺一律不写；
- 标题三条仍守铁律（型号全码+成色词+服务词），但语序换一种排法，别跟常规版式一样。
"""


def _facts_text(data, model):
    lines = ["型号: " + model]
    # 品类喂给 AI（2026-11-17：不喂就出「家用热水器/电器」这种瞎猜品类标题）
    _cat = str(data.get('cat') or '').strip()
    if _cat and _cat not in ('电器', '家电'):
        lines.insert(1, "品类: " + _cat)
    if data.get('pname'):
        lines.append("产品名: " + data['pname'])
    if data.get('price'):
        lines.append("价格: " + str(data['price']))
    for g, kv in (data.get('groups') or []):
        # 2026-11-10 防御：groups 里混进非两层结构（扁平键值对/字符串）就跳过这组，
        # 绝不让文案整单炸（参数库捷径形状实锤过 not enough values to unpack）
        if not isinstance(kv, (list, tuple)):
            continue
        _pairs = [x for x in kv if isinstance(x, (list, tuple)) and len(x) == 2]
        if not _pairs:
            continue
        lines.append("[%s] %s" % (g, "；".join("%s:%s" % (a, b) for a, b in _pairs)))
    if data.get('sellpoints'):
        lines.append("官网卖点: " + "、".join(data['sellpoints']))
    # 说明书要点（2026-11-23 新链）：官方使用说明转述给 AI 当真料——数字全真，
    # 进 facts 后数字闸放行；尺寸类行有 _is_size_line 一票否决兜底。
    _ml = [str(m).strip() for m in (data.get('manual') or []) if str(m).strip()]
    if _ml:
        lines.append("官方说明书要点(真实资料可转述): " + "；".join(_ml[:14]))
    return "\n".join(lines)


NUM_RE = re.compile(r"\d+(?:\.\d+)?")
SHIPY_RE = re.compile(r"发货|可发|物流|运费|包邮|偏远|送装|到家")
# 2026-11-14 用户令「640*420*390mm 横式壁挂节省空间算什么卖点」：尺寸/外形类一票否决。
# 两把尺：① 外形尺寸类词（外形尺寸/产品尺寸/机身尺寸/净重/毛重/装箱尺寸…）；
#          ② 两段以上数字用 ×/*/x 拼一起的长宽高（640*420*390、750×433×405）。
SIZE_WORD_RE = re.compile(
    r"外形尺寸|产品尺寸|机身尺寸|外机尺寸|内机尺寸|包装尺寸|装箱尺寸|安装尺寸|净重|毛重|重量|"
    r"横式壁挂|竖式安装|壁挂安装|外形.*mm|尺寸.*mm")
SIZE_NUM_RE = re.compile(r"\d+(?:\.\d+)?\s*[*×xX]\s*\d+(?:\.\d+)?\s*[*×xX]\s*\d+(?:\.\d+)?")
# 用户令「怎么文案里还会有全新原封」：这个词从词表里整体除名，标题/卖点出现即剔。
NO_WORDS = ("全新原封", "全新未拆封", "全新未开封")
# 2026-11-20 用户令「不要有这些什么鬼，全部按参数来写」（实锤「周末采购一周食材，冰箱
# 轻松装满不拥挤」「百搭又耐看，摆哪都显质感」）：场景/生活画面/心理感受类套话一票剔除
# ——prompt 已明令禁止，这里硬闸兜底，AI 不听话就整句毙。
SCEN_WORDS = ("周末采购", "一周食材", "一次买齐", "买齐", "囤", "聚会", "装满", "不拥挤",
              "百搭", "耐看", "显质感", "质感", "摆哪", "高级配色", "有档次", "有面子",
              "口感", "更新鲜", "保鲜期延长", "幸福感", "仪式感", "拿得出手")

_FACT_FLOATS = {}


def _fact_floats(facts):
    key = id(facts)
    if key not in _FACT_FLOATS:
        _FACT_FLOATS[key] = [float(x) for x in NUM_RE.findall(facts)]
    return _FACT_FLOATS[key]


def _is_size_line(line):
    """尺寸类卖点判定（2026-11-14）：长宽高连写 / 尺寸词命中都算。"""
    s = str(line or "")
    if SIZE_NUM_RE.search(s):
        return True
    if SIZE_WORD_RE.search(s):
        return True
    return False


def _verify(ai, facts, nums_set, bans=(), grounded=(), model=''):
    """防编造：titles/points 整行剔除。
    2026-11-14 加两道闸（用户令「谁看得懂啊」+「这种肯定不是卖点」）：
      · 参数直给卖点每条必须含数字，光有形容词的空话直接扔；
      · 品类禁写词（保证/绝对/隔夜还…）命中即扔，绝不承诺做不到的事。
    2026-11-14 再加两道（用户令「尺寸算什么卖点」+「怎么还有全新原封」）：
      · 尺寸/外形类整行扔（SIZE 判定）；
      · 「全新原封/全新未拆封」等被除名的词命中即扔。
    2026-11-17 补一条（用户令「蓝底都能写怎么到聚光就写不了」）：无数字的行如果
      命中【真实参数值/参数名】（grounded，如「洗烘一体」「一级能效」）也算数——
      防编造防的是凭空冒出来的，不是真参数不带数字。
    2026-11-20 加型号闸（BCD-335WLLFDEDBX1 丢 U 实锤）：行里出现长得像型号的串
      （大写字母数字+连字符）就必须跟真型号全码精确一致——AI 缩写/漏字母的串号
      型号比不写还毒，差一个字符整行毙。数字闸拦不住它（335/1 都是「真数字」）。"""
    fl = _fact_floats(facts)
    _true_models = {str(model or '').strip().upper()}
    try:
        _true_models.add(norm_model(model or '').upper())
    except Exception:
        pass
    _true_models.discard('')

    def ok(line):
        if _is_size_line(line):
            return False
        for w in NO_WORDS:
            if w in line:
                return False
        for w in SCEN_WORDS:
            if w in line:
                return False
        # 型号闸：像型号的串必须全码对上真型号
        for _c in re.findall(r'[A-Z0-9]{2,}(?:-[A-Z0-9]+)+', str(line).upper()):
            if _c not in _true_models:
                return False
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

    def ok_pt(line):
        if not ok(line):
            return False
        if not NUM_RE.search(line):      # 没数字：真参数命中可过，纯形容词空话照剔
            if not any(g and g in line for g in grounded):
                return False
        for b in bans:
            if b and b in line:
                return False
        return True

    out = {}
    out['titles'] = [t for t in ai.get('titles') or [] if isinstance(t, str) and ok(t)][:3]
    out['points'] = [p for p in ai.get('points') or [] if isinstance(p, str) and ok_pt(p)][:8]
    out['xhs'] = []
    return out


def glm_copy(model, kind, data, log):
    facts = _facts_text(data, model)
    has_params = bool(data.get('groups'))
    no_params = ("（本次没有抓到参数表，只有型号：文案里绝不允许出现任何具体参数和数字，"
                 "只写成色/交易保障/通用卖点）" if not has_params else "")
    brand = (data.get('brand') or '').strip() or '品牌'
    # 品类档案（2026-11-14）：认出品类的必讲维度/禁写词，认不出走通用兜底
    _props = []
    for _g, _kv in (data.get('groups') or []):
        if isinstance(_kv, (list, tuple)):
            _props += [x for x in _kv if isinstance(x, (list, tuple)) and len(x) == 2]
    try:
        import copy_cats as _cc
        cat_block = _cc.dims_text("%s %s" % (data.get('pname') or '', model), _props)
        bans = _cc.ban_words("%s %s" % (data.get('pname') or '', model), _props)
    except Exception:
        cat_block, bans = "品类：未识别（只写参数表里真实存在的项）", ()
    # 跨品类护栏（2026-11-20 实锤「开着空调也不心疼电费」出现在冰箱文案里）：
    # 认出是冰箱就禁提别的大家电，AI 串台词一票剔除。
    if '冰箱' in (str(data.get('cat') or '') + str(data.get('pname') or '')):
        bans = tuple(bans) + ('空调', '洗衣机', '电视', '微波炉', '洗碗机')
    kk_titles = ('注意：这是正品全新机——标题里必须用「全新机」，绝不许出现「展示样机/样机/全新原封」字样。'
                 if kind != 's' else
                 '注意：这是展示样机——标题里必须用「展示样机/功能完好」，绝不许出现「全新机/全新/全新原封」字样。')
    # 2026-11-23 LES50H-AC5 实锤「50L电水式」：AI 把品类词「电热水器」写变形——
    # 品类词必须逐字完整，禁任何缩写/变形/拼错。
    _kat = str(data.get('cat') or '').strip()
    if _kat and _kat not in ('电器', '家电'):
        kk_titles += ('\n品类词必须完整写「%s」，禁止任何缩写/变形/拼错'
                      '（绝不允许出现「电水式」「水热器」这类变形），标题里的品类词与「%s」逐字一致。'
                      % (_kat, _kat))
    # 2026-11-22 CWC16-B07BK 实锤：AI 把「装载容量 8套/每抽屉」整段写进标题/卖点，
    # 跟品名整机口径 16套 打架——容量一律用整机总量，禁出现「每抽屉」口径
    if '每抽屉' in str(_props) or '每抽屉' in str(data.get('pname') or ''):
        bans = tuple(bans) + ('每抽屉',)
    nums_set = set(NUM_RE.findall(norm_model(model)))
    # 2026-11-20 三列撞车治理：其他列（含蓝底口径那份）已发出的句子列成禁复用清单，
    # 直接喂给 AI——它看不见别列写了什么才是全列撞车的病根。
    _kk2 = _kk(kind)
    _others = _other_copy_lines(model, _kk2) + (_blue_points(model) or [])
    _ban_block = ""
    if _others:
        _ban_block = ("\n\n【另一列已发出的文案——禁复用清单】下面这些句子已经用在别的列了，"
                      "你不许再写意思相同或连续7字以上相同的句子（型号/品牌/容积这类硬指标数字除外），"
                      "每条卖点必须换角度、换句式、换措辞重新写（仍只许写参数事实，不许编场景）：\n"
                      + "\n".join("- " + o for o in _others[:30]))
    # 真参数词表（2026-11-17）：参数值/参数名里 ≥3 字的实词——无数字卖点命中即放行
    # （「洗烘一体」「一级能效」是真参数不带数字，不该被当空话剔）。值为「暂无数据」
    # 的参数整条不算（机器可能压根没有，AI 提了反而是编造）。
    grounded = set()
    for _g, _kv in (data.get('groups') or []):
        if not isinstance(_kv, (list, tuple)):
            continue
        for _k2, _v2 in _kv:
            _v2 = str(_v2 or '').strip()
            if not _v2 or _v2 in ('暂无数据', '暂无', '无', '-', '/', '不支持'):
                continue
            _k2s = str(_k2 or '').strip()
            if len(_k2s) >= 3:
                grounded.add(_k2s)
            for _tok in re.split(r'[，,、/；;\s]+', _v2):
                _tok = _tok.strip()
                if len(_tok) >= 3 and not NUM_RE.fullmatch(_tok):
                    grounded.add(_tok)
    # 2026-10-13 用户反馈「小红书/闲鱼拉胯」整改：质量闸（剔空洞口号/一句话配文）
    # + 质量不够自动重试一次（两次取分高那次），AI 解析失败也不再炸整单
    best, best_score = None, -1
    if not _api_key():
        log("没拿到 GLM key（云端没配 / 没登录）：AI 裸写跳过，只用官网实抓 + 欣盛兜底")
    for attempt in range(2):
        try:
            _ptxt = PROMPT.format(model=model, pname=data.get('pname') or model, brand=brand,
                                  no_params=no_params, kk_titles=kk_titles, facts=facts,
                                  cat_block=cat_block)
            if _ban_block:
                _ptxt += _ban_block
            if kind == 'x':
                _ptxt += STYLE3_X
            raw = _chat(_ptxt)
            m = re.search(r'\{.*\}', raw, re.S)
            ai = json.loads(m.group(0)) if m else {}
        except Exception as e:
            log("AI 第 %d 次生成失败：%s" % (attempt + 1, e))
            ai = {}
        res = _verify(ai, facts, nums_set, bans, grounded, model=model)
        # 价格尾巴兜底：AI 没听话写出 1649.0元 → 1649元（4.5kg 这类真小数不受影响）
        for _k in ('titles', 'points'):
            res[_k] = [re.sub(r'(\d)\.0+(?=[^\d]|$)', r'\1', s) for s in res[_k]]
        # 质量闸：卖点 8-40 字（参数直给不写长句，剔口水话）
        res['points'] = [p for p in res['points'] if 8 <= len(p) <= 40]
        # 2026-11-20 三列撞车：重复句重罚（一条 -3），两次生成取「跟其他列最不像」那次。
        # 基线 +10：AI 哑火吐空档时（两次都解析失败/全被闸剔）得分恒为最低，
        # 绝不许空档赢过带撞车罚分的实档（335 实锤：实档 -5 被空档 0 翻盘）。
        if not res['points'] and not res['titles']:
            score = -100
            _nd = 0
        else:
            _nd = sum(1 for _l in (res['titles'] + res['points'])
                      if _is_dup_line(_l, _others, model, brand)) if _others else 0
            score = 10 + len(res['points']) + len(res['titles']) - 3 * _nd
        if score > best_score:
            best, best_score = res, score
        if len(res['points']) >= 5 and (not _others or _nd <= 1):
            break
        if _others and _nd:
            log("第 %d 次生成跟其他列撞车 %d 条，重试换角度" % (attempt + 1, _nd))
        log("第 %d 次生成质量不够（卖点 %d/5，空话或无数字被剔）" % (attempt + 1, len(res['points'])))
    res = best or {'titles': [], 'points': [], 'xhs': []}
    dropped = ((3 - len(res['titles'])) + (8 - len(res['points'])))
    if dropped > 0:
        log("防编造/质量闸共剔除 %d 条不合格内容" % dropped)
    return res


# ---------------------------------------------------------------- 聚光卖点（2026-11-11 用户令）
def _resolve_model(model):
    """别名纠偏（2026-11-17 HRF-H575SEDU1 实锤）：发布记录里的型号串打错/官网口径
    不一致时，kitset 目录不存在 → 模糊匹配唯一近似目录名（≥0.88 且唯一才算）。
    匹配不上原样返回，不猜。"""
    try:
        import kitset as _ks
        if _ks.model_dir(model) and os.path.isdir(_ks.model_dir(model)):
            return model
        import difflib
        root = _ks.model_dir("X")
        root = os.path.dirname(root) if root else None
        if not root or not os.path.isdir(root):
            return model
        names = [n for n in os.listdir(root)
                 if os.path.isdir(os.path.join(root, n))]
        hits = difflib.get_close_matches(model, names, n=2, cutoff=0.88)
        if len(hits) == 1:
            return hits[0]
    except Exception:
        pass
    return model


def _load_setjson_cfg(model):
    """蓝底 set.json 的 cfg 快照（读不到返回 {}）——品类/品牌/尺寸数字的权威口径。"""
    try:
        import kitset as _ks
        p = os.path.join(_ks.model_dir(_resolve_model(model)), "set.json")
        if os.path.isfile(p):
            return json.load(io.open(p, encoding="utf-8")).get("cfg") or {}
    except Exception:
        pass
    return {}


def _blue_params(model):
    """蓝底 set.json 的真实参数表（cfg.params_rows / params），读不到返回 []。
    2026-11-17 用户问「蓝底都能写怎么到聚光就写不了」：官网/欣盛这条文案链没抓到
    参数（欣盛 groups 空），AI 没料可写 → 质量闸把仅剩的卖点剔到 1 条。蓝底抓取链
    明明抓到了参数（params_rows 8 条），同一台机器直接拿来喂文案 + 垫质量闸。"""
    try:
        import kitset as _ks
        p = os.path.join(_ks.model_dir(_resolve_model(model)), "set.json")
        if os.path.isfile(p):
            j = json.load(io.open(p, encoding="utf-8"))
            cfg = j.get("cfg") or {}
            rows = cfg.get("params_rows") or []
            out = []
            for r0 in rows:
                if isinstance(r0, (list, tuple)) and len(r0) >= 2:
                    k2, v2 = str(r0[0]).strip(), str(r0[1]).strip()
                    if k2 and v2 and v2 not in ("暂无数据", "暂无", "无", "-"):
                        out.append((k2, v2))
            if not out:
                for k2, v2 in (cfg.get("params") or {}).items():
                    v2 = str(v2).strip()
                    if v2 and v2 not in ("暂无数据", "暂无", "无", "-"):
                        out.append((str(k2).strip(), v2))
            return out
    except Exception:
        pass
    return []


def _blue_points(model):
    """蓝底文案已用的卖点一话一行（kitset set.json 的 points），读不到返回 []。
    聚光卖点要跟蓝底「尽量不要写的一样」，先把蓝底那份拿来做撞车比对。"""
    try:
        import kitset as _ks
        p = os.path.join(_ks.model_dir(_resolve_model(model)), "set.json")
        if os.path.isfile(p):
            j = json.load(io.open(p, encoding="utf-8"))
            return [str(x).strip() for x in (j.get("points") or []) if str(x).strip()]
    except Exception:
        pass
    return []


def _ptkey(s):
    """卖点比对键：去标点/空白、公斤→kg、统一小写——「10公斤大容量」和
    「10kg大容量，床单被罩一次洗完」这类换皮重复也要拦住。"""
    t = re.sub(r"[，,。；;：:、\s]+", "", str(s))
    return t.replace("公斤", "kg").replace("KG", "kg").lower()


# 2026-11-16 用户令「括号里面的加进去了干什么呢」：AI 爱在卖点后面补一句参数原名标注，
# 「12kg超大洗涤容量（洗涤容量12kg），轻松应对…」括号纯属重复。凡括号里带冒号/等号或数字的，
# 整段括号剥掉（正常括号如「（一级能效）」不动，因为后面没数字也不是 key:value）。
_META_PAREN_RE = re.compile(r"[（(][^（()）]{0,30}?(?:[:：=]|\d)[^（()）]{0,30}[）)]")
_ANY_PAREN_RE = re.compile(r"[（(]([^（()）]{1,30})[）)]")


def _strip_meta_paren(line):
    """剥掉卖点里的参数原名括号标注，并清理留下的多余空格/重复标点。

    两把尺：① 括号里带「参数名:值」或数字 → 剥（（洗净比:1.28）、（12kg））；
            ② 括号内容在括号前已经写过 → 剥（一级能效等级（一级能效）这种纯重复）。
    """
    s = str(line or "")
    for _ in range(3):                      # 可能连着两层括号
        before = s
        s = _META_PAREN_RE.sub("", s)

        def _rep(m):
            inner, head = m.group(1), s[:m.start()]
            k = _ptkey(inner)
            return "" if k and k in _ptkey(head) else m.group(0)

        s = _ANY_PAREN_RE.sub(_rep, s)
        if s == before:
            break
    s = re.sub(r"\s+([，,。；;])", r"\1", s)
    s = re.sub(r"([，,。；;])\1+", r"\1", s)
    return s.strip().strip("，,。；; ")


def _dedupe_vs_blue(pts, blue, cap=5):
    """聚光卖点跟蓝底去重（整句 + 首段两把尺），再砍到 ≤cap 条（用户令「不能超过五条」）。
    「尽量不要一样」：全撞车时保底用原序前 cap 条——条数合规优先，差异尽力而为。"""
    bk = {_ptkey(b) for b in (blue or [])}
    bk1 = {_ptkey(re.split(r"[，,]", b)[0]) for b in (blue or [])}
    out = []
    for p in (pts or []):
        if _ptkey(p) in bk or _ptkey(re.split(r"[，,]", p)[0]) in bk1:
            continue
        if _ptkey(p) in {_ptkey(x) for x in out}:
            continue
        out.append(p)
    if not out:
        out = list(pts or [])
    return out[:cap]


# 2026-11-20 三列大同小异实锤（用户令「能不能改一改」）：科创列=蓝底口径、东里列=kind=l、
# 文案三列=kind=x，三列各写各的 → 「400L大容量」「冷藏261L+冷冻139L」全列撞车。
# 治法：① AI prompt 里喂「其他列已发出的句子」当禁复用清单；② 打分时重复句重罚，
# 两次生成取「跟其他列最不像」的那次；③ generate() 末尾再硬过滤一遍换皮重复。

def _lcs_len(a, b):
    """最长公共连续子串长度（中文卖点换皮比对用）。"""
    if not a or not b:
        return 0
    if len(a) > len(b):
        a, b = b, a
    prev = [0] * (len(b) + 1)
    best = 0
    for ca in a:
        cur = [0] * (len(b) + 1)
        for j, cb in enumerate(b):
            if ca == cb:
                cur[j + 1] = prev[j] + 1
                if cur[j + 1] > best:
                    best = cur[j + 1]
        prev = cur
    return best


def _cmp_line(line, model, brand):
    """比对键：剥掉型号全码/品牌词/标点空白，只留实义文字再比。"""
    s = str(line or "")
    for w in (norm_model(model) or "", str(brand or "").strip(), "品牌"):
        if w and w in s:
            s = s.replace(w, "")
    return re.sub(r"[\s，,。；;：:、！!？?·\-—()（）/\+]+", "", s)


def _is_dup_line(line, others, model, brand):
    """换皮重复判定：剥掉型号/品牌后连续 7 字以上撞上其他列的句子即算。"""
    a = _cmp_line(line, model, brand)
    if len(a) < 8:
        return False
    for o in others:
        b = _cmp_line(o, model, brand)
        if b and _lcs_len(a, b) >= 7:
            return True
    return False


def _other_copy_lines(model, kk):
    """其他列已发出的句子清单（kind 缓存 l/s/x 互查，供禁复用/硬过滤）。"""
    nm = _safe_dir(norm_model(model) or model)
    out = []
    for k2 in ("l", "s", "x"):
        if k2 == kk:
            continue
        f = os.path.join(copy_root(), "%s_%s_v4.json" % (nm, k2))
        if not os.path.isfile(f):
            continue
        try:
            d = json.load(io.open(f, encoding="utf-8"))
        except Exception:
            continue
        for key in ("titles", "points"):
            for t in (d.get(key) or []):
                if isinstance(t, str) and t.strip():
                    out.append(t.strip())
    return out


def _load_kind_cache(model, k):
    """读指定列的文案缓存（读不到/坏了返回 {}），供文案三「按参数、改排版」复用。"""
    nm = _safe_dir(norm_model(model) or model)
    f = os.path.join(copy_root(), "%s_%s_v4.json" % (nm, k))
    if not os.path.isfile(f):
        return {}
    try:
        return json.load(io.open(f, encoding="utf-8"))
    except Exception:
        return {}


# 2026-11-20 用户令「文案就是不肯给我改是吧」实锤：x/l 列卖点「700mm超薄机身」
# 同开头整词撞车（规格词就那几个，AI 禁了还写）。撞车的开头规格句自动「换装」：
# 数字挪进句中换种说法（400L大容量，… → 容量一次给到400L，…），换完不再撞才收。
_REWORD_OPENERS = (
    ("大容量", "容量一次给到"),
    ("超薄机身", "机身厚度压到"),
    ("窄机身", "机身宽度只有"),
    ("合适高度", "高度正好卡在"),
    ("高级配色", "颜色用的是"),
    ("配色", "颜色用的是"),
)


def _reword_opener(line):
    s = str(line)
    m = re.match(r"^(\d+(?:\.\d+)?)(L|升|mm|分贝|W|kg|KG)([^，,]*)[，,]\s*(.+)$", s)
    if m:
        num, unit, desc, rest = m.group(1), m.group(2), m.group(3).strip(), m.group(4).strip()
        for k, v in _REWORD_OPENERS:
            if k in desc:
                return "%s%s%s，%s" % (v, num, unit, rest)
    m = re.match(r"^([一-龥]{2,4})(?:高级)?配色[，,]\s*(.+)$", s)
    if m:
        return "颜色用的是%s，%s" % (m.group(1), m.group(2))
    m = re.match(r"^([12一二]级)能效([^，,]*)[，,]\s*(.+)$", s)
    if m:
        return "能效%s%s，%s" % (m.group(1), m.group(2).strip(), m.group(3))
    m = re.match(r"^运行噪音\s*(\d+(?:\.\d+)?)\s*分贝[^，,]*[，,]\s*(.+)$", s)
    if m:
        return "噪音控制在%s分贝，%s" % (m.group(1), m.group(2))
    return s


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
        # 同 _facts_text 的防御：喂歪的组直接跳过（2026-11-10 参数库捷径实锤）
        if not isinstance(kv, (list, tuple)):
            continue
        for x in kv:
            if not (isinstance(x, (list, tuple)) and len(x) == 2):
                continue
            a, b = x
            s = '%s %s' % (a, b)
            if s not in flat:
                flat.append(s)
    if flat:
        L.append('【参数】' + ' | '.join(flat[:10]))
    if str(data.get('price') or '').strip():
        # 2026-11-21 实锤：x（文案三）被落成「展示样机」——文案三也是全新机口径（与
        # 1039 行标题规则/1782 行 AI 规则对齐），只有 s（样机列）才用展示样机
        L.append('【官网价】%s （本店售价见标价，%s）'
                 % (data['price'], '展示样机' if kk == 's' else '全新机'))
    L.append('【交易】先验后签收，破损拒收')
    L.append('')
    if kk == 'l':
        L.append('%s官方正品，全新机，全国联保' % brand)
        L.append('品质保证，放心购买，售后无忧')
    elif kk == 'x':
        # 文案三结尾措辞与聚光必异（三列意思可同措辞必异），口径同为全新机
        L.append('%s官方正品，全新机，全国联保' % brand)
        L.append('功能完好，放心拍，售后无忧')
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
    log("配图落盘 %d 张（官网 %d + 说明书 %d）" % (len(saved), n_off, len(saved) - n_off))
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
    nm = _safe_dir(norm_model(model) or model)
    kk = _kk(kind)
    cache = os.path.join(copy_root(), "%s_%s_v7.json" % (nm, kk))   # v7：图四层/参数两层新链（旧缓存不读）
    if not force and os.path.isfile(cache):
        try:
            with io.open(cache, encoding="utf-8") as f:
                res = json.load(f)
            res["cached"] = True
            # 2026-11-16 用户实锤「啥都没改到」：缓存直读绕过了括号剥离——旧档在这里再兜一道，
            # 剥完跟原档不一致就回写，desc 逐行剥（卖点块/正文里残留的参数原名括号）。
            _pt = [p for p in (_strip_meta_paren(x) for x in (res.get("points") or [])) if p]
            _dl = lambda t: "\n".join(_strip_meta_paren(x) for x in str(t or "").split("\n"))
            if _pt != res.get("points"):
                res["points"] = _pt
                res["desc_nat"] = _dl(res.get("desc_nat"))
                res["desc_rep"] = _dl(res.get("desc_rep"))
                try:
                    with io.open(cache, "w", encoding="utf-8") as f:
                        json.dump(res, f, ensure_ascii=False)
                except Exception:
                    pass
            return res
        except Exception:
            pass
    if kind == 'z':
        # 婷宝版配文专用老链（2026-11-23 用户令「宝是宝婷是婷」分家）：官网+欣盛，
        # v3.0.87 原版行为；宝三文案（l/s/x）走下面的官网→说明书新链，互不影响
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
        # 抓取链 0 参数时的补强顺序（2026-11-21）：① 海尔电子说明书站（纯接口秒级零成本）
        # → ② 欣盛详情长图视觉读参（~2 分钟）。都是真参数，AI 铁律不编造照旧。
        if not data.get('groups'):
            _hzy_rows = _hzy_params(model, log)
            if _hzy_rows:
                data['groups'] = [("参数", _hzy_rows)]
        if not data.get('groups'):
            _xs_pid = data.get('pid') or (xs or {}).get('pid')
            if _xs_pid:
                _xs_rows = _xs_img_params(model, {'pid': _xs_pid}, log)
                if _xs_rows:
                    data['groups'] = [("参数", _xs_rows)]
    else:
        # 2026-11-23 用户令（宝窗口新链）：参数=官网→说明书站两层；图=官网→说明书→
        # 欣盛→中关村四层；R2 图库/pubwork/必应/GLM/欣盛读参 全下线。
        # 官网优先（need_imgs=True：云端参数库命中也要打官网拿图）；
        # 官网缺参数或缺图（或整没命中）才打说明书站全量——官网抓到的不再重复抓。
        data = fetch_official(model, log, need_imgs=True)
        _book = None
        if not data:
            log("官网没命中，转说明书站兜底")
            data = {'source': '无数据', 'brand': '', 'pname': model, 'price': '',
                    'url': '', 'groups': [], 'sellpoints': [], 'imgs': []}
        if not data.get('groups') or not (data.get('imgs') or []):
            _book = _book_fetch(model, log)
            if _book:
                # 参数：官网有的键不覆盖，缺的整组追加（官网优先，说明书补缺）
                if _book.get('rows'):
                    if not data.get('groups'):
                        data['groups'] = [("参数", _book['rows'])]
                        if log:
                            log("参数全量来自说明书站：%d 条" % len(_book['rows']))
                    else:
                        _have = set()
                        for _g, kv in (data.get('groups') or []):
                            if isinstance(kv, (list, tuple)):
                                for k, _v in kv:
                                    _have.add(str(k).strip())
                        _add = [(k, v) for k, v in _book['rows'] if str(k).strip() not in _have]
                        if _add:
                            data['groups'].append(("参数", _add))
                            if log:
                                log("说明书补缺参数 %d 条" % len(_add))
                # 图：官网一张没有 → 说明书全量顶上；官网有图 → 只补尺寸线稿（官网详情页没有线稿）
                # 扫描版 PDF（scanned）例外：页面全是线稿/文字当不了主图，留给中关村层
                if (not (data.get('imgs') or []) and _book.get('imgs')
                        and not _book.get('scanned')):
                    data['imgs'] = [u for _n, u in _book['imgs']]
                    data['_book_imgs'] = True
                    if log:
                        log("官网无图，说明书图 %d 张顶上" % len(data['imgs']))
                elif _book.get('size_imgs'):
                    data['_book_size'] = [u for _n, u in _book['size_imgs']
                                          if u not in (data.get('imgs') or [])]
                    if log and data['_book_size']:
                        log("说明书补尺寸线稿 %d 张（追加在官网图之后）" % len(data['_book_size']))
                # 使用说明文字 → AI 素材
                if _book.get('manual'):
                    data['manual'] = _book['manual']
                # 官网整没命中时，品牌/品名用说明书口径兜底（树根 name=「型号说明书」）
                if data['source'] == '无数据':
                    data['source'] = '说明书'
                    data['url'] = _book.get('url') or ''
                    if _book.get('brand') and not data.get('brand'):
                        data['brand'] = _book['brand']
                    _bn = re.sub(r'说明书\s*$', '', str(_book.get('root') or '')).strip()
                    if _bn and norm_model(_bn) == norm_model(model):
                        data['pname'] = _bn
        # 图片兜底两层（2026-11-23 用户令「图片加一个中关村的，参数只抓说明书网站的」）：
        # 参数只走 官网→说明书（欣盛详情长图读参砍掉——一张 30 秒起步，是文案慢的大头）；
        # 欣盛/中关村只补图不补参，图全空才依次打。
        # 2026-11-23 补：扫描版 PDF 说明书整页渲染全是线稿/文字页，不算「有真机图」。
        _scanned_book = bool((_book or {}).get('scanned'))
        if not (data.get('imgs') or []) or _scanned_book:
            _xs = fetch_xinsheng(model, log)
            if _xs and (_xs.get('imgs') or []) and not (data.get('imgs') or []):
                data['imgs'] = list(_xs['imgs'])
                if log:
                    log("欣盛商城图 %d 张顶上" % len(data['imgs']))
            if not (data.get('imgs') or []):
                # 中关村图兜底（只图不参；懒 import 避免环，运行期 kitset 必已加载或可独立加载）
                try:
                    import kitset as _ks
                    _zi, _zp = _ks._zol_data(model, str(data.get('brand') or ''), log)
                except Exception:
                    _zi = []
                if _zi:
                    data['imgs'] = [u for u in _zi if u]
                    if log:
                        log("中关村图 %d 张顶上" % len(data['imgs']))
            _xs = _xs or {}
            if data['source'] == '无数据' and _xs.get('pname'):
                data['source'] = '欣盛商城'
                data['pname'] = _xs['pname']
                data['price'] = str(_xs.get('price') or '')
    # 蓝底参数直给（2026-11-17 用户令「不是抓不到时才兜底，直接把蓝底 set.json 的
    # 真实参数给聚光，不要我去抓了」）：聚光文案的参数源固定为蓝底 set.json 快照——
    # 有就直接用（覆盖官网/欣盛抓来的），抓取链只补价格/图，不再决定参数有没有。
    # 质量闸口径跟蓝底一致：真参数（含不带数字的参数词）都算数，不凭空加严。
    _bp = _blue_params(model)
    if _bp:
        data['groups'] = [("参数", _bp)]
        if log:
            log("聚光文案参数直接用蓝底 set.json 的 %d 条真实参数（不依赖抓取链）" % len(_bp))
    # 品类补强（2026-11-17 HRF-H57SSEDU1 实锤「家用热水器/电器」）：info.json 的 cat
    # 常是「电器」废词，蓝底 set.json 的 cfg.cat 是抓取链认过的正经品类——直接喂文案，
    # 标题品类词跟蓝底口径一致。
    try:
        _cfg = (_load_setjson_cfg(model) or {})
        _cat = (_cfg.get('cat') or '').strip()
        if _cat and _cat != '电器' and (data.get('cat') or '') in ('', '电器', '家电'):
            data['cat'] = _cat
            data['pname'] = data.get('pname') or ((_cfg.get('brand') or '') + _cat)
            if log:
                log("品类补强：文案品类用蓝底口径「%s」" % _cat)
        # 品牌补强（2026-11-20 kind=l 缓存标题出现占位词「品牌」实锤）：leader 这类
        # 官网抓不到品牌的机器，标题里绝不让「品牌」两个字占位——用蓝底 cfg.brand 兜底。
        if not str(data.get('brand') or '').strip():
            _bd = (_cfg.get('brand') or '').strip()
            if _bd:
                data['brand'] = _bd
                if log:
                    log("品牌补强：文案品牌用蓝底口径「%s」" % _bd)
        # 裸型号 pname 升级（2026-11-22 CWC16-B07BK 实锤）：官网/欣盛全无数据的型号
        # pname 只剩裸型号，AI 拿不到品类语境瞎猜出「家用热水器」标题。蓝底 set.json
        # 顶层 name（品类自愈后=品牌+品类+型号）是权威：带品类词且含型号才升级。
        # 顺手把蓝底真卖点（品类自愈后的容量/双子抽屉等）喂给 AI 当料——无数据型号
        # sellpoints 是空的，不喂 AI 就没东西可写。
        _CATW = ('洗碗机', '冰箱', '洗衣机', '热水器', '空调', '电视', '油烟机',
                 '燃气灶', '消毒柜', '蒸箱', '烤箱', '微蒸烤', '酒柜', '冷柜',
                 '净水', '茶吧机', '饮水机')
        try:
            import kitset as _ks2
            _sj = (json.load(io.open(os.path.join(
                _ks2.model_dir(_resolve_model(model)), "set.json"),
                encoding="utf-8")) or {})
        except Exception:
            _sj = {}
        _sjname = str(_sj.get('name') or '').strip()
        _pn = str(data.get('pname') or '').strip()
        if (_sjname and nm in _sjname
                and any(w in _sjname for w in _CATW)
                and not any(w in _pn for w in _CATW)):
            data['pname'] = _sjname
            if log:
                log("pname 升级：裸型号 → 蓝底口径「%s」" % _sjname)
        if not (data.get('sellpoints') or []):
            _bpts = [p for p in (_sj.get('points') or []) if p and str(p).strip()]
            if _bpts:
                data['sellpoints'] = _bpts
                data['_blue_pts'] = _bpts
                if log:
                    log("卖点喂料：文案卖点用蓝底 set.json 的 %d 条真卖点" % len(_bpts))
    except Exception:
        pass
    # 价格统一整数显示：抓来的 "1649.0" 直接进标题会带小数尾巴（2026-10-12 实锤）
    _pr = str(data.get('price') or '').strip()
    try:
        _f = float(_pr)
        data['price'] = str(int(_f)) if _f == int(_f) else str(round(_f, 2))
    except ValueError:
        pass
    # 2026-11-20 用户令「全部按参数来弄，改排版即可」：文案三（kind=x）不再单独让
    # AI 另写一套（参数就那几条，写出来还是大同小异），直接复用聚光（kind=l）同一份
    # 参数事实做排版级变换——开头换装 + 分句轮换（A，B，C→B，C，A）+ 顺序轮换，
    # 标题倒序重排。事实同一份、措辞和排版必异。
    ai = None
    _x_layout = False
    if kk == 'x':
        _lsrc = _load_kind_cache(model, 'l')
        if _lsrc and (_lsrc.get('points') or _lsrc.get('titles')):

            def _layout(p):
                q = _reword_opener(p)
                parts = [x.strip() for x in re.split(r"[，,]", q) if x.strip()]
                if len(parts) >= 3:
                    q = "，".join(parts[1:] + parts[:1])
                return q

            _px = [_layout(p) for p in (_lsrc.get('points') or [])]
            if len(_px) >= 3:
                _px = _px[2:] + _px[:2]
            ai = {'titles': list(_lsrc.get('titles') or [])[::-1], 'points': _px}
            _x_layout = True
            if log:
                log("文案三按用户令「按参数、改排版」：复用聚光参数事实，换装+分句轮换+顺序轮换")
    if ai is None:
        ai = glm_copy(model, kk, data, log)
    mdir = os.path.join(copy_root(), nm)
    if kind == 'z':
        # 图片合并（婷老链 v3.0.87 原版）：官网图在前、欣盛图在后，URL 去重后全部落盘
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
    else:
        # 图片合并（2026-11-23 新链）：官网图在前；官网无图时说明书图顶上（标签「说明书」）；
        # 官网有图时把说明书的尺寸线稿追加在尾部（官网详情页没有线稿，用户令「尺寸图也在说明书找」）
        pairs, _seen = [], set()
        _pair_src = '说明书' if data.get('_book_imgs') else '官网'
        for u in (data.get('imgs') or []):
            if u and u not in _seen:
                _seen.add(u)
                pairs.append((u, _pair_src))
        for u in (data.get('_book_size') or []):
            if u and u not in _seen:
                _seen.add(u)
                pairs.append((u, '说明书'))
        images = dl_images(pairs, mdir, log)
    # 2026-10-24 用户令：0_主图_卖点拼版.jpg 所有的都不用做，自动合成已停
    # 卖点：AI 从真实参数提炼的加厚条优先（2026-10-13 用户定稿，官网 sellpoint 是
    # 「智能生活新选择」类空洞口号不要）；AI 全被剔才拿官网卖点兜底
    pts = ai['points'][:15] or [p for p in (data.get('sellpoints') or []) if p][:15]
    # 2026-11-16 用户令「括号里面的加进去了干什么呢」：剥掉「（洗涤容量12kg）」这类
    # 参数原名括号标注（prompt 侧已禁写，这里对缓存/旧档再兜一道），剥空的整条丢掉。
    pts = [p for p in (_strip_meta_paren(x) for x in pts) if p]
    # 2026-11-14 用户令「聚光的卖点尽量不要跟蓝底的写的一样，也是在5条以下，不能超过五条」：
    # 先跟蓝底 set.json 的卖点去重（整句/首段归一比对，换皮重复也拦），再统一砍到 ≤5 条；
    # 卖点 tab、描述里的 ·卖点 块、一键复制全吃这份，口径一致。
    # 2026-11-14 用户令「640*420*390mm 横式壁挂节省空间算什么卖点」：尺寸/外形类
    # 兜底再扫一遍（AI 闸在 _verify，这条兜官网 sellpoints 直通的漏网）。
    _pts0 = _dedupe_vs_blue(pts, _blue_points(model), cap=5)
    pts = [p for p in _pts0 if not _is_size_line(p)
           and "每抽屉" not in p
           and not any(w in p for w in NO_WORDS) and not any(w in p for w in SCEN_WORDS)]
    # 2026-11-22 无数据型号保底（CWC16-B07BK 实锤「东星斑都没有数据了」）：全网零参数
    # 型号 AI 只有蓝底几条真卖点当料，质量闸/两列去重剔完只剩 1 条——蓝底喂料在
    # 3 条以下时换装补回（_reword_opener 换开头，不跟蓝底整句一样，11-14 两列
    # 不同口径照守），凑够卖点条数优先。
    if len(pts) < 3 and data.get('_blue_pts'):
        for _bp in data['_blue_pts']:
            if len(pts) >= 5:
                break
            _bp2 = _reword_opener(_bp)
            _k2 = _bp2.split("，")[0]
            # 首段撞头也算双胞胎（16套大容量×2 实锤）：开头规格句一样的不补
            if _bp2 and not any((_x in p or p in _x) for p in pts for _x in (_bp, _bp2)) \
                    and not any(p.split("，")[0] in _k2 or _k2 in p.split("，")[0]
                                for p in pts) \
                    and not _is_size_line(_bp2) \
                    and not any(w in _bp2 for w in NO_WORDS + SCEN_WORDS):
                pts.append(_bp2)
        if log and len(pts) >= 3:
            log("无数据型号卖点保底：蓝底喂料换装补到 %d 条" % len(pts))
    # 2026-11-20 三列撞车治理：AI 写完再硬过滤一遍——跟其他列/蓝底连续 7 字撞车的
    # 换皮句整条扔；剔完不足 3 条就回退（条数优先，撞车靠 prompt 禁复用清单压）。
    _oth2 = _other_copy_lines(model, kk) + (_blue_points(model) or [])
    if _oth2 and not _x_layout:
        _bd0 = str(data.get('brand') or '')
        _kept, _rw = [], 0
        for _p in pts:
            if _is_dup_line(_p, _oth2, model, _bd0):
                _p2 = _reword_opener(_p)
                if not _is_dup_line(_p2, _oth2, model, _bd0):
                    _kept.append(_p2)
                    _rw += 1
                # 换装后还撞 → 整条扔
                continue
            _kept.append(_p)
        if _rw and log:
            log("撞车卖点开头换装 %d 条（同开头规格句换说法）" % _rw)
        if len(_kept) >= 3:
            if len(_kept) < len(pts) and log:
                log("跟其他列撞车的卖点剔掉 %d 条（连续7字换皮比对）" % (len(pts) - len(_kept)))
            pts = _kept
        elif log:
            log("撞车卖点剔完只剩 %d 条，保留原序 %d 条（禁复用清单兜底）" % (len(_kept), len(pts)))
    res = {"ok": True, "cached": False, "model": model, "kind": kk,
           "source": data['source'], "source_url": data['url'],
           "pname": data['pname'], "price": data['price'],
           "titles": ai['titles'], "points": pts,
           "desc_nat": build_desc(model, kk, data, pts, 'nat'),
           "desc_rep": build_desc(model, kk, data, pts, 'rep'),
           "xhs": ai.get('xhs') or [],
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

REWRITE_DESC_PROMPT = """你是闲鱼商品描述写手。把下面这段商品描述**推倒重写**——换一套完全不同的结构和写法，开场白、卖点措辞、段落顺序、结尾话术都要换新写法，跟原文差异越大越好，不要照抄原句：
- 只能使用描述原文和【真实数据】里出现的参数/数字，禁止编造任何新参数新数字
- 必须保留的要素（一个都不能少）：【产品名】行（型号全码保留）、·卖点条目（条数不许减少）、【参数】行、【官网价】行、【交易】行、结尾话术、可发货地区行
- 仍然分段式排版、一话一行、每段之间空一行
- 成色词口径：{kk_rule}
- 不加 emoji，不加 #话题，不出现「最好/第一/顶级」等极限词
只输出重写后的完整描述正文，不要任何解释。

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
    nm = _safe_dir(norm_model(model) or model)
    return os.path.join(copy_root(), "%s_%s_v4.json" % (nm, _kk(kind)))


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


def polish(model, kind, target, text, log=None, mode='polish'):
    """AI 润色：target='titles'（重写三选一）| 'desc'（润色描述正文）。带防编造校验。"""
    log = log or (lambda s: None)
    kk = _kk(kind)
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
        kk_rule = ('成色词必须用「全新机」，绝不许出现「样机/全新原封」字样' if kk != 's'
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
    kk_rule = ('成色词必须用「全新机」，绝不许出现「样机/全新原封」字样' if kk != 's'
               else '成色词必须用「展示样机/功能完好」，绝不许出现「全新」字样')
    _dprompt = REWRITE_DESC_PROMPT if mode == 'rewrite' else POLISH_DESC_PROMPT
    new = _chat(_dprompt.format(facts=facts, kk_rule=kk_rule, orig=str(text or ''))).strip()
    new = re.sub(r'^```[a-z]*\s*|\s*```$', '', new).strip()
    # 数字防编造：逐行校验。润色版=带未知数字的行回退原文对应行（行对行）；
    # 重写版=结构全换行对不上，带未知数字的行直接剔除。
    if text and mode != 'rewrite':
        old_lines = str(text).split('\n')
        out = []
        for i, ln in enumerate(new.split('\n')):
            if ln.strip() and not ok_nums(ln) and i < len(old_lines) and ok_nums(old_lines[i]):
                out.append(old_lines[i])
            else:
                out.append(ln)
        new = '\n'.join(out)
    elif mode == 'rewrite':
        bad = [ln for ln in new.split('\n') if ln.strip() and not ok_nums(ln)]
        if len(bad) > 3:
            return {"ok": False, "msg": "重写失败：AI 编了 %d 处未知数字，防编造校验整版拒收，再试一次" % len(bad)}
        new = '\n'.join(ln for ln in new.split('\n') if not (ln.strip() and not ok_nums(ln)))
        if not new.strip():
            return {"ok": False, "msg": "重写失败：全被防编造校验剔除，再试一次"}
    return {"ok": True, "desc": new}


def qa_copy(model, kind, text, log=None):
    """AI 自检：对标题+卖点+描述整体挑毛病，另附广告法极限词本地硬扫。"""
    log = log or (lambda s: None)
    kk = _kk(kind)
    facts = _facts_of(model, kk)
    kk_rule = ('这是正品全新机——文案里必须用「全新机」，绝不许出现「展示样机/样机」字样' if kk != 's'
               else '这是展示样机——文案里必须用「展示样机」，绝不许出现「全新机/全新」字样')
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
        if kk == 's' and re.search(r'全新机|全新原封|全新未拆封', ln):
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
    if _kk(kind) == 'x':
        # 第三种文案（2026-11-17 文案三列）：不走 peiwen 覆盖——标题/描述必须跟
        # 聚光款错开，让 AI 按家庭贴心风直出；括号剥离 generate 里已兜过。
        return res
    _info = None
    try:
        import pubwork as _pw
        import pei_core as _pc
        _info = _pw.load_info(model)
        # 2026-11-23 用户令「参数只抓说明书网站的」+「文案写的慢」：宝链（l/s/x）只
        # 复用本地 peiwork 缓存，绝不起 pubwork 网络爬取链（新爬虫→必应旧检索→兴达——
        # C8U12G3 实锤旧检索一卡 480+ 秒，就是文案慢的元凶）。本地缓存没有就用
        # generate() 自产结果（官网→说明书口径）；婷宝版（kind=z）老链照旧。
        if kind == 'z' and (not _info or not (_info.get("props") or [])):
            try:
                _info = _pw.work(model, prog=lambda p, m: log(m))
            except Exception as _we:
                log("peiwen 抓取链未命中（%s），用内置算法" % _we)
                _info = None
        if _info and (_info.get("props") or []):
            # 2026-11-17 用户令「质量闸跟蓝底一样」：peiwen 信息里的「暂无数据/无/-」
            # 脏行先清掉再喂标题/描述（HL732EH 实锤标题出「家用暂无数据」），
            # 口径与 copywriter._blue_params 一致——真参数才算数。
            _cp = [p for p in _info["props"]
                   if isinstance(p, (list, tuple)) and len(p) == 2
                   and str(p[0] or '').strip()
                   and str(p[1]).strip() not in ("暂无数据", "暂无", "无", "-", "/", "不支持")]
            if _cp:
                _info["props"] = _cp
            # 品类纠偏（2026-11-17 HRF-H57SSEDU1 实锤「571L风冷无霜电器」）：peiwen
            # info 的 cat 常是「电器」废词且 title 只有裸型号认不回来，蓝底 set.json
            # 的 cfg.cat 是权威口径，直接纠。
            try:
                _bc = (_load_setjson_cfg(model) or {}).get('cat')
                if _bc and str(_bc).strip() not in ('', '电器', '家电') \
                        and str(_info.get('cat') or '').strip() in ('', '电器', '家电'):
                    _info['cat'] = str(_bc).strip()
            except Exception:
                pass
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
            _kk2 = _kk(kind)
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
