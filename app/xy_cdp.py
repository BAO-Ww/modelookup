# -*- coding: utf-8 -*-
"""goofish 内嵌页自动化（2026-10-17）
====================================
背景：闲鱼卖家中心（goofish）是跨域页面，主窗口的 JS 够不着内嵌 iframe——
「快速进入」选号页得用户手点、菜单路由（宝贝管理真实 hash）拿不到。
解法：WebView2 开远程调试口（pywebview 的 REMOTE_DEBUGGING_PORT），
本模块从 127.0.0.1 走 CDP 直接在 goofish 文档里执行 JS：

  ① autoclick：每 2.5s 扫一遍 goofish 相关目标（独立窗口/内嵌 iframe），

     页面上有「快速进入」就替用户点掉（用户要求：登录选号页不用手点）；
  ② dump_menu()：在已登录的 goofish 页面上下文里调 mtop 菜单接口，
     拿全量菜单配置（含各页面的真实 hash 路由）——/api/xymenu 用它。

安全边界：只连 127.0.0.1 的调试口；websocket-client 缺失时全部静默降级
（自动点和路由探测失效，软件其余功能不受影响）。"""
import json
import os
import threading
import time
import urllib.request


def _port():
    """调试口：main.py 启动时定好写进 ML_CDP_PORT（默认 pid 派生防多进程撞口）"""
    try:
        return int(os.environ.get("ML_CDP_PORT") or 9333)
    except Exception:
        return 9333


def _http(path, timeout=2.5):
    r = urllib.request.urlopen("http://127.0.0.1:%d%s" % (_port(), path), timeout=timeout)
    return r.read().decode("utf-8", "ignore")


_URL_KW = ("goofish", "taobao", "havana", "m.goofish")


def targets(kinds=("page", "iframe", "webview")):
    """列出 goofish 相关的调试目标（独立窗口=page，内嵌页=iframe/OOPIF）"""
    try:
        arr = json.loads(_http("/json/list"))
    except Exception:
        return []
    out = []
    for t in arr:
        u = t.get("url") or ""
        if not t.get("webSocketDebuggerUrl"):
            continue
        if not any(k in u for k in _URL_KW):
            continue
        if kinds and t.get("type") not in kinds:
            continue
        out.append(t)
    return out


def _ws_eval(ws_url, expr, timeout=5.0):
    """连一个调试目标执行 JS（awaitPromise 开着，Promise 结果直接取回）。
    suppress_origin：Chromium 默认拒带 Origin 的 WS（403），本机回环直连不带即可"""
    import websocket  # websocket-client，懒加载（缺失则整体降级）
    ws = websocket.create_connection(ws_url, timeout=timeout, suppress_origin=True)
    try:
        ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                            "params": {"expression": expr, "returnByValue": True,
                                       "awaitPromise": True}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = json.loads(ws.recv())
            if msg.get("id") == 1:
                r = (msg.get("result") or {}).get("result") or {}
                if "value" in r:
                    return r.get("value")
                return None
    finally:
        try:
            ws.close()
        except Exception:
            pass
    return None


# 替点「快速进入」（选号页）：找文字精确匹配的最内层元素，点它 + 它的可点祖先
_CLICK_JS = """(function(){
  var want = ['快速进入'];
  var els = document.querySelectorAll('button,a,[role=button],span,div,p');
  for (var i = 0; i < els.length; i++) {
    var e = els[i];
    if (e.children && e.children.length) continue;
    var t = (e.textContent || '').trim();
    if (want.indexOf(t) < 0) continue;
    var c = e.closest ? e.closest('button,a,[role=button]') : null;
    (c || e).click();
    return 'clicked:' + t;
  }
  return 'no';
})()"""


def autoclick_once():
    """扫一遍所有 goofish 目标：① 见到「快速进入」就点；② 顺手记 hash（路由嗅探）"""
    n = 0
    for t in targets():
        n += 1
        try:
            _ws_eval(t["webSocketDebuggerUrl"], _CLICK_JS, timeout=3.0)
        except Exception:
            pass
        try:
            h = _ws_eval(t["webSocketDebuggerUrl"], "location.hash", timeout=3.0)
            if h:
                _note_route(str(h))
        except Exception:
            pass
    return n


_ROUTES_SEEN = []          # 本进程内见过的 goofish hash（去重、新的在前）
_ROUTES_SEEN_MAX = 60


def _note_route(h):
    """路由嗅探：用户在内嵌页里点到哪，就记到哪（/api/xymenu 的 routes_seen 返回）"""
    h = (h or "").strip()
    if not h.startswith("#") or h in _ROUTES_SEEN:
        return
    _ROUTES_SEEN.insert(0, h)
    del _ROUTES_SEEN[_ROUTES_SEEN_MAX:]
    try:
        base = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                            "ModelLookup")
        os.makedirs(base, exist_ok=True)
        with open(os.path.join(base, "_xy_routes.log"), "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), h))
    except Exception:
        pass


def routes_seen():
    return list(_ROUTES_SEEN)


def start_autoclick():
    """后台守护线程：每 2.5s 扫一轮（幂等；没点中是常态，点中即完成选号）"""
    def _loop():
        while True:
            try:
                autoclick_once()
            except Exception:
                pass
            time.sleep(2.5)
    th = threading.Thread(target=_loop, daemon=True, name="xy-autoclick")
    th.start()
    return th


# 在已登录 goofish 页面里调 mtop 菜单接口（lib-mtop 自动带 token/签名）
_MENU_JS = """new Promise(function(resolve){
  try{
    var m = null;
    try { m = (window.lib && window.lib.mtop) || null; } catch(e){}
    if ((!m || !m.request) && window.mtop && window.mtop.request) m = window.mtop;
    if (!m || !m.request) { resolve('nomtop'); return; }
    var done = false;
    var T = setTimeout(function(){ if(!done){ done = true; resolve('timeout'); } }, 9000);
    m.request({
      api: 'mtop.alibaba.idle.seller.platform.sys.menu.query', v: '1.0', data: {},
      success: function(r){ if(done) return; done = true; clearTimeout(T);
        try { resolve(JSON.stringify(r)); } catch(e){ resolve('ser-err'); } },
      fail: function(e){ if(done) return; done = true; clearTimeout(T);
        try { resolve('ERR:' + JSON.stringify(e)); } catch(e2){ resolve('ERR:?'); } }
    });
  }catch(e){ resolve('EXC:' + e); }
})"""


def dump_menu(timeout=12.0):
    """在第一个 goofish 页面目标里调菜单接口，返回原始 JSON 字符串/错误说明"""
    for t in targets(kinds=("page", "webview", None)):
        try:
            v = _ws_eval(t["webSocketDebuggerUrl"], _MENU_JS, timeout=timeout)
            if v and v not in ("nomtop",):
                return v
        except Exception:
            continue
    # 没有独立窗口时，内嵌 iframe（OOPIF）也试一把
    for t in targets(kinds=("iframe",)):
        try:
            v = _ws_eval(t["webSocketDebuggerUrl"], _MENU_JS, timeout=timeout)
            if v and v != "nomtop":
                return v
        except Exception:
            continue
    return ""
