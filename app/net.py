# -*- coding: utf-8 -*-
"""HTTP 基础（2026-10-11 拆分一期：从 main.py 原样搬出，纯搬运零行为变化）。
依赖方向：… → payload → net（只依赖标准库 + config）。"""
import ssl
import urllib.error
import urllib.request

from config import UA


def _fresh_opener(context=None):
    """每次请求现建 opener、实时读代理（2026-10-26 死代理根治）：
    Python 的 urlopen() 首次调用会把当时的系统代理设置缓存进全局 opener，
    之后再也不重读——FlClash 开着时启动软件、随后关掉 FlClash，软件就把
    所有出网请求永远撞在那个已死的 127.0.0.1:7890 上（WinError 10061 积极拒绝，
    2026-10-26 实锤：进程 70 秒 315 次连死端口）。改为每次请求 getproxies()
    实时取当前代理：代理开着就走、关了就直连、换端口自动跟随。"""
    if context is None:
        context = ssl.create_default_context()
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=context),
        urllib.request.ProxyHandler(urllib.request.getproxies()),
    )


def urlopen_ssl(req, timeout=90):
    """带 SSL 兜底的 urlopen（2026-10-25 Mac 实锤）：
    Mac 打包环境没有系统钥匙串根证书，默认上下文必报 CERTIFICATE_VERIFY_FAILED
    （urllib 会把 SSL 错误包进 URLError.reason，直接 except ssl.SSLError 是接不住的）。
    顺序：默认上下文 → 失败且是 SSL 错 → 不校验重试（目标是我们自己的 worker/大厂直链，
    登录态/签名鉴权都在应用层，传输层不校验可接受）。Windows 正常环境永远走第一路，行为不变。
    2026-10-26 起三路全部走 _fresh_opener（实时代理，不吃全局缓存 opener）。"""
    try:
        return _fresh_opener().open(req, timeout=timeout)
    except urllib.error.URLError as e:
        if isinstance(getattr(e, "reason", None), ssl.SSLError):
            return _fresh_opener(ssl._create_unverified_context()).open(req, timeout=timeout)
        raise
    except ssl.SSLError:
        return _fresh_opener(ssl._create_unverified_context()).open(req, timeout=timeout)


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
