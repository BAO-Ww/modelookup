# -*- coding: utf-8 -*-
"""AI 密钥（2026-11-12 安全整改）
================================
背景：硅基流动 / 智谱 key 原先硬编码在 pei_core.py、copywriter.py 里，随公开仓库
明文暴露，被 GitHub 爬虫抓走跑 DeepSeek 烧钱（11-11 硅基流动账单实锤）。

现在改成三层，仓库里零明文：
    1) 环境变量（ML_SF_KEY / ML_GLM_KEY / ML_SN_KEY）
    2) 本机文件 sf_key.txt / glm_key.txt / sn_key.txt（换 key 免重装，优先级高于缓存）
    3) 本机缓存 aikey.json（登录后从 worker 的 /aikey 拉下来，7 天有效）

云端那份存 worker KV 键 aikeys，只有带合法登录态才能取，走 /admin/aikey 改。
"""
import io
import json
import os
import time

from paths import wp

_CACHE = wp("aikey.json")
_TTL = 7 * 86400
_MEM = {}          # 进程内缓存：避免每次调用都读盘 / 打网络


def _read_txt(name):
    try:
        p = wp(name)
        if os.path.isfile(p):
            with io.open(p, encoding="utf-8-sig") as f:
                return (f.read() or "").strip()
    except Exception:
        pass
    return ""


def _read_cache():
    try:
        with io.open(_CACHE, encoding="utf-8") as f:
            j = json.load(f)
        if isinstance(j, dict) and (j.get("ts") or 0) > time.time() - _TTL:
            return j
    except Exception:
        pass
    return None


def _pull_cloud():
    """向 worker 拉一次密钥并写本机缓存。没登录/没网都返回 None，绝不抛。"""
    try:
        import cloud
        r, _err = cloud.cloud_req("/aikey", timeout=20)
        if not r or not (r.get("sf") or r.get("glm") or r.get("sn")):
            return None
        j = {"sf": r.get("sf") or "", "glm": r.get("glm") or "",
             "sn": r.get("sn") or "", "ts": int(time.time())}
        try:
            with io.open(_CACHE, "w", encoding="utf-8") as f:
                json.dump(j, f)
        except Exception:
            pass
        return j
    except Exception:
        return None


def _get(kind, env_name, fname):
    v = (os.environ.get(env_name) or "").strip()
    if v:
        return v
    v = _read_txt(fname)
    if v:
        return v
    if _MEM.get(kind):
        return _MEM[kind]
    j = _read_cache()
    if not j:
        j = _pull_cloud()
    v = (j or {}).get(kind) or ""
    if v:
        _MEM[kind] = v
    return v


def sf_key():
    """硅基流动 key（视觉看图首选 Qwen3.5-4B）。拿不到返回空串。"""
    return _get("sf", "ML_SF_KEY", "sf_key.txt")


def glm_key():
    """智谱 key（写文案 glm-4.5-flash / 看图兜底 glm-4v-flash）。拿不到返回空串。"""
    return _get("glm", "ML_GLM_KEY", "glm_key.txt")


def sn_key():
    """商汤 SenseNova key（看图第三兜底 sensenova-6.8-flash-lite）。拿不到返回空串。"""
    return _get("sn", "ML_SN_KEY", "sn_key.txt")
