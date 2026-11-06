# -*- coding: utf-8 -*-
"""云端中转（Cloudflare Worker）客户端（2026-10-11 拆分一期：从 main.py 原样搬出，纯搬运零行为变化）。
登录态镜像 / cloud_req / 库存与表格云同步 / 告警上报轮询 / 版本比较。
依赖方向：… → payload → net → cloud（cloud 在 payload 之上：sync 要触发 rebuild）。"""
import base64
import io
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

import state
from config import CLOUD_HOST, _CLOUD_UA
from net import urlopen_ssl
from paths import wp
from payload import rebuild


def client_log_upload(reason):
    """客户端诊断上送（2026-10-25 Mac 空数据实锤用的）：把本机关键文件清单 +
    状态 + app.log 尾部传到 worker /clientlog（KV 每用户一份覆盖写）。
    失败静默——诊断通道绝不能反过来制造新问题。"""
    try:
        d = wp("")
        files = {}
        for fn in ("auth_token.json", "xlsx_ts.txt", "data.xlsx", "price.xlsx",
                   "subsidy.xlsx", "payload.json", "stocklive.json", "app.log"):
            try:
                p = os.path.join(d, fn)
                if os.path.isfile(p):
                    files[fn] = os.path.getsize(p)
            except OSError:
                pass
        tail = ""
        lp = os.path.join(d, "app.log")
        if os.path.isfile(lp):
            tail = io.open(lp, encoding="utf-8", errors="ignore").read()[-6000:]
        st = state.read_state()
        cloud_req("/clientlog", "POST",
                  {"reason": reason, "files": files,
                   "state": {k: st.get(k) for k in
                             ("status", "msg", "price_at", "stock_at", "data_alert")},
                   "log": tail},
                  timeout=30)
    except Exception:
        pass


def _cloud_token():
    """页面登录态镜像，调 worker 用。auth_token.json 兼容两种格式：
    页面 /api/token POST 的是 JSON {"u","token","role"...}；裸 token 字符串也认。"""
    try:
        with io.open(wp("auth_token.json"), encoding="utf-8") as f:
            raw = f.read().strip()
        if not raw:
            return ""
        try:
            d = json.loads(raw)
            if isinstance(d, dict):
                return str(d.get("token") or "").strip()
        except Exception:
            pass
        return raw
    except Exception:
        return ""


_PROXY_BAD = [0.0]          # 代理连挂的话 5 分钟内不再试（免得每个请求都白等一次）


def cloud_req(path, method="GET", obj=None, timeout=90, progress=None):
    """调云端 worker（带页面登录态）。返回 (dict|None, err|None)。
    2026-11-01：settings.json 配了 net_proxy（"auto"=自动探测本机代理端口）就先走代理，
    代理网络层失败自动退直连（HTTP 4xx/5xx 不算网络失败，照常返回不重试）。
    2026-11-05：progress(got_bytes, total_bytes) 边下边报——大包（表格 20MB）不再干转圈。"""
    tok = _cloud_token()
    if not tok:
        return None, "还没有登录态（页面没登录过）"
    body = json.dumps(obj).encode("utf-8") if obj is not None else None
    px = None
    try:
        from paths import net_proxy as _npx
        px = _npx()
    except Exception:
        px = None
    if px and time.time() - _PROXY_BAD[0] < 300:
        px = None

    def _read(r):
        if not progress:
            return json.loads(r.read().decode("utf-8"))
        try:
            total = int(r.headers.get("Content-Length") or 0)
        except Exception:
            total = 0
        buf, got = [], 0
        while True:
            chunk = r.read(262144)
            if not chunk:
                break
            buf.append(chunk)
            got += len(chunk)
            try:
                progress(got, total)
            except Exception:
                pass
        return json.loads(b"".join(buf).decode("utf-8"))

    def _do(use_px):
        req = urllib.request.Request(CLOUD_HOST + path, data=body, method=method)
        req.add_header("X-Token", tok)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", _CLOUD_UA)
        req.add_header("Origin", CLOUD_HOST)
        req.add_header("Referer", CLOUD_HOST + "/")
        with urlopen_ssl(req, timeout=timeout, proxy=use_px) as r:
            return _read(r)

    last_err = None
    for use_px in ((px, None) if px else (None,)):
        try:
            j = _do(use_px)
            # 2026-10-31 云端写入配额（KV 每天 1000 次写上限）：非 GET 记一次，面板上看得见
            if method != "GET":
                try:
                    import extras
                    extras.quota_bump(1)
                except Exception:
                    pass
            return j, None
        except urllib.error.HTTPError as e:
            try:
                j = json.loads(e.read().decode("utf-8"))
                return (j, None) if isinstance(j, dict) else (None, "HTTP %d" % e.code)
            except Exception:
                return None, "HTTP %d" % e.code
        except Exception as e:
            last_err = e
            if use_px:
                _PROXY_BAD[0] = time.time()
                try:
                    state.LOG.append("云端走代理失败（%s），退直连重试" % str(e)[:80])
                except Exception:
                    pass
    return None, str(last_err)


def yy_alert_push(msg, log=None):
    """用友自动登录失败 → 上报云端告警（管理号软件轮询 /yyalert 能看到）。
    2026-10-08 用户口径：密码不对了要通知管理账户。失败只记日志，不影响主流程。"""
    def _f():
        j, err = cloud_req("/yyalert", "POST",
                           {"msg": "用友登录失败：%s" % msg}, timeout=30)
        if err or not j or not j.get("ok"):
            (log or state.LOG.append)("⚠ 告警上报云端失败：%s" % (err or (j or {}).get("msg")))
        else:
            (log or state.LOG.append)("已上报云端告警（管理号会收到）：%s" % msg)
    try:
        threading.Thread(target=_f, daemon=True).start()
    except Exception:
        pass


def yy_alert_poll():
    """管理号轮询云端告警：有新告警（ts 比已知的大）→ 存进 STATE，
    页面 30 秒轮询 /api/state 时读 alert_ts/alert_msg 亮横幅。只管 admin。
    2026-10-10 两条规定（用户反馈：自己机器的告警弹回自己、重启后陈年告警又亮）：
    · 本机账号自己推的告警不收（自己推的自己必然知道，弹回纯打扰）；
    · 超过 30 分钟的陈旧告警不收（KV 里那条一直躺着，重启 STATE 归零就会重亮）。"""
    if state.PERM_STATE.get("role") != "admin":
        return
    j, err = cloud_req("/yyalert", "GET", timeout=30)
    if j and j.get("ok"):
        ts = int(j.get("ts") or 0)
        who = str(j.get("by") or "")
        if who and who == state.PERM_STATE.get("u"):
            return                       # 自己机器产生的，自己不用被提醒
        if ts and ts < time.time() - 1800:
            return                       # 超过 30 分钟的陈年告警不再亮牌
        if ts and ts > int(state.STATE.get("alert_ts") or 0):
            msg = str(j.get("msg") or "")
            state.set_state(alert_ts=ts, alert_msg=msg + ("（来自 %s）" % who if who else ""))
            state.LOG.append("🔔 用友告警：%s" % msg)


def stock_push_cloud(log):
    """管理号拉完用友 → 整包上传云端，全员共享。失败只记日志，不影响本机。"""
    with state.STOCK_LOCK:
        obj = {"ts": state.STOCK.get("ts"), "rows": state.STOCK.get("rows"), "stock": state.STOCK.get("stock"),
               "sbc": state.STOCK.get("sbc") or None,
               "mo2meta": state.STOCK.get("mo2meta") or None}
    j, err = cloud_req("/stockcloud", "POST", obj, timeout=180)
    if err or not j or not j.get("ok"):
        log("⚠ 库存上传云端失败（子账号拿不到这次的更新）：%s" % (err or (j or {}).get("msg")))
    else:
        log("库存已上传云端（%.1f MB），子账号自动共享" % (j.get("mb") or 0))


def _ts_key(ts):
    """时间戳统一成可字典序比较的键（2026-10-14 体检 P1-A1 跨年修复）：
    旧格式 %m-%d %H:%M 无年份，12 月的 "12-31" 和 1 月的 "01-02" 裸比较会判成
    「云端没更新」→ 库存同步从元旦起全部静默失效。旧格式一律视为最旧，
    第一次云端带新年份的数据来时就强制同步一次，之后双方都是新格式。"""
    s = str(ts or "")
    return s if (len(s) > 5 and s[4:5] == "-") else "0000-" + s


def stock_cloud_peek():
    """轻量探云端库存时间戳（给自动轮询用，不拉大数据）。
    返回云端 ts 字符串 / None（网络或数据问题）。"""
    j, err = cloud_req("/stockmeta", "GET", timeout=30)
    if j and j.get("ok"):
        return str(j.get("ts") or "")
    return None


def stock_pull_cloud(log, force=True):
    """子账号：不从用友拉（没有登录态），直接取管理号共享的云端库存。
    force=False（自动轮询用）：云端 ts 跟本地一样就什么都不做（省流量、不打扰）；
    变了才拉全量、落盘、并 bump rev 让页面无感刷新。"""
    if not force:
        cloud_ts = stock_cloud_peek()
        if not cloud_ts:
            return                        # 探测失败：静默，下轮再说
        with state.STOCK_LOCK:
            local_ts = state.STOCK.get("ts") or ""
        if _ts_key(cloud_ts) <= _ts_key(local_ts):
            # 2026-11-05：没新数据也把「云端最新抓取时间」显示出来（管理号/子账号同一个时间）
            try:
                if (state.STATE.get("stock_at") or "") != cloud_ts:
                    state.set_state(stock_at=cloud_ts)
            except Exception:
                pass
            return                        # 云端没有比本地更新的东西（2026-10-09 改成比先后；2026-10-14 兼容跨年）
        log("云端库存有更新（%s → %s），自动同步…" % (local_ts or "无", cloud_ts))
    log("正在从云端同步库存（管理号共享的用友实时数据）…")
    state.set_state("running", "正在从云端同步库存…")
    j, err = cloud_req("/stockcloud", "GET", timeout=180)
    if err:
        state.set_state("error", "云端库存同步失败：%s" % err)
        return
    if not j or not j.get("ok") or not isinstance(j.get("stock"), dict):
        state.set_state("error", (j or {}).get("msg") or "云端还没有库存数据（等管理号更新一次）")
        return
    with state.STOCK_LOCK:
        state.STOCK = {"ts": j.get("ts"), "rows": j.get("rows") or 0, "stock": j["stock"],
                       "sbc": j.get("sbc") or None, "mo2meta": j.get("mo2meta") or None}
    state.stock_save()
    ts = j.get("ts") or ""
    log("云端库存同步完成：%s 行（%s）" % (j.get("rows") or 0, ts))
    state._STK_DETAIL[0] = "从云端同步 %s 行（管理号共享）" % (j.get("rows") or 0)
    if force:
        state.set_state("done", "库存已同步（云端共享 · %s）" % ts, stock_at=ts)
    else:
        # 自动轮询：bump rev 触发页面无感重载（reloadAll 会重新取 /api/stock）
        state.set_state("done", "库存已自动同步（云端共享 · %s）" % ts,
                        stock_at=ts, rev=state.STATE.get("rev", 0) + 1)


def _ver_gt(a, b):
    """版本号比较：'2.7.2' > '2.7.1'。"""
    def nums(s):
        return [int(x) for x in re.findall(r"\d+", str(s) or "")]
    try:
        return nums(a) > nums(b)
    except Exception:
        return False


# ---------------- 数据链路哨兵（2026-10-24）----------------
# 背景：kdocs/用友 cron 挂了、或云端不可达时，软件只是安静地显示旧数据，没人知道。
# 信号：① /xlsxmeta 探测连续失败（网络/接口挂）；② xlsxlive.up（kdocs 每小时 :40 一轮，
#   CI 内容没变也走 /xlsxping 报到）停在 3 小时前；③ stocklive.up（用友每 30 分钟一轮）
#   停在 2 小时前。各自带营业窗口保护（开跑几小时后才报，防清晨误报）。
# 写进 state.data_alert → 前端 30 秒轮询亮红条；恢复自动清。写额度零增加（不进 KV）。
_PROBE_FAIL = [0]                    # [n] 用列表躲 global
def _in_business(now=None):
    """告警窗口：kdocs 8:40-23:40（11:40 后才报）∪ 用友 10:30-23:00（13:30 后才报）"""
    t = time.localtime(now if now is not None else time.time())
    hm = t.tm_hour * 60 + t.tm_min
    kd = 8*60+40 <= hm <= 23*60+40 and hm >= 11*60+40
    yy = 10*60+30 <= hm <= 23*60 and hm >= 13*60+30
    return kd, yy

def _sentinel_put(msg):
    from state import set_state as _ss, STATE as _ST
    if _ST.get("data_alert", "") != msg:
        _ss(data_alert=msg)

def _stale_msg(tag, up_ms, hours):
    import time as _t
    return "%s定时任务自 %s 起没有成功跑批（可能停了，现在显示的是旧数据）" % (
        tag, _t.strftime("%m-%d %H:%M", _t.localtime(up_ms / 1000)))

def _data_sentinel(j, err, ts):
    import time as _t
    from state import STATE as _ST
    cur = _ST.get("data_alert", "")
    if err or not j:                 # 探测失败：连续 3 次（约 3 分钟）才报，抖一下不吓人
        _PROBE_FAIL[0] += 1
        if _PROBE_FAIL[0] >= 3 and not cur:
            _sentinel_put("云端数据探测连续 %d 次失败（%s），云端可能不可达" % (_PROBE_FAIL[0], err or "无响应"))
        return
    _PROBE_FAIL[0] = 0
    _, yyok = _in_business()
    msgs = []
    # ① kdocs 跑批停摆（up 停 3h+ 且在告警窗口）
    try:
        up = int((j.get("up") if isinstance(j, dict) else 0) or 0)
        if up and _t.time() * 1000 - up > 3 * 3600 * 1000 and yyok:
            msgs.append(_stale_msg("金山表格", up, 3))
    except Exception:
        pass
    # ② 用友跑批停摆（up 停 2h+ 且在告警窗口；管理号机器才探得到——子账号 /stockmeta 也开放，一并看）
    try:
        js, errs = cloud_req("/stockmeta", "GET", timeout=30)
        up2 = int((js or {}).get("up") or 0) if (js and js.get("ok")) else 0
        if up2 and _t.time() * 1000 - up2 > 2 * 3600 * 1000 and yyok:
            msgs.append(_stale_msg("用友库存", up2, 2))
    except Exception:
        pass
    # ③ GitHub 令牌临期提醒（2026-11-06 用户令「快过期了要通知我啊」）：本机触发器
    #   每次调 GitHub 都会把令牌过期时间落到 pat_expiry.json；剩 7 天内亮提醒——
    #   令牌一过期三层触发全瘫（11-05 实锤），必须提前换。
    try:
        _pe = json.loads(io.open(wp("pat_expiry.json"), encoding="utf-8").read() or "{}")
        _exp = float(_pe.get("utc_ts") or 0)
        if _exp:
            _left = (_exp - _t.time()) / 86400.0
            if 0 < _left <= 7:
                msgs.append("GitHub 令牌 %s 过期（还剩 %.0f 天），到期抓取会全停——提前在 GitHub 重新生成并更新" % (
                    _t.strftime("%m-%d", _t.localtime(_exp)), _left))
    except Exception:
        pass
    if msgs:
        _sentinel_put("；".join(msgs))
    elif cur:
        _sentinel_put("")


_sync_fail = {"ts": "", "at": 0.0}   # 2026-10-26：同步失败退避记录（同 ts 10 分钟内不重试）

_XLSX_FILES = ("data.xlsx", "price.xlsx", "subsidy.xlsx")   # 云端整包里的三件套


def _fts_load():
    """本机每件表格的内容指纹（2026-11-05 增量同步用）。读不到返回 {}——那时会全拉一次建基线。"""
    try:
        d = json.loads(io.open(wp("xlsx_fts.json"), encoding="utf-8").read() or "{}")
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _fts_save(d):
    try:
        io.open(wp("xlsx_fts.json"), "w", encoding="utf-8").write(json.dumps(d, ensure_ascii=False))
    except Exception:
        pass


def _cloud_time(j, fallback=""):
    """云端最近一次抓取时间（/xlsxmeta 的 up，毫秒时间戳）→ "YYYY-MM-DD HH:MM"。
    2026-11-05 用户令：不管子账号还是管理号，侧栏显示的都必须是**云端抓取时间**，
    不是本机重建时间（否则同一份数据两台机显示两个时间，子账号看着像没更新）。"""
    try:
        up = int((j or {}).get("up") or 0)
    except Exception:
        up = 0
    if up > 0:
        try:
            return time.strftime("%Y-%m-%d %H:%M", time.localtime(up / 1000.0))
        except Exception:
            pass
    return fallback


def sync_xlsx_cloud(log, touch=False):
    """从云端同步金山表格（2026-10-09 v2.8 全云端口径；2026-11-05 改增量拉取）：
    轻探 /xlsxmeta（ts=整包哈希，fts=每件表格各自的内容指纹）→
    **只拉指纹变了/本地缺的那几件**（/xlsxcloud?f=price）→ 落盘 → 重建 → 记 ts。
    原来任一文件变就整包重拉 20.9MB（价格表每小时变一次、补贴表 11.9MB 几乎不变），
    现在日常只拉 0.38MB；晚高峰子账号那台不再卡几分钟。
    touch=True（手动「全部更新」）：即使内容没变也算「云端确认过最新」，刷新侧栏时间。
    返回："synced"（同步并重建完成）/ "unchanged"（云端没新东西）/ 其他 = 错误信息。"""
    try:
        j, err = cloud_req("/xlsxmeta", "GET", timeout=30)
        ts = str((j or {}).get("ts") or "") if (j and j.get("ok")) else ""
        _data_sentinel(j, err, ts)          # 数据链路哨兵（2026-10-24）：异常亮牌，正常清牌
        if not ts:
            return "unchanged"           # 云端还没有数据 / 探测失败：当没新东西
        # 2026-11-05：时间一律用云端抓取时间（管理号/子账号显示同一个）
        cat = _cloud_time(j)
        local = ""
        try:
            local = io.open(wp("xlsx_ts.txt"), encoding="utf-8").read().strip()
        except Exception:
            pass
        if ts == local:
            if cat or touch:
                state.set_state(price_at=cat or time.strftime("%Y-%m-%d %H:%M"))
            return "unchanged"
        # 2026-10-26 修：同一 ts 失败过 → 退避 10 分钟。此前失败路径 ts 永不更新，
        # 每 60s 重拉重建且注定再失败（金山表改版=无限白烧云端流量）。
        if ts == _sync_fail["ts"] and time.time() - _sync_fail["at"] < 600:
            return "unchanged"
        # ---- 2026-11-05 增量：先算「这次到底要拉哪几件」----
        cfts = j.get("fts") if isinstance(j.get("fts"), dict) else {}
        lfts = _fts_load()
        if cfts and lfts:
            need = [n for n in _XLSX_FILES
                    if cfts.get(n) and (lfts.get(n) != cfts.get(n) or not os.path.isfile(wp(n)))]
            if not need:
                # ts 变了但三件内容都没变（云端重传了同一份）→ 只记 ts，不白拉、不重建
                with io.open(wp("xlsx_ts.txt"), "w", encoding="utf-8") as f:
                    f.write(ts)
                if cat or touch:
                    state.set_state(price_at=cat or time.strftime("%Y-%m-%d %H:%M"))
                return "unchanged"
        else:
            # 老云端没有 fts / 本机首次（升级上来没基线）→ 全拉一次，顺手把基线建起来
            need = list(_XLSX_FILES)
        if not state._update_lock.acquire(blocking=False):
            return "busy"                # 别的更新正在跑，这轮先不动
        try:
            log("☁ 云端表格有更新（%s…），需要拉：%s" % (ts[:8], ", ".join(need)))
            state.set_state("running", "正在从云端同步表格数据…")
            # 只拉几件就带 ?f=，三件全要才走整包（老云端/排障仍支持）
            q = "/xlsxcloud"
            if len(need) < len(_XLSX_FILES):
                q += "?f=" + ",".join(n[:-5] for n in need)

            _pg = {"t": 0.0}

            def _progress(got, total):
                now = time.time()
                if now - _pg["t"] < 0.8 and (not total or got < total):
                    return              # 限频 0.8s，别把状态区刷爆
                _pg["t"] = now
                try:
                    if total:
                        state.set_state("running", "正在从云端同步表格数据… %.1f/%.1f MB" % (
                            got / 1048576.0, total / 1048576.0))
                    else:
                        state.set_state("running", "正在从云端同步表格数据… 已收 %.1f MB" % (got / 1048576.0))
                except Exception:
                    pass

            # 2026-10-31 晚：晚高峰跨境链路极慢（85 字节的 /xlsxmeta 都要 3.2s），
            # 300s 超时把能拉完的也判死 → 提到 600s，且失败自动重试一次再宣判。
            j2, err2 = cloud_req(q, "GET", timeout=600, progress=_progress)
            if err2 or not j2 or not j2.get("ok"):
                state.set_state("running", "云端响应慢，正在重试同步表格…")
                j2, err2 = cloud_req(q, "GET", timeout=600, progress=_progress)
            if err2 or not j2 or not j2.get("ok"):
                msg = "云端表格同步失败：%s" % (err2 or (j2 or {}).get("msg"))
                _sync_fail.update(ts=ts, at=time.time())   # 退避，防 60s 无限重拉
                state.set_state("error", msg)
                return msg
            files = j2.get("files") or {}
            got_fts = j2.get("fts") if isinstance(j2.get("fts"), dict) else {}
            saved = []
            for name in _XLSX_FILES:
                b64 = files.get(name)
                if not b64:
                    continue
                raw = base64.b64decode(b64)
                if raw[:2] != b"PK":
                    raise RuntimeError("%s 不是有效的表格文件" % name)
                with open(wp(name), "wb") as f:
                    f.write(raw)
                saved.append(name)
            if not saved:
                return "unchanged"
            state.set_state("running", "正在重建数据（云端表格）…")
            rebuild(log)
            with io.open(wp("xlsx_ts.txt"), "w", encoding="utf-8") as f:
                f.write(ts)
            # 指纹跟着更新：只记录「这次真下下来」的那几件（缺的下轮补，绝不虚标）
            for n in saved:
                if got_fts.get(n):
                    lfts[n] = got_fts[n]
            if lfts and got_fts:
                _fts_save(lfts)
            state.set_state("done", "数据已自动同步（云端表格 · %s…）" % ts[:8],
                            price_at=cat or time.strftime("%Y-%m-%d %H:%M"))
            log("云端表格同步完成：%s" % ", ".join(saved))
            return "synced"
        finally:
            state._update_lock.release()
    except Exception as e:
        try:
            # 2026-10-26 修：重建/落盘失败同样退避（同 ts 10 分钟内不重试）
            try:
                _sync_fail.update(ts=ts, at=time.time())
            except Exception:
                pass
            state.LOG.append("云端表格同步异常：%s" % e)
            state.set_state("error", "云端表格同步异常：%s" % e)
        except Exception:
            pass
        return "云端表格同步异常：%s" % e
