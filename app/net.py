# -*- coding: utf-8 -*-
"""HTTP 基础（2026-10-11 拆分一期：从 main.py 原样搬出，纯搬运零行为变化）。
依赖方向：… → payload → net（只依赖标准库 + config）。"""
import os
import threading
import time
import ssl
import urllib.error
import urllib.request

from config import UA


def _fresh_opener(context=None, proxy=None):
    """每次请求现建 opener、实时读代理（2026-10-26 死代理根治）：
    Python 的 urlopen() 首次调用会把当时的系统代理设置缓存进全局 opener，
    之后再也不重读——FlClash 开着时启动软件、随后关掉 FlClash，软件就把
    所有出网请求永远撞在那个已死的 127.0.0.1:7890 上（WinError 10061 积极拒绝，
    2026-10-26 实锤：进程 70 秒 315 次连死端口）。改为每次请求 getproxies()
    实时取当前代理：代理开着就走、关了就直连、换端口自动跟随。
    proxy 显式传值（如 settings.json 的 update_proxy=http://127.0.0.1:7891）
    时覆盖系统代理——更新下载挂节点加速（2026-10-29 用户令）。"""
    if context is None:
        context = ssl.create_default_context()
    ph = (urllib.request.ProxyHandler({"http": proxy, "https": proxy})
          if proxy else urllib.request.ProxyHandler(urllib.request.getproxies()))
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=context),
        ph,
    )


def urlopen_ssl(req, timeout=90, proxy=None):
    """带 SSL 兜底的 urlopen（2026-10-25 Mac 实锤）：
    Mac 打包环境没有系统钥匙串根证书，默认上下文必报 CERTIFICATE_VERIFY_FAILED
    （urllib 会把 SSL 错误包进 URLError.reason，直接 except ssl.SSLError 是接不住的）。
    顺序：默认上下文 → 失败且是 SSL 错 → 不校验重试（目标是我们自己的 worker/大厂直链，
    登录态/签名鉴权都在应用层，传输层不校验可接受）。Windows 正常环境永远走第一路，行为不变。
    2026-10-26 起三路全部走 _fresh_opener（实时代理，不吃全局缓存 opener）。
    proxy 参数透传 _fresh_opener（更新下载挂节点）。"""
    try:
        return _fresh_opener(proxy=proxy).open(req, timeout=timeout)
    except urllib.error.URLError as e:
        if isinstance(getattr(e, "reason", None), ssl.SSLError):
            return _fresh_opener(ssl._create_unverified_context(),
                                 proxy=proxy).open(req, timeout=timeout)
        raise
    except ssl.SSLError:
        return _fresh_opener(ssl._create_unverified_context(),
                             proxy=proxy).open(req, timeout=timeout)


def http_get(url, headers=None, timeout=180):
    hdrs = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, headers=hdrs)
    for ctx in (ssl.create_default_context(), ssl._create_unverified_context()):
        try:
            with _fresh_opener(ctx).open(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except ssl.SSLError:
            continue
        except urllib.error.URLError as e:
            # 2026-10-25 Mac 实锤：SSL 错误被包进 URLError，老代码 except ssl.SSLError 接不住
            if isinstance(getattr(e, "reason", None), ssl.SSLError):
                continue
            raise
    raise RuntimeError("网络请求失败：" + url[:80])


def fetch_xlsx_by_url(url, log):
    code, raw = http_get(url, timeout=300)
    if raw[:2] == b"PK":
        log("已下载 %.2f MB" % (len(raw) / 1048576.0))
        return raw
    raise RuntimeError("直链返回的不是文件（HTTP %s）" % code)


def dl_parallel(url, headers, dest, proxy=None, nchunks=8, progress=None,
                min_size=4 * 1024 * 1024, log=None):
    """并发分块下载（2026-10-29 用户令「我不要装不装 Clash，我就是要下载快」）：
    单线程瓶颈实测 0.06-0.19MB/s，8 路并发合计约 0.5-0.8MB/s（约 5 倍）。
    流程：先发 Range: bytes=0-0 探测——206 且 Content-Range 给出总大小时按块并发；
    否则（源不支持分段/文件太小）退回单流下载，行为与旧版完全一致。
    每块独立失败重试 3 次，任一块拿不下 → 删半成品整体退回单流（SHA-256 校验在
    调用方，最终完整性永远有兜底）。返回 (写入字节数, 总大小或0)。
    progress(got, total) 可选回调（更新器拿来刷百分比）。"""

    def _open_range(start, end):
        req = urllib.request.Request(url, headers=dict(headers or {}))
        req.add_header("Range", "bytes=%d-%d" % (start, end))
        return urlopen_ssl(req, timeout=120, proxy=proxy)

    def _single(progress_on=True):
        """单流全量下载（探测失败/并发翻车时的兜底）。"""
        got = 0
        with _plain(url, headers) as r:
            t = int(r.headers.get("Content-Length") or 0)
            with open(dest, "wb") as f:
                while True:
                    chunk = r.read(262144)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    if progress and progress_on:
                        progress(got, t)
            return got, t

    # ---- 探测：支不支持分段 ----
    total = 0
    try:
        with _open_range(0, 0) as r:
            if r.status == 206:
                cr = r.headers.get("Content-Range") or ""
                total = int(cr.rsplit("/", 1)[-1]) if "/" in cr else 0
    except Exception:
        total = 0
    if not total or total < min_size:
        if log:
            log("下载走单流（源%s分段请求）" % ("太小不走" if total else "不支持"))
        return _single()

    # ---- 并发分块 ----
    if log:
        log("下载走 %d 路并发（%.1f MB）" % (nchunks, total / 1048576.0))
    bounds, step = [], total // nchunks
    for i in range(nchunks):
        s = i * step
        e = (total - 1) if i == nchunks - 1 else (s + step - 1)
        bounds.append((s, e))
    lock = threading.Lock()
    got_all = [0]            # 已确认落盘字节（并发进度）
    okflags = [False] * nchunks

    def _progress_delta(n):
        with lock:
            got_all[0] += n
            if progress:
                progress(got_all[0], total)

    def _grab(idx):
        s, e = bounds[idx]
        want = e - s + 1
        for attempt in range(3):
            got = 0
            try:
                with _open_range(s, e) as r:
                    with open(dest, "r+b") as f:
                        f.seek(s)
                        while got < want:
                            chunk = r.read(min(262144, want - got))
                            if not chunk:
                                break
                            f.write(chunk)
                            got += len(chunk)
                _progress_delta(got if attempt == 0 else 0)   # 重试不重复计进度
            except Exception:
                time.sleep(1 + attempt)
                continue
            if got == want:
                with lock:
                    okflags[idx] = True
                return
            # 半截：回退该块起点重试（进度按差额补）
            _progress_delta(0)
        # 全侧重试后仍差一点：把已写部分进度补齐后放弃
        if got:
            _progress_delta(0)
    try:
        with open(dest, "wb") as f:
            f.truncate(total)
    except OSError:
        return _single()
    ths = []
    for i in range(nchunks):
        t = threading.Thread(target=_grab, args=(i,))
        ths.append(t)
        t.start()
    for t in ths:
        t.join()
    if all(okflags):
        return total, total
    if log:
        log("并发下载有块失败，退回单流重下")
    try:
        os.remove(dest)
    except OSError:
        pass
    return _single()


def _plain(url, headers):
    """不带 Range 的普通 GET（单流兜底用）。"""
    req = urllib.request.Request(url, headers=dict(headers or {}))
    return urlopen_ssl(req, timeout=900)
