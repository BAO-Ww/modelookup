# -*- coding: utf-8 -*-
"""闲鱼直连（2026-10-14 用户需求：软件里直接改价/下架，不打开网页）。
接口与签名口径照抄开源 pyxianyu（MIT）的浏览器抓包实录：
  下架  POST https://h5api.m.goofish.com/h5/mtop.taobao.idle.item.downshelf/2.0/
        data={"itemId":"..."} → ret[0]=SUCCESS::调用成功（data.success=true）
  改价  ① mtop.idle.pc.idleitem.editDetail/1.0 读整包编辑详情
        ② itemPriceDTO.priceInCent 改成新价（单位：分）
        ③ mtop.idle.pc.idleitem.edit/1.0 原样回提（title/图/描述/运费全按详情带回，只动价）
  签名  sign = MD5(token & t & appKey & data)，token = cookie _m_h5_tk 前缀，
        appKey = 34839810（goofish PC web）；token 缺/过期 mtop 会 Set-Cookie 发新
        _m_h5_tk，重签重试一次即可。
Cookie：用户从浏览器（登录 goofish.com 后 F12 → 网络 → 任意请求头）整串复制，
存 wp("xy_cookies.json")，只存本机不进云端。失效表现 = ret 带 SESSION/TOKEN/登录类字样。
风险：非官方接口，闲鱼风控可能拦；改价走整包重提，类目不支持网页端编辑时会失败并回显原因。
"""
import hashlib
import json
import re
import time
import uuid

import requests

from paths import wp

APPKEY = "34839810"
H5 = "https://h5api.m.goofish.com/h5/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36")


def _cf():
    return wp("xy_cookies.json")


def load_cookies():
    try:
        with open(_cf(), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def has_cookie(acc):
    return bool(str(load_cookies().get(str(acc or "").strip()[:24], "")).strip())


def save_cookie(acc, cookie):
    acc = str(acc or "").strip()[:24]
    if not acc:
        return {"ok": False, "msg": "缺闲鱼号名"}
    cookie = str(cookie or "").strip()
    d = load_cookies()
    if cookie:
        d[acc] = cookie
    else:
        d.pop(acc, None)
    with open(_cf(), "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    return {"ok": True, "has": bool(cookie)}


def _jar(cookie_str):
    j = requests.cookies.RequestsCookieJar()
    for part in str(cookie_str).split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            k = k.strip()
            v = v.strip()
            if k:
                j.set(k, v)
    return j


def _headers():
    return {
        "accept": "application/json",
        "content-type": "application/x-www-form-urlencoded",
        "origin": "https://www.goofish.com",
        "referer": "https://www.goofish.com/",
        "user-agent": UA,
    }


def _mtop(sess, api, v, data_obj, spm="a21ybx.publish.0.0"):
    """mtop POST：sign=MD5(token&t&appKey&data)；token 缺/过期重签重试一次。"""
    data_val = json.dumps(data_obj, ensure_ascii=False, separators=(",", ":"))
    j = {}
    for attempt in (0, 1):
        token = ""
        for c in sess.cookies:
            if c.name == "_m_h5_tk" and c.value:
                token = c.value.split("_")[0]
                break
        t = str(int(time.time() * 1000))
        sign = hashlib.md5(("%s&%s&%s&%s" % (token, t, APPKEY, data_val)).encode("utf-8")).hexdigest()
        params = {
            "jsv": "2.7.2", "appKey": APPKEY, "t": t, "sign": sign, "v": v,
            "type": "originaljson", "accountSite": "xianyu", "dataType": "json",
            "timeout": "20000", "api": api, "sessionOption": "AutoLoginOnly",
            "spm_cnt": spm, "spm_pre": spm, "log_id": "mlk" + t[-8:],
        }
        try:
            r = sess.post(H5 + api + "/" + v + "/", params=params, data={"data": data_val},
                          headers=_headers(), timeout=25)
            j = r.json()
        except Exception as e:
            return {"ret": ["FAIL::网络请求失败 %s" % e]}
        ret = (j.get("ret") or [""])[0]
        if attempt == 0 and "TOKEN" in ret.upper():
            continue          # _m_h5_tk 缺/过期：响应已 Set-Cookie 新 token，重签再打一次
        return j
    return j


def _ok(j):
    ret = (j.get("ret") or [""])[0]
    return ret.startswith("SUCCESS") or "调用成功" in ret


def _sess_for(acc):
    ck = str(load_cookies().get(str(acc or "").strip()[:24], "")).strip()
    if not ck:
        return None, {"ok": False, "msg": "这个号还没存闲鱼 Cookie——先粘一次（登录 goofish.com 后 F12 复制请求头整串 Cookie）"}
    if "_m_h5_tk" not in ck:
        return None, {"ok": False, "msg": "Cookie 里没有 _m_h5_tk——要复制整串（F12 → 网络 → 任意 goofish 请求 → 请求头 Cookie）"}
    sess = requests.Session()
    sess.cookies.update(_jar(ck))
    return sess, None


def off_shelf(acc, item_id):
    acc = str(acc or "").strip()[:24]
    item_id = str(item_id or "").strip()
    if not re.fullmatch(r"\d{6,}", item_id):
        return {"ok": False, "msg": "闲鱼ID 不对（要 6 位以上纯数字）"}
    sess, err = _sess_for(acc)
    if err:
        return err
    j = _mtop(sess, "mtop.taobao.idle.item.downshelf", "2.0",
              {"itemId": item_id}, spm="a21ybx.item.0.0")
    ok = _ok(j) or bool((j.get("data") or {}).get("success"))
    ret = (j.get("ret") or [""])[0]
    if not ok and ("SESSION" in ret.upper() or "LOGIN" in ret.upper()):
        return {"ok": False, "msg": "Cookie 失效了——重新去浏览器复制一次（登录 goofish.com）"}
    return {"ok": ok, "msg": ret}


# edit 链路原样带回的字段（口径 = pyxianyu build_reshelf_payload：editDetail 抽取 + 最小标准化）
_EDIT_SCALARS = ("attribute_biz_line", "bizcode", "bucketId", "canBargain", "defaultPrice",
                 "errorTipsMsg", "freebies", "itemStatus", "itemTypeStr", "quantity",
                 "scene", "simpleItem", "supportBargainPrice", "topics")
_EDIT_OBJECTS = ("asyncSecurityInfo", "baseParams", "itemAddrDTO", "itemCatDTO",
                 "itemGroupDTO", "itemPostFeeDTO", "itemPriceDTO", "itemTextDTO",
                 "itemTopicParams", "yhbItemInfoDTO")
_EDIT_LISTS = ("imageInfoDOList", "itemLabelExtList", "itemProperties", "itemSkuList",
               "userRightsProtocols")


def set_price(acc, item_id, price):
    acc = str(acc or "").strip()[:24]
    item_id = str(item_id or "").strip()
    if not re.fullmatch(r"\d{6,}", item_id):
        return {"ok": False, "msg": "闲鱼ID 不对（要 6 位以上纯数字）"}
    try:
        p = round(float(price), 2)
    except Exception:
        return {"ok": False, "msg": "价格不是数字"}
    if not (0 < p < 1000000):
        return {"ok": False, "msg": "价格超出范围"}
    sess, err = _sess_for(acc)
    if err:
        return err
    j1 = _mtop(sess, "mtop.idle.pc.idleitem.editDetail", "1.0", {"itemId": item_id})
    if not _ok(j1):
        ret = (j1.get("ret") or [""])[0]
        if "TOKEN" in ret.upper() or "SESSION" in ret.upper() or "LOGIN" in ret.upper():
            return {"ok": False, "msg": "Cookie 失效了——重新去浏览器复制一次（登录 goofish.com）"}
        return {"ok": False, "msg": "读宝贝详情失败：" + ret}
    d1 = j1.get("data") or {}
    if not isinstance(d1.get("itemPriceDTO"), dict) or not d1.get("itemPriceDTO"):
        return {"ok": False, "msg": "宝贝详情里拿不到价格域（这个类目可能不支持网页端改价）"}
    payload = {"itemId": item_id}
    for k in _EDIT_SCALARS:
        if k in d1:
            payload[k] = d1[k]
    for k in _EDIT_OBJECTS:
        if isinstance(d1.get(k), dict) and d1.get(k):
            payload[k] = dict(d1[k])
    for k in _EDIT_LISTS:
        if k in d1:
            payload[k] = d1[k]
    pf = payload.get("itemPostFeeDTO")
    if isinstance(pf, dict):
        for k in ("canFreeShipping", "supportFreight", "onlyTakeSelf"):
            if k in pf and isinstance(pf[k], bool):
                pf[k] = "true" if pf[k] else "false"
    pd = dict(payload["itemPriceDTO"])
    pd["priceInCent"] = str(int(round(p * 100)))
    payload["itemPriceDTO"] = pd
    payload["uniqueCode"] = uuid.uuid4().hex
    payload.setdefault("bizcode", "pcMainPublish")
    payload.setdefault("publishScene", "pcMainPublish")
    payload.setdefault("sourceId", "editDetail")
    j2 = _mtop(sess, "mtop.idle.pc.idleitem.edit", "1.0", payload)
    ok = _ok(j2)
    if ok:
        return {"ok": True, "msg": "已改成 ¥%s" % p}
    ret = (j2.get("ret") or [""])[0]
    return {"ok": False, "msg": ret or "改价失败（闲鱼没给原因）"}
