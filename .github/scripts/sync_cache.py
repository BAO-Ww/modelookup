# -*- coding: utf-8 -*-
"""CI 仓库快照同步器（2026-11-14，用户令「怎么稳定怎么改」）。

为什么存在：本机自建 runner 到 codeload.github.com 频繁 TLS 断连，`actions/checkout`
一挂整班白跑（#940 / #926 / #929 实锤，登录/抓取步骤根本没执行）。但金山抓取脚本的
手机端 payload 分支要 import 仓库里的 app/payload.py（连带 state/parsers_yr/normalize…）
和 app/assets 几张库，脱离仓库就会静默降级。

做法：按 commit sha 做「带清单的增量同步」——
  ① api.github.com 取 main 的 commit sha + 递归 tree（每次仅 2 个请求）；
  ② 只同步本班真正要用的白名单文件，本地 sha 与远端一致就跳过；
  ③ 下载优先走 jsDelivr CDN（按 commit sha 取，国内快且不会串版本），失败退 api blobs；
  ④ API 全挂就沿用本地缓存照常跑，只在关键文件缺失时才失败。

用法：python sync_cache.py <缓存根目录>
"""

import base64
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request

REPO = "BAO-Ww/modelookup"
BRANCH = "main"
API = "https://api.github.com"
CDN = "https://cdn.jsdelivr.net/gh"

# 白名单：app/ 下所有 py 模块 + 手机端 payload 要读的几张库 + 手机版页面 + 自己的脚本
WANT_EXACT = (
    "app/assets/images_cache.json",
    "app/assets/xinsheng.sqlite3",
    "app/assets/stock_rules.json",
    "app/assets/midea_prices.json",
    "app/assets/ronshen_prices.json",
)
WANT_PREFIX = (".github/scripts/", "mobile/app/www/")
REQUIRED = (".github/scripts/kdocs_cloud_pull.py", "app/payload.py")


def want(path):
    if path.startswith("app/") and path.endswith(".py"):
        return True
    if path in WANT_EXACT:
        return True
    return any(path.startswith(p) for p in WANT_PREFIX)


def _hdr():
    h = {"User-Agent": "ml-ci-sync", "Accept": "application/vnd.github+json"}
    tok = (os.environ.get("GH_TOKEN") or "").strip()
    if tok:
        h["Authorization"] = "Bearer " + tok
    return h


def _get(url, timeout=120):
    with urllib.request.urlopen(urllib.request.Request(url, headers=_hdr()), timeout=timeout) as r:
        return r.read()


def _api(path, timeout=45):
    return json.loads(_get(API + path, timeout=timeout).decode("utf-8"))


def _retry(fn, times=3, base=3, label=""):
    last = None
    for i in range(1, times + 1):
        try:
            return fn()
        except Exception as e:
            last = e
            print("  %s 第 %d 次失败：%r" % (label, i, e), flush=True)
            if i < times:
                time.sleep(base * i)
    raise last


def fetch_file(commit, path, blob_sha):
    """① jsDelivr（按 commit sha，国内快）→ ② api blobs（大文件也支持）"""
    try:
        return _retry(lambda: _get("%s/%s@%s/%s" % (CDN, REPO, commit, path), timeout=180),
                      times=2, base=2, label="cdn " + path)
    except Exception:
        pass
    j = _retry(lambda: _api("/repos/%s/git/blobs/%s" % (REPO, blob_sha)), times=2, label="blob " + path)
    return base64.b64decode(j["content"])


def main():
    root = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
    os.makedirs(root, exist_ok=True)
    mpath = os.path.join(root, "_manifest.json")
    manifest = {}
    try:
        with io.open(mpath, encoding="utf-8") as f:
            manifest = json.load(f) or {}
    except Exception:
        manifest = {}
    files = manifest.get("files") or {}

    commit = ""
    tree = None
    try:
        c = _retry(lambda: _api("/repos/%s/commits/%s" % (REPO, BRANCH)), times=3, base=3, label="commit")
        commit = c["sha"]
        t = _retry(lambda: _api("/repos/%s/git/trees/%s?recursive=1" % (REPO, commit)), times=3, base=3, label="tree")
        tree = [x for x in (t.get("tree") or []) if x.get("type") == "blob"]
        print("远端 commit %s，文件 %d 个" % (commit[:10], len(tree)))
    except Exception as e:
        print("⚠ 远端清单取不到（%r）——沿用本地缓存继续跑" % (e,))
        tree = None

    added = skipped = 0
    failed = []
    if tree is not None:
        for it in tree:
            path = it["path"]
            if not want(path):
                continue
            sha = it["sha"]
            dst = os.path.join(root, path.replace("/", os.sep))
            if files.get(path) == sha and os.path.isfile(dst):
                skipped += 1
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            try:
                blob = fetch_file(commit, path, sha)
                with open(dst + ".tmp", "wb") as f:
                    f.write(blob)
                os.replace(dst + ".tmp", dst)
                files[path] = sha
                added += 1
                print("  同步 %-46s %8d B" % (path, len(blob)), flush=True)
            except Exception as e:
                failed.append(path)
                print("  ⚠ 同步失败 %s：%r" % (path, e), flush=True)
        if commit:
            manifest = {"commit": commit, "files": files}
            with io.open(mpath, "w", encoding="utf-8") as f:
                f.write(json.dumps(manifest, ensure_ascii=False, indent=0))

    print("快照完成：更新 %d / 未变 %d / 失败 %d → %s" % (added, skipped, len(failed), root))

    missing = [p for p in REQUIRED if not os.path.isfile(os.path.join(root, p.replace("/", os.sep)))]
    if missing:
        print("✘ 关键文件缺失：%s" % ", ".join(missing))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
