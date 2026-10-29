# -*- coding: utf-8 -*-
"""更新编排域（2026-10-11 拆分一期：从 main.py 原样搬出，纯搬运零行为变化）。
云端跟随线程 / 软件自更新 / 全部更新 / do_update / do_stock_update（已退役编排）。
依赖方向：updater 在 kdocs/yyclient/cloud 之上、server 之下。"""
import hashlib
import io
import os
import subprocess
import threading   # 2026-10-26 补：诊断上送 _send 用（此前 NameError 被 except 吞，上送从未执行）
import time
import traceback
import urllib.parse
import urllib.request

import state
import winref
from cloud import _cloud_token, cloud_req, stock_pull_cloud, sync_xlsx_cloud
from config import (APP_VERSION, CLEAN_XLSX, CLOUD_HOST, _CLOUD_UA, FILE_ID,
                    ONLINE_SHARE, PRICE_SHARE, SUBSIDY_FID_CACHE, SUBSIDY_SHARE,
                    SUBSIDY_XLSX,
                    YY_CAS_ENTRY, PRICE_XLSX)
from kdocs import obtain_download_url, probe_file_id
from net import fetch_xlsx_by_url, urlopen_ssl
from parsers_yr import load_records_from_path
from payload import rebuild
from paths import wp
from yy_agg import yy_finish
from yy_login import auto_yy_login
from yy_transport import (yy_call, yy_call_direct, yy_capture_cookies,
                          yy_cookies_load, yy_on_c3, yy_on_login_page, yy_pull_all)


def xlsx_follower():
    """云端表格跟随线程（每 60 秒轻探一次）：GitHub Actions 定时任务（云端浏览器）
    从金山拉「公布版 + 价格表 + 补贴表」传云端；本机有新内容才拉全量重建。
    金山表一更新，哪怕所有软件都关着，云端也会先拿到、开机 90 秒内跟上。"""
    time.sleep(15)                       # 启动稍让首屏（2026-10-25：90→15s，Mac 首装等太久）
    _diag = {"sent": False, "last_bad": 0.0}
    while True:
        try:
            if state.read_state()["status"] in ("running", "login"):
                time.sleep(60)
                continue
            r = sync_xlsx_cloud(state.LOG.append)
            # 诊断上送（2026-10-25 Mac 空数据排查）：首轮必发一份（成功也发，证明活到哪
            # 一步），之后只在异常结果时限频 30 分钟再发。延迟 5 秒让日志泵先写完尾巴。
            bad = r not in ("synced", "unchanged")
            if not _diag["sent"] or (bad and time.time() - _diag["last_bad"] > 1800):
                _diag["sent"] = True
                if bad:
                    _diag["last_bad"] = time.time()

                def _send(_reason=r):
                    time.sleep(5)
                    from cloud import client_log_upload
                    client_log_upload("xlsx:" + str(_reason))
                threading.Thread(target=_send, daemon=True).start()
        except Exception:
            pass
        time.sleep(60)


def xs_last():
    """上次欣盛商城同步时间（xs_ts.txt，没有 = 从未同步过 → 首轮就拉）。"""
    try:
        return io.open(wp("xs_ts.txt"), encoding="utf-8").read().strip()
    except Exception:
        return ""


def xinsheng_follower():
    """欣盛臻选商城月度同步线程（2026-10-12）：官方售价 + 缺图补图的数据源。
    每小时看一眼，距上次同步 ≥30 天才真拉（增量只拉新增/变价，几十秒）；
    同步完直接挂到当前 payload 上 bump rev 无感刷新，不用重解析金山表。
    任何异常只记日志，绝不影响其他更新链路。"""
    time.sleep(300)                      # 开场让位给首屏和表格跟随
    while True:
        try:
            last = xs_last()
            due = True
            if last:
                try:
                    t = time.mktime(time.strptime(last, "%Y-%m-%d %H:%M"))
                    due = (time.time() - t) >= 30 * 86400
                except Exception:
                    due = True
            if due and state.read_state()["status"] not in ("running", "login"):
                from xinsheng_datasource import XinshengDataSource
                ds = XinshengDataSource(db_path=wp("xinsheng.sqlite3"))
                st = ds.sync()
                now = time.strftime("%Y-%m-%d %H:%M")
                try:
                    io.open(wp("xs_ts.txt"), "w", encoding="utf-8").write(now)
                except Exception:
                    pass
                state.LOG.append("欣盛商城月度同步：%s" % st)
                try:
                    from payload import attach_xinsheng, get_payload, set_payload
                    p = get_payload()
                    attach_xinsheng(p, state.LOG.append)
                    set_payload(p)       # rev+1 → 页面无感重载
                except Exception as e:
                    state.LOG.append("欣盛数据挂载失败：%s" % e)
        except Exception as e:
            try:
                state.LOG.append("欣盛同步异常：%s" % e)
            except Exception:
                pass
        time.sleep(3600)                 # 每小时看一眼，满 30 天才真拉


def _app_install_bg(log):
    """软件自更新：从云端下安装包 → 静默覆盖安装 → 本进程退出让位（2026-10-08）。
    走 worker 中转（/appdl），客户端不用碰 GitHub。"""
    try:
        j, err = cloud_req("/appcheck", timeout=30)
        if err or not j or not j.get("ok"):
            state.set_state("error", "查询新版本失败：%s" % (err or (j or {}).get("msg")))
            return
        tag = str(j.get("tag") or "")
        ver = str(j.get("ver") or "")
        log("自更新：开始下载 %s（%.1f MB）…" % (j.get("name") or tag, (j.get("size") or 0) / 1048576.0))
        state.set_state("running", "正在下载新版本 v%s（几十 MB，视网速 1-3 分钟）…" % ver)
        tok = _cloud_token()
        req = urllib.request.Request(CLOUD_HOST + "/appdl?tag=" + urllib.parse.quote(tag))
        req.add_header("X-Token", tok)
        req.add_header("User-Agent", _CLOUD_UA)
        # 更新下载挂节点（2026-10-29 用户令）：settings.json 配了 update_proxy 就走它，
        # 实测直连 0.10MB/s → 走节点 0.55MB/s。没配置保持直连，老机器零变化。
        try:
            from paths import update_proxy as _upd_proxy
            _upx = _upd_proxy()
        except Exception:
            _upx = None
        if _upx:
            log("自更新：下载走代理 %s" % _upx)
        dest = os.path.join(os.environ.get("TEMP") or wp(""),
                            "ModelLookup-Setup-%s.exe" % (ver or APP_VERSION))
        got, total = 0, 0
        with urlopen_ssl(req, timeout=900, proxy=_upx) as r:
            total = int(r.headers.get("Content-Length") or 0)
            with io.open(dest, "wb") as f:
                while True:
                    chunk = r.read(262144)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    if total:
                        state.set_state("running", "正在下载新版本 v%s… %d%%" % (ver, got * 100 // total))
        if got < 1048576:
            state.set_state("error", "安装包下载不完整（%d 字节），重试一次" % got)
            return
        # 2026-10-14 体检 P0-3：SHA-256 完整性校验（worker /appcheck 透传 GitHub 资产 digest）。
        # 更新链上任一环被篡改，这里装包前就拦下——先 taskkill 再 /SILENT 的链路容不得坏包。
        want_sha = str(j.get("sha256") or "")
        if want_sha:
            h = hashlib.sha256()
            with io.open(dest, "rb") as f:
                for chunk in iter(lambda: f.read(1048576), b""):
                    h.update(chunk)
            got_sha = h.hexdigest()
            if got_sha != want_sha:
                try:
                    os.remove(dest)
                except Exception:
                    pass
                log("自更新：SHA-256 校验失败！want=%s got=%s，已丢弃不安装" % (want_sha, got_sha))
                state.set_state("error", "安装包校验失败（SHA-256 不匹配，已删除下载），未安装")
                return
            log("自更新：SHA-256 校验通过")
        else:
            log("自更新：云端未提供校验值（旧资产），跳过完整性校验")
        log("自更新：下载完成 %s（%.1f MB），准备退出并静默安装…" % (dest, got / 1048576.0))
        state.set_state("running", "下载完成，软件将自动关闭并安装新版，装完会自动重新打开…")
        # 2026-10-09 修复「下载了但静默安装没落地」：原来是先起安装器、自己 2.5 秒后再退，
        # 安装器解压快的话旧进程还没让位就去覆盖 ModelLookup.exe → 文件被占用，
        # /SUPPRESSMSGBOXES 又吞掉报错窗 → 静默失败什么都不装。
        # 改成交给一个独立的 cmd 帮助进程（DETACHED，跟本进程无父子关系）：
        # 等 2 秒 → 强杀本程序（含 onefile 引导进程）→ 等 1 秒放句柄 →
        # 跑静默安装（带 /LOG 留痕）→ 装完拉起新版。安装器 InitializeSetup
        # 里另有同样的 taskkill 兜底（双保险，手动双击安装也吃这层）。
        logf = os.path.join(os.environ.get("TEMP") or wp(""), "ModelLookup-Update.log")
        newexe = os.path.join(os.environ.get("LOCALAPPDATA") or "",
                              "Programs", "ModelLookup", "ModelLookup.exe")
        # 2026-10-09 深夜 2.7.11→2.7.12 自更新静默失败复盘：安装链用 cmd /c 单行长命令 +
        # 内嵌多层引号，经 list2cmdline 转义后 cmd 的引号剥离规则可能把命令行搞乱 →
        # 安装器从未运行（TEMP 无 /LOG 文件实锤）。改成把步骤写成独立 .cmd 批处理
        # （纯 ASCII，无引号嵌套问题），再 cmd /c 执行该文件；每步 echo 留痕到
        # ModelLookup-Update.log，下次失败能直接看出断在哪一步。
        bat = os.path.join(os.environ.get("TEMP") or wp(""), "ml_update.cmd")
        setupf = dest
        setlog = os.path.join(os.environ.get("TEMP") or wp(""), "ModelLookup-Setup.log")
        q = lambda p: '"' + p + '"'           # 批处理路径加引号（不能用 % 格式化：
        batlines = [                          # 行内 %date%/%errorlevel% 会被 Python 误解析）
            "@echo off",
            "echo [%date% %time%] update helper start > " + q(logf),
            "ping -n 3 127.0.0.1 >nul",
            "taskkill /F /IM ModelLookup.exe >>" + q(logf) + " 2>&1",
            "echo taskkill rc=%errorlevel% >> " + q(logf),
            "ping -n 2 127.0.0.1 >nul",
            q(setupf) + " /SILENT /SUPPRESSMSGBOXES /NORESTART /LOG=" + q(setlog)
            + " >>" + q(logf) + " 2>&1",
            "echo setup rc=%errorlevel% >> " + q(logf),
            "if exist " + q(newexe) + ' start "" ' + q(newexe),
            "echo [%date% %time%] update helper done >> " + q(logf),
        ]
        # 2026-10-14 体检 P1-C1：不能再用 ascii+ignore 写 bat——中文用户名机器上
        # TEMP/安装路径里的中文字符会被 errors="ignore" 静默剥掉，taskkill/setup 变无效命令，
        # 自更新从此永远失败且无感知。mbcs（Windows ANSI/GBK）cmd 才认。
        with io.open(bat, "w", encoding="mbcs", errors="strict") as f:
            f.write("\r\n".join(batlines) + "\r\n")
        # 2026-10-09 修「更新时弹黑色 ping 窗口」：CREATE_NO_WINDOW 让 cmd 带一个
        # 隐藏控制台，所有子程序继承它，全程无窗口。（父进程 os._exit 后帮助进程
        # 照样存活，不依赖父子关系。）
        # 2026-10-10 修 PyInstaller 6.22.2 父进程校验误伤：环境里的 _PYI_* 会让
        # 新版 exe 引导器误判自己是「onefile 子进程」→ 弹 Security validation
        # failure 起不来。这里必须显式传干净 env——只在程序启动时清理不够，
        # 因为校验发生在新版引导器里、比新版的 Python 清理代码更早执行。
        _clean_env = {k: v for k, v in os.environ.items()
                      if not (k.startswith("_PYI") or k == "_MEIPASS2")}
        subprocess.Popen(["cmd", "/d", "/c", bat],
                         creationflags=0x08000000,        # CREATE_NO_WINDOW
                         env=_clean_env)
        time.sleep(1.5)                      # 帮助进程先起来，页面此时已收到 ok 回应
        try:
            if winref.MAIN_WIN is not None:
                winref.MAIN_WIN.hide()
        except Exception:
            pass
        os._exit(0)                          # 让位给安装器（正常情况已被 taskkill 杀掉，这里是兜底）
    except Exception as e:
        state.set_state("error", "自更新失败：%s" % e)


def _updateall_bg(log, wf="all"):
    """全部更新（2026-10-09 v2.8 全云端口径）：本机不拉数据 —— 一键触发云端
    （GitHub Actions：金山拉表 + 用友拉库存），跑完自动从云端同步下来。
    wf = all | kd（只表格）| yy（只库存）。"""
    errs, parts = [], []
    state._ALLRUN[4] = 0
    state._ALLRUN[5] = time.time()
    try:
        state._all_stage("正在触发云端任务（GitHub Actions）…", 5)
        state.set_state("running", "正在触发云端更新（GitHub Actions）…")
        j, err = cloud_req("/citrigger", "POST", {"wf": wf}, timeout=60)
        if err or not j or not j.get("ok"):
            raise RuntimeError("触发云端任务失败：%s" % (err or (j or {}).get("msg")))
        tmark = str(j.get("at") or "")[:19]          # ISO UTC 截到秒，比较 run 是否本次触发的
        prev = j.get("prev") or {}                   # 触发前各任务最新 run id（比 tmark 更可靠）
        log("云端任务已触发（%s），等它跑完（一般 1-3 分钟）…" % wf)
        state._all_stage("云端任务已触发，等待开跑…", 12)
        res, done = {}, False
        deadline = time.time() + 900
        time.sleep(25)                               # dispatch 后 run 要几秒才出现在列表里
        n_fail, last_err = 0, ""                     # 失败留痕（2026-10-09：之前静默 continue，15 分钟瞎等查不到原因）
        while time.time() < deadline:
            time.sleep(15)
            # 云端一般 1-3 分钟跑完：按已等时长平滑推进 15%→68%（只进不退）
            waited = time.time() - state._ALLRUN[5]
            state._all_stage("云端拉数据中（一般 1-3 分钟）…",
                             min(68, 15 + int(waited * 53 / 180.0)))
            js, err2 = cloud_req("/cistatus", "GET", timeout=30)
            if err2 or not js or not js.get("ok"):
                n_fail += 1
                last_err = err2 or (js or {}).get("msg") or "未知"
                state._all_stage("云端状态查询失败 %d 次（%s），重试中…"
                                 % (n_fail, last_err[:40]), state._ALLRUN[4])
                if n_fail % 5 == 0:
                    log("⚠ 查询云端状态第 %d 次失败：%s" % (n_fail, last_err))
                if n_fail >= 40:
                    raise RuntimeError("云端状态连续查询失败 %d 次（最后错误：%s）——"
                                       "检查本机网络/代理（Clash 需给 %s 加直连规则）"
                                       % (n_fail, last_err, CLOUD_HOST.split("//")[-1]))
                continue                             # 网络抖动：下轮再试
            n_fail = 0
            res = js
            want = [k for k in ("yy", "kd")
                    if (k == "yy" and wf in ("all", "yy")) or (k == "kd" and wf in ("all", "kd"))]
            def _is_this_run(k, _res=res, _prev=prev, _tmark=tmark):
                r = _res.get(k) or {}
                if (r.get("status") or "") != "completed":
                    return False
                rid = int(r.get("id") or 0)
                if _prev.get(k) and rid:             # 有触发前 id：run 必须比它新（防止旧 run 误判）
                    return rid > int(_prev[k])
                return str(r.get("at") or "")[:19] >= _tmark
            if all(_is_this_run(k) for k in want):
                done = True
                break
        if not done:
            raise RuntimeError("云端任务等了 15 分钟还没跑完（去 GitHub Actions 页面看看）")
        state._all_stage("云端跑完了，正在下载数据到本机…", 72)
        for k, label in (("kd", "表格"), ("yy", "库存")):
            if k not in want_local(wf):
                continue
            r = res.get(k) or {}
            if (r.get("con") or r.get("conclusion") or "") != "success":
                msg = "云端%s任务失败（%s）" % (label, r.get("conclusion") or "未知")
                errs.append(msg)
                parts.append("云端%s：❌ %s" % (label, r.get("conclusion") or "未知"))
                continue
            if k == "kd":
                state._all_stage("正在同步云端表格并重建数据（约 20-40 秒）…", 78)
                st = sync_xlsx_cloud(log, touch=True)
                if st == "synced":
                    parts.append("表格：云端已更新，本机已同步重建")
                    state._UPD_DETAIL[0] = "云端表格同步重建"
                elif st == "unchanged":
                    parts.append("表格：云端确认过最新（内容无变化，时间已刷新）")
                    state._UPD_DETAIL[0] = "云端表格确认最新"
                else:
                    errs.append("表格：%s" % st)
                    parts.append("表格：❌ %s" % st)
            else:
                state._all_stage("正在同步云端用友库存…", 88)
                before = (state.STOCK or {}).get("ts") or ""
                stock_pull_cloud(log, force=False)
                after = (state.STOCK or {}).get("ts") or ""
                if after and after != before:
                    parts.append("库存：云端已更新，本机已同步（%s）" % after)
                    state._STK_DETAIL[0] = "从云端同步 %s 行（云端定时共享）" % (state.STOCK.get("rows") or 0)
                else:
                    state.set_state(stock_at=time.strftime("%Y-%m-%d %H:%M"))
                    parts.append("库存：云端确认过最新（内容无变化，时间已刷新）")
                    state._STK_DETAIL[0] = "云端库存确认最新"
    except Exception as e:
        errs.append(str(e))
        parts.append("❌ %s" % e)
        try:
            state.set_state("error", str(e))
        except Exception:
            pass
    finally:
        state._ALLRUN[0] = False
        state._ALLRUN[1] = "；".join(errs)
        state._ALLRUN[2] = "　|　".join(parts)
        state._ALLRUN[3] = ""
        state._ALLRUN[4] = 100
        if not errs:
            state.set_state("done", "云端更新完成", rev=state.STATE.get("rev", 0) + 1)
        log("全部更新结束 → %s" % state._ALLRUN[2])


def want_local(wf):
    return {"all": ("kd", "yy"), "kd": ("kd",), "yy": ("yy",)}[wf if wf in ("all", "kd", "yy") else "all"]


def do_update(log, xlsx_bytes=None, xlsx_path=None, auto=False):
    _upd_parts = None               # 非 None = 走了金山直拉，记每张表的结果（2026-10-09 摘要用）
    if not state._update_lock.acquire(blocking=False):
        return False, "已有更新在进行中"
    try:
        if xlsx_bytes is not None:
            state.set_state("running", "正在解析上传的 Excel…")
            tmp = wp("upload.xlsx")
            with open(tmp, "wb") as f:
                f.write(xlsx_bytes)
            log("使用本地文件（%.2f MB）" % (len(xlsx_bytes) / 1048576.0))
            recs, mo = load_records_from_path(tmp)
            kind = "l" if any(r.get("kind") in ("l", "s") for r in recs) else "p"
            dst = wp(CLEAN_XLSX if kind == "l" else PRICE_XLSX)
            with open(dst, "wb") as f:
                f.write(xlsx_bytes)
            log("已识别为%s，共 %d 条" % ("公布版清理明细" if kind == "l" else "价格表", len(recs)))
        elif xlsx_path:
            state.set_state("running", "正在解析本地 Excel…")
            log("使用本地文件：%s" % xlsx_path)
            recs, mo = load_records_from_path(xlsx_path)
            kind = "l" if any(r.get("kind") in ("l", "s") for r in recs) else "p"
            import shutil
            shutil.copyfile(xlsx_path, wp(CLEAN_XLSX if kind == "l" else PRICE_XLSX))
        else:
            # ---- ① 公布版（auto=定时自动更新时绝不弹窗，拿不到直链就这轮跳过）
            state.set_state("running", "正在连接金山文档（公布版）…")
            du = obtain_download_url(log, FILE_ID, ONLINE_SHARE,
                                     allow_prompt=not auto)
            if not du:
                if auto:
                    log("自动更新：拿不到公布版直链（可能要登录），这轮跳过")
                    state.set_state("done", "自动更新跳过（下次再试）")
                    return False, "跳过"
                raise RuntimeError("拿不到下载直链")
            state.set_state("running", "正在下载公布版在线表…")
            raw = fetch_xlsx_by_url(du, log)
            with open(wp(CLEAN_XLSX), "wb") as f:
                f.write(raw)
            _upd_parts = ["公布版✓"]

            # ---- ② 价格表（fileId 现场反查；拿不到就沿用上次的，不阻断整体更新）
            try:
                state.set_state("running", "正在连接金山文档（价格表）…")
                fid = probe_file_id(log, PRICE_SHARE)
                if fid:
                    state.set_state("running", "正在下载价格表…")
                    du2 = obtain_download_url(log, fid, PRICE_SHARE,
                                              allow_prompt=False, silent=3)
                    raw2 = fetch_xlsx_by_url(du2, log)
                    with open(wp(PRICE_XLSX), "wb") as f:
                        f.write(raw2)
                    _upd_parts.append("价格表✓")
                else:
                    log("价格表 fileId 没拿到，保留上一次的价格表数据")
                    _upd_parts.append("价格表沿用上次")
            except Exception as e:
                log("价格表这次没更新成功（%s），保留上一次的数据" % e)
                _upd_parts.append("价格表沿用上次")

            # ---- ③ 补贴表（以旧换新 69码+售价；这张表要金山登录，没登录就沿用旧数据）
            try:
                state.set_state("running", "正在连接金山文档（补贴表）…")
                sfid = probe_file_id(log, SUBSIDY_SHARE, cache_name=SUBSIDY_FID_CACHE)
                if sfid:
                    state.set_state("running", "正在下载补贴表…")
                    du3 = obtain_download_url(log, sfid, SUBSIDY_SHARE,
                                              allow_prompt=True, silent=4)
                    raw3 = fetch_xlsx_by_url(du3, log)
                    with open(wp(SUBSIDY_XLSX), "wb") as f:
                        f.write(raw3)
                    _upd_parts.append("补贴表✓")
                else:
                    log("补贴表 fileId 没拿到，沿用上一次的补贴数据")
                    _upd_parts.append("补贴表沿用上次")
            except Exception as e:
                log("补贴表这次没更新成功（%s），沿用上一次的数据" % e)
                _upd_parts.append("补贴表沿用上次")

        state.set_state("running", "正在重建数据…")
        _pl = rebuild(log)
        if _upd_parts is not None:
            state._UPD_DETAIL[0] = "%s，重建 %d 个型号" % ("、".join(_upd_parts), len(_pl["rows"]))
        else:
            state._UPD_DETAIL[0] = "本地文件导入，重建 %d 个型号" % len(_pl["rows"])
        state.set_state("done", "更新完成", price_at=time.strftime("%Y-%m-%d %H:%M"))
        return True, state._UPD_DETAIL[0]
    except Exception as e:
        msg = str(e) or repr(e)
        log("更新失败：%s" % msg)
        log(traceback.format_exc())
        state.set_state("error", msg)
        return False, msg
    finally:
        state._update_lock.release()


def do_stock_update(log, auto=False):
    """【已退役 2026-10-10 · 全云端口径】本机直拉用友的入口已全部删除
    （/api/stock 改走云端，定时跟随 auto_updater 本来就只同步云端）——
    本函数不再有任何调用方，保留代码备查。别再接回去：本机 CAS 自动登录
    会拿存的账密反复试错（有锁号风险），数据也会和云端 CI 拉的出现两套口径。
    历史逻辑备忘：
    拉用友实时库存：优先用存下的会话直拉（免登录）；失效才弹窗等人登录一次。
    auto=True（定时自动更新）时绝不弹窗：会话失效就这轮跳过，下小时再试。
    2026-10-08 两条新规：
    · 子账号（role != admin）不碰用友，直接取云端共享库存（管理号更新后全员跟进）；
    · 有保存的用友账密（YY_USER/YY_PASS 或 wp()/yy_auth.json）→ 会话失效时自动填表登录。"""
    if not state._update_lock.acquire(blocking=False):
        state.set_state("error", "已有更新在进行中")
        return
    try:
        # ---- 子账号：云端直达（这是 2026-10-08 的主路径，用友只由管理号登录）
        if state.PERM_STATE.get("role") not in (None, "admin"):
            return stock_pull_cloud(log)

        # ---- 快路径：本地存过用友会话 → urllib 直拉，不开窗、不用登录
        saved = yy_cookies_load()
        if saved:
            state.set_state("running", "正在连接用友 YonSuite（本地会话）…")
            log("用本地保存的会话直拉（会话存于 %s）" % (saved.get("ts") or "?"))
            data, err = yy_call_direct(1, 1)
            if err is None:
                recs, err = yy_pull_all(yy_call_direct, log)
                if err is None:
                    yy_finish(recs, log)
                    return
            if err != "NOTLOGIN":
                state.set_state("error", "用友接口异常：%s" % err)
                return
            log("本地会话已失效，改走窗口登录…")

        # ---- 慢路径：WebView 登录流程（登录成功后抓 cookie 存盘，下次走快路径）
        if auto:
            # 自动模式绝不弹窗：先试自动登录（有账密），不行这轮跳过
            if auto_yy_login(log):
                data, err = yy_call_direct(1, 1)
                if err is None:
                    recs, err = yy_pull_all(yy_call_direct, log)
                    if err is None:
                        yy_finish(recs, log)
                        return
                state.set_state("error", "用友接口异常：%s" % (err or "?"))
                return
            log("自动更新：用友会话失效且自动登录不可用，这轮跳过（下小时再试）")
            state.set_state("done", "自动更新跳过（下次再试）")
            return
        # 手动点按钮：也先试自动登录（省得人点）；不行再走人工弹窗
        if auto_yy_login(log):
            data, err = yy_call_direct(1, 1)
            if err is None:
                recs, err = yy_pull_all(yy_call_direct, log)
                if err is None:
                    yy_finish(recs, log)
                    return
            state.set_state("error", "用友接口异常：%s" % (err or "?"))
            return
        if winref.YY_WIN is None or winref.YY_CLOSED.is_set():
            state.set_state("error", "用友窗口不可用，重启软件再试")
            return
        state.set_state("running", "正在连接用友 YonSuite…")
        log("打开用友工作台…")
        try:
            winref.YY_WIN.load_url(YY_CAS_ENTRY)
        except Exception:
            pass
        # 等页面定下来（最多 15 秒）：要么落在 c3（有会话），要么被踢到 CAS 登录页
        ready = False
        for _ in range(30):
            time.sleep(0.5)
            if winref.YY_CLOSED.is_set():
                state.set_state("error", "用友窗口被关闭，已取消")
                return
            if yy_on_c3(winref.YY_WIN) or yy_on_login_page(winref.YY_WIN):
                ready = True
                break
        if not ready:
            state.set_state("error", "用友页面加载不出来（检查网络）")
            return

        # 静默探测会话：pageSize=1 打一发
        data, err = yy_call(winref.YY_WIN, 1, 1)
        if err == "NOTLOGIN":
            log("没有有效会话，弹出登录窗口（登录一次，之后一直有效）")
            try:
                winref.YY_WIN.show()
            except Exception:
                pass
            state.set_state("login", "请在弹出的窗口里登录用友（一次就好，之后点更新直接拉库存）")
            t0 = time.time()
            ok = False
            while time.time() - t0 < 900:
                time.sleep(3)
                if winref.YY_CLOSED.is_set():
                    state.set_state("error", "登录窗口被关闭，已取消")
                    return
                if yy_on_c3(winref.YY_WIN):
                    data, err = yy_call(winref.YY_WIN, 1, 1)
                    if err != "NOTLOGIN":
                        ok = True
                        break
                elif yy_on_login_page(winref.YY_WIN) and int(time.time() - t0) % 30 == 0:
                    state.set_state("login", "等待登录中…（登录成功后自动继续，窗口别关）")
            if not ok:
                try:
                    winref.YY_WIN.hide()
                except Exception:
                    pass
                if data is None and err == "NOTLOGIN":
                    state.set_state("error", "等待登录超时，已取消")
                elif err:
                    state.set_state("error", "登录后拉取失败：%s" % err)
                return
            log("登录成功，会话已存住（下次不用再登）")
            yy_capture_cookies(log)          # ← 关键：把会话 cookie 存下来，重启免登录
            try:
                winref.YY_WIN.hide()
            except Exception:
                pass
        elif err:
            state.set_state("error", "用友接口异常：%s" % err)
            return

        recs, err = yy_pull_all(lambda p: yy_call(winref.YY_WIN, p), log)
        if err:
            if err == "NOTLOGIN":
                state.set_state("error", "拉取中途会话失效，请重新点「更新实时库存」")
            else:
                state.set_state("error", "用友接口异常：%s" % err)
            return
        yy_finish(recs, log)
    except Exception as e:
        state.set_state("error", "实时库存更新失败：%s" % e)
    finally:
        state._update_lock.release()
