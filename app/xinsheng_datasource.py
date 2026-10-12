# -*- coding: utf-8 -*-
"""
欣盛臻选电器 数据源模块（可直接嵌入 ModelLookup）
================================================
零第三方依赖（纯标准库），线程安全可选。

用法（3 行跑通增量同步）:
    from xinsheng_datasource import XinshengDataSource
    ds = XinshengDataSource(db_path='xinsheng.sqlite3')
    stats = ds.sync()          # 全量/增量同步到本地 sqlite
    rows = ds.search('冰箱')   # 本地查询
    ds.export_json('out.json') # 导出统一格式

设计要点:
- 走官方 H5 后端 batchSubmit 通道（与小程序/H5 页面同源），带 0.4s/批 限速
- sqlite 缓存 + 断点续传，重复 sync 只拉新增/变化商品
- 接口失效时抛 XinshengApiError 并记录日志，不影响宿主软件其他功能
"""
import hashlib
import http.cookiejar
import json
import logging
import os
import re
import sqlite3
import time
import urllib.parse
import urllib.request

log = logging.getLogger('xinsheng')

BASE_URL = 'https://ys.longyangdq.cn'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126 Safari/537.36'
BATCH_SIZE = 20          # batchSubmit 每批商品数
THROTTLE = 0.4           # 每批间隔秒
MAX_RETRY = 3

# 常见家电型号前缀（用于从商品名提取型号）
MODEL_PATTERNS = [
    r'BCD[-\s]?\d{2,4}[A-Z0-9]*',      # 冰箱
    r'XQG?\d{2,3}[-\s]?[A-Z0-9]+',     # 洗衣机
    r'KFR[-\s]?\d{2}GW[A-Z0-9/]*',     # 空调挂机
    r'KFR[-\s]?\d{2}LW[A-Z0-9/]*',     # 空调柜机
    r'KFR[-\s]?\d{2}W[A-Z0-9/]*',      # 空调中央
    r'ES\d{2}H[A-Z0-9]*',              # 电热水器
    r'JSQ\d{2}[-\s]?[A-Z0-9]+',        # 燃气热水器
    r'CXW[-\s]?\d{3}[A-Z0-9]*',        # 油烟机
    r'JZT[-\s]?[A-Z0-9]+',             # 燃气灶
    r'WQP\d+[-\s]?[A-Z0-9]*',          # 洗碗机
    r'[A-Z]{2,4}\d{2,4}[A-Z]{1,4}\d*', # 兜底：通用大写字母+数字
]


class XinshengApiError(Exception):
    pass


class XinshengDataSource:
    """欣盛臻选商城数据源：同步 → sqlite → 查询/导出"""

    def __init__(self, db_path=None, throttle=THROTTLE):
        self.db_path = db_path or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'xinsheng.sqlite3')
        self.throttle = throttle
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self._jar))
        self._init_db()
        self._touch_session()

    # ---------------- HTTP 层 ----------------

    def _request(self, url, data=None, headers=None, timeout=20):
        h = {'User-Agent': UA, 'Referer': BASE_URL + '/'}
        if headers:
            h.update(headers)
        req = urllib.request.Request(url, data=data, headers=h)
        last_err = None
        for attempt in range(MAX_RETRY):
            try:
                with self._opener.open(req, timeout=timeout) as r:
                    raw = r.read()
                    if 'gzip' in r.headers.get('Content-Encoding', ''):
                        import gzip
                        raw = gzip.decompress(raw)
                    return raw.decode('utf-8', 'replace')
            except Exception as e:
                last_err = e
                time.sleep(1.5 * (attempt + 1))
        raise XinshengApiError(f'{url} 连续 {MAX_RETRY} 次失败: {last_err}')

    def _touch_session(self):
        """访问首页拿 acw_tc 会话 cookie"""
        try:
            self._request(BASE_URL + '/')
        except XinshengApiError as e:
            log.warning('会话建立失败（可能断网）: %s', e)

    def _api(self, path, method='GET', params=None, body=None):
        q = {'iShopId': '', 'terminaltype': 'PC', '_': int(time.time() * 1000)}
        if params:
            q.update(params)
        url = f'{BASE_URL}/{path.lstrip("/")}?' + urllib.parse.urlencode(q)
        data = None
        headers = {'X-Requested-With': 'XMLHttpRequest'}
        if body is not None:
            data = json.dumps(body).encode()
            headers['Content-Type'] = 'application/json;charset=utf-8'
        resp = self._request(url, data=data, headers=headers)
        try:
            js = json.loads(resp)
        except json.JSONDecodeError:
            raise XinshengApiError(f'{path} 返回非 JSON: {resp[:120]}')
        return js

    def _batch(self, calls):
        """batchSubmit 批量打包：calls = [{'path','method','params','body'}]
        网关对非浏览器 GET 一律 404，必须经此 POST 通道。"""
        payload = []
        for c in calls:
            q = {'iShopId': '', 'terminaltype': 'PC', 'token': ''}
            if c.get('params'):
                q.update(c['params'])
            payload.append({
                'requestUrl': '/' + c['path'].lstrip('/') + '?' + urllib.parse.urlencode(q),
                'requestMethod': c.get('method', 'GET').upper(),
                'requestData': c.get('body'),
            })
        r = self._api('client/batchSubmit', 'POST', body=payload)
        if r.get('code') != 200:
            raise XinshengApiError(f'batchSubmit 失败: {str(r)[:150]}')
        return r['data']

    # ---------------- 业务接口 ----------------

    def iter_product_list(self, pagesize=50, max_pages=60):
        """全量商品列表（生成器，自动翻页到空）"""
        page = 1
        while page <= max_pages:
            r = self._api('client/Orders/getProductMainTopList', 'POST',
                          body={'queryCondition': {'pagesize': pagesize, 'pageindex': page}})
            items = (r.get('data') or {}).get('data') or []
            if not items:
                return
            yield from items
            page += 1
            time.sleep(self.throttle)

    def get_details(self, pids):
        """批量拉商品详情（pid 列表 → {pid: detail_dict}）"""
        out = {}
        for i in range(0, len(pids), BATCH_SIZE):
            chunk = pids[i:i + BATCH_SIZE]
            calls = [{'path': 'client/Products/index', 'method': 'GET', 'params': {'id': pid}}
                     for pid in chunk]
            items = self._batch(calls)
            for pid, item in zip(chunk, items):
                if item and item.get('code') == 200 and isinstance(item.get('data'), dict):
                    d = item['data']
                    if d.get('cName'):          # 空壳（id 不存在）过滤
                        out[pid] = d
            time.sleep(self.throttle)
        return out

    # ---------------- 本地存储 ----------------

    def _init_db(self):
        con = sqlite3.connect(self.db_path)
        con.execute('''CREATE TABLE IF NOT EXISTS products(
            pid TEXT PRIMARY KEY, name TEXT, brand TEXT, category TEXT,
            code TEXT, barcode TEXT, sale_price REAL, mark_price REAL,
            suggest_price REAL, sales INTEGER, stock INTEGER,
            on_sale_date TEXT, tags TEXT, album_urls TEXT, desc_html TEXT,
            detail_url TEXT, raw TEXT, synced_at TEXT)''')
        con.commit()
        con.close()

    @staticmethod
    def category_of(name):
        for kw in ['冰箱', '洗衣机', '空调', '电视', '热水器', '油烟机', '燃气灶', '洗碗机',
                   '净水', '消毒柜', '电饭煲', '电压力锅', '微波炉', '破壁', '风扇', '冷柜',
                   '蒸箱', '烤箱', '酒柜']:
            if kw in name:
                return kw
        return '其他'

    @staticmethod
    def extract_model(name):
        """从商品名提取型号（BCD-xxx / XQG100-xxx / KFR-35GW 等）"""
        for pat in MODEL_PATTERNS:
            m = re.search(pat, name, re.I)
            if m:
                return m.group(0).upper().replace(' ', '-')
        return ''

    def sync(self, force=False):
        """增量同步：列表发现新增/变化 → 只对无详情或已变化的 pid 拉详情。
        返回统计 dict。force=True 全量重拉详情。"""
        t0 = time.time()
        con = sqlite3.connect(self.db_path)
        # 精简库不存 raw → 增量签名直接用表列（售价/库存），语义与原 raw 版一致
        known = {row[0] for row in
                 con.execute('SELECT pid FROM products').fetchall()}
        known_sig = {pid: hashlib.md5((str(sp) + str(iv)).encode()).hexdigest()
                     for pid, sp, iv in
                     con.execute('SELECT pid, sale_price, stock FROM products').fetchall()}

        listed = {}
        for it in self.iter_product_list():
            listed[it['pid']] = it
        log.info('列表拉取 %d 个商品', len(listed))

        todo = []
        for pid, it in listed.items():
            # ⚠ 列表接口没有 nSalePrice，售价字段就是 fSalePrice（1973 条实测）——
            #   原版拿 nSalePrice 算签名恒为空 → 每次都全量重拉，这里修正
            sig = hashlib.md5(str(it.get('fSalePrice', '')).encode() +
                              str(it.get('lInventoryCount', '')).encode()).hexdigest()
            if force or pid not in known or sig != known_sig.get(pid):
                todo.append(pid)

        added = updated = 0
        pids = list(todo)
        for i in range(0, len(pids), BATCH_SIZE):
            chunk = pids[i:i + BATCH_SIZE]
            details = self.get_details(chunk)
            for pid in chunk:
                d = details.get(pid)
                if not d:
                    continue
                li = listed.get(pid, {})
                albums = [al.get('lImgUrl') or al.get('cFolder') for al in (d.get('lsAlbums') or [])
                          if al.get('lImgUrl') or al.get('cFolder')]
                is_new = pid not in known
                # ModelLookup 精简版（2026-10-12）：desc_html/raw 不落盘——
                # 全量图文详情会让库膨胀到 3.9GB（1962 条 × ~2MB 富文本），
                # 软件只需要 编码/品牌/品类/售价/图集URL（几百 KB）；要看图文详情再按需拉
                con.execute('''INSERT OR REPLACE INTO products VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                            (pid, d.get('cName'), d.get('brand_Name'), self.category_of(d.get('cName', '')),
                             d.get('cCode'), d.get('cBarCode'), d.get('fSalePrice'), d.get('fMarkPrice'),
                             d.get('fSuggestedPrice'), d.get('iSales'), d.get('lInventoryCount'),
                             (d.get('dOnSaleTime') or '')[:10],
                             json.dumps(d.get('lsTagRel'), ensure_ascii=False),
                             json.dumps(albums), '',
                             f'{BASE_URL}/detail?goods_id={pid}',
                             '', time.strftime('%Y-%m-%d %H:%M:%S')))
                added += is_new
                updated += not is_new
            log.info('详情进度 %d/%d', min(i + BATCH_SIZE, len(pids)), len(pids))

        con.commit()
        n = con.execute('SELECT COUNT(*) FROM products').fetchone()[0]
        con.close()
        stats = {'列表商品': len(listed), '新增': added, '更新': updated, '库内总数': n,
                 '耗时秒': round(time.time() - t0, 1)}
        log.info('同步完成 %s', stats)
        return stats

    # ---------------- 查询 / 导出 ----------------

    def search(self, keyword, limit=50):
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT * FROM products WHERE name LIKE ? OR code LIKE ? OR barcode LIKE ? LIMIT ?",
            (f'%{keyword}%', f'%{keyword}%', f'%{keyword}%', limit)).fetchall()
        con.close()
        return [dict(r) for r in rows]

    def export_json(self, path):
        """导出统一格式（与 ModelLookup 其他品牌数据同构）"""
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        rows = con.execute('SELECT * FROM products ORDER BY brand, category, name').fetchall()
        con.close()
        out = []
        for r in rows:
            d = dict(r)
            out.append({
                '品牌': d['brand'] or '未知',
                '产品名': d['name'],
                '型号': self.extract_model(d['name'] or ''),
                '品类': d['category'],
                '商品编码': d['code'],
                '条码': d['barcode'],
                '售价': d['sale_price'], '划线价': d['mark_price'], '建议价': d['suggest_price'],
                '销量': d['sales'], '库存': d['stock'],
                '上架日期': d['on_sale_date'],
                '图片': json.loads(d['album_urls'] or '[]'),
                '图文详情HTML': d['desc_html'] or '',
                '来源URL': d['detail_url'],
                '数据来源': '欣盛臻选电器商城（用友umall后端，官方接口）',
            })
        json.dump(out, open(path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        return len(out)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    ds = XinshengDataSource()
    print(ds.sync())
    print('搜索"冰箱"样例:', [r['name'][:30] for r in ds.search('冰箱', 5)])
    print('导出:', ds.export_json(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'xinsheng_unified.json')), '条')
