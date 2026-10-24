# -*- coding: utf-8 -*-
"""新 CF 账号重建 R2（2026-10-24 用户拍板）：
1) 建/确认桶 modelookup-img
2) 把 KV(modelookup) 的 myimgs 原值搬进 R2 对象 "myimgs"（worker myimgsGet 同构）
3) 写 _r2_bucket.txt（_cf_update_code.py 据此挂 IMG binding）
跑完再跑 _cf_update_code.py 部署。幂等：桶在/对象同长就跳过。
"""
import io
import json
import ssl
import sys
import time
import urllib.error
import urllib.request

TOKEN = io.open("_cf_token.txt", encoding="utf-8").read().strip().splitlines()[0]
KV_NS = "b4eefb3c573645e093217fbc03072ddb"          # 新号 KV modelookup
BUCKET = "modelookup-img"
API = "https://api.cloudflare.com/client/v4"
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def api(method, path, data=None, raw=None, ctype="application/json"):
    req = urllib.request.Request(API + path, method=method)
    req.add_header("Authorization", "Bearer " + TOKEN)
    req.add_header("Content-Type", ctype)
    body = raw if raw is not None else (json.dumps(data).encode() if data is not None else None)
    for _ in range(4):
        try:
            r = urllib.request.urlopen(req, body, context=CTX, timeout=90)
            return json.load(r)
        except urllib.error.HTTPError as e:
            try:
                return json.load(e)
            except Exception:
                return {"success": False, "errors": [{"message": str(e)}]}
        except Exception as e:
            last = e
            time.sleep(3)
    return {"success": False, "errors": [{"message": "net: %s" % last}]}


def api_raw(path):
    """返回原始字节（KV values / R2 object body 用）"""
    req = urllib.request.Request(API + path)
    req.add_header("Authorization", "Bearer " + TOKEN)
    r = urllib.request.urlopen(req, context=CTX, timeout=120)
    return r.read()


accs = api("GET", "/accounts")
if not accs.get("success"):
    print("❌ 列账号失败：", accs.get("errors")); sys.exit(1)
ACC = accs["result"][0]["id"]
print("ACC =", ACC)

# ---- 1) 桶 ----
bl = api("GET", "/accounts/%s/r2/buckets" % ACC)
if not bl.get("success"):
    print("❌ 列 R2 桶失败（多半 token 缺 R2 编辑权限）：", bl.get("errors")); sys.exit(2)
names = [b["name"] for b in (bl["result"]["buckets"] if isinstance(bl["result"], dict) else bl["result"])]
print("现有桶：", names)
if BUCKET not in names:
    cr = api("PUT", "/accounts/%s/r2/buckets/%s" % (ACC, BUCKET), data={"locationHint": "apac"})
    print("建桶：", cr.get("success"), cr.get("errors") or "")
    if not cr.get("success"):
        sys.exit(3)
else:
    print("桶已存在，跳过创建")

# ---- 2) KV myimgs → R2 ----
try:
    raw = api_raw("/accounts/%s/storage/kv/namespaces/%s/values/myimgs" % (ACC, KV_NS))
except urllib.error.HTTPError as e:
    if e.code == 404:
        print("⚠ KV 里没有 myimgs 键（可能已是空库）——按空对象迁移")
        raw = b"{}"
    else:
        print("❌ 读 KV myimgs 失败：HTTP", e.code); sys.exit(4)
print("KV myimgs 大小：%.1f MB" % (len(raw) / 1048576))
try:
    probe = json.loads(raw.decode("utf-8"))
    print("JSON 合法，键数：", len(probe))
except Exception as e:
    print("❌ 不是合法 JSON：", repr(e)[:80]); sys.exit(6)

obj_url = "/accounts/%s/r2/buckets/%s/objects/myimgs" % (ACC, BUCKET)
need_put = True
try:
    oldraw = api_raw(obj_url)
    if len(oldraw) == len(raw):
        need_put = False
        print("R2 已有同长对象，跳过上传")
except urllib.error.HTTPError as e:
    if e.code != 404:
        print("⚠ 回读旧对象异常 HTTP", e.code, "（继续上传覆盖）")
except Exception as e:
    print("⚠ 回读旧对象网络异常（继续上传覆盖）：", repr(e)[:60])
if need_put:
    put = api("PUT", obj_url, raw=raw, ctype="application/json")
    print("上传 R2 对象 myimgs：", put.get("success"), put.get("errors") or "")
    if not put.get("success"):
        sys.exit(7)

# 回读校验
try:
    back = api_raw(obj_url)
    print("R2 回读：%.1f MB，键数 %d（应与 KV 一致）" % (len(back) / 1048576, len(json.loads(back.decode("utf-8")))))
except Exception as e:
    print("⚠ 回读校验没成（部署后用 worker 验证）：", repr(e)[:100])

# ---- 3) _r2_bucket.txt ----
io.open("_r2_bucket.txt", "w", encoding="utf-8").write(BUCKET)
print("已写 _r2_bucket.txt =", BUCKET)
print("下一步：跑 _cf_update_code.py 挂 IMG binding 部署")
