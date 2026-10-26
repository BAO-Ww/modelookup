# -*- coding: utf-8 -*-
"""把某型号「发布图」目录推到 GitHub 私有仓库（建仓 + 按型号追加提交）。

两种调用方式：
  - 命令行：  python publish_images_github.py <task.json>
              python publish_images_github.py --sel <发布图目录> --model <型号>
  - 模块：    import publish_images_github; publish_images_github.run(task_json_path)
              # 返回仓库内该型号目录的网页链接字符串，失败抛异常

仓库策略（用户 2026-09-15 定）：
  - 统一一个仓库（PUBLISH_REPO，默认 xianyu-peiwen-publish，私有）
  - 按型号分文件夹：images/<model>/...（精选6图+主图 平铺，AI5张 在 AI5张/ 子目录）
  - 首次不存在则 auto_init 新建；之后用 base_tree 追加提交，保留其它型号
PAT 来源优先级：
  GITHUB_PAT 环境变量 > 同目录 _pat.txt > 上级 _pat.txt > 工作区根 _pat.txt
"""
import os
import sys
import json
import base64
import ssl
import time
import urllib.request
import urllib.error
from net import urlopen_ssl

OWNER = "BAO-Ww"
REPO = os.environ.get("PUBLISH_REPO", "xianyu-peiwen-publish")
BRANCH = "main"
GH_PREFIX = "images"  # GitHub 上型号图统一放 images/<model>/

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


def load_tok():
    ev = os.environ.get("GITHUB_PAT") or os.environ.get("PAT")
    if ev and ev.strip():
        return ev.strip()
    here = os.path.dirname(os.path.abspath(__file__))
    cands = [
        os.path.join(here, "_pat.txt"),
        os.path.join(here, "..", "_pat.txt"),
        r"C:/Users/Administrator/WorkBuddy/2026-09-07-18-42-48/_pat.txt",
    ]
    for c in cands:
        if os.path.exists(c):
            try:
                t = open(c, encoding="utf-8").read().strip()
                if t:
                    return t
            except Exception:
                pass
    return None


def api(method, path, data=None, tok=None):
    url = "https://api.github.com" + path
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", "Bearer " + tok)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "xianyu-peiwen-publish")
    body = None
    if data is not None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        req.add_header("Content-Type", "application/json")
        req.data = body   # 2026-10-26 修：urlopen_ssl(req, timeout) 只收 2 参，body 挂 Request
    try:
        with urlopen_ssl(req, timeout=180) as r:
            raw = r.read().decode("utf-8", "ignore")
            try:
                return r.status, json.loads(raw or "{}")
            except Exception:
                return r.status, raw
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:500]


def ensure_repo(tok):
    """仓库不存在则 auto_init 新建（私有）；存在则直接返回。"""
    st, r = api("GET", "/repos/%s/%s" % (OWNER, REPO), tok=tok)
    if st == 200:
        print("仓库已存在: %s (private=%s)" % (r.get("full_name"), r.get("private")))
        return r
    st, r = api("POST", "/user/repos", {
        "name": REPO,
        "description": "闲鱼配文 · 发布图（各型号 listing 套图，私有权限）",
        "private": True,
        "auto_init": True,
    }, tok=tok)
    if st != 201:
        raise RuntimeError("创建仓库失败: %s %s" % (st, r))
    print("创建仓库成功: %s (private=%s)" % (r.get("full_name"), r.get("private")))
    # auto_init 的初始提交是异步创建的，重试等 ref 就绪
    for _ in range(12):
        st2, _ = api("GET", "/repos/%s/%s/git/ref/heads/%s" % (OWNER, REPO, BRANCH), tok=tok)
        if st2 == 200:
            break
        time.sleep(1)
    return r


def collect_files(sel_dir, model):
    """遍历发布图目录，返回 [(github_path, local_full), ...] 按 github_path 排序。"""
    exts = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif")
    out = []
    for root, dirs, files in os.walk(sel_dir):
        for fn in files:
            if fn.lower().endswith(exts):
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, sel_dir).replace("\\", "/")
                gh_path = "%s/%s/%s" % (GH_PREFIX, model, rel)
                out.append((gh_path, full))
    return sorted(out)


def get_base(tok):
    """返回 (base_commit_sha, base_tree_sha)；全新仓库返回 (None, None)。"""
    st, ref = api("GET", "/repos/%s/%s/git/ref/heads/%s" % (OWNER, REPO, BRANCH), tok=tok)
    if st != 200:
        return None, None
    base_sha = ref["object"]["sha"]
    st, c = api("GET", "/repos/%s/%s/git/commits/%s" % (OWNER, REPO, base_sha), tok=tok)
    base_tree = c.get("tree", {}).get("sha") if st == 200 else None
    return base_sha, base_tree


def run(task_path):
    """执行一次发布。task_path 指向 task json：
    {"sel_dir": ".../发布图", "model": "BCD-500WGHFDB5XAU1", "version": "B"|"N"}
    返回该型号在仓库里的网页目录链接。"""
    tok = load_tok()
    if not tok:
        raise RuntimeError("未找到 GitHub PAT（设置环境变量 GITHUB_PAT 或放置 _pat.txt）")
    task = json.load(open(task_path, encoding="utf-8"))
    sel_dir = task["sel_dir"]
    model = task.get("model") or os.path.basename(os.path.dirname(sel_dir.rstrip("/\\")))
    version = task.get("version", "N")
    if not os.path.isdir(sel_dir):
        raise RuntimeError("发布图目录不存在: %s" % sel_dir)
    files = collect_files(sel_dir, model)
    if not files:
        raise RuntimeError("发布图目录里没有图片: %s" % sel_dir)

    ensure_repo(tok)
    base_sha, base_tree = get_base(tok)

    tree = []
    for gh_path, full in files:
        data = open(full, "rb").read()
        payload = {"content": base64.b64encode(data).decode("ascii"), "encoding": "base64"}
        st, resp = api("POST", "/repos/%s/%s/git/blobs" % (OWNER, REPO), payload, tok=tok)
        if st != 201:
            print("  ! blob 失败: %s -> %s %s" % (gh_path, st, resp))
            continue
        tree.append({"path": gh_path, "mode": "100644", "type": "blob", "sha": resp["sha"]})
        print("  + %s  (%d bytes)" % (gh_path, len(data)))
    if not tree:
        raise RuntimeError("没有文件成功创建 blob")

    body = {"tree": tree}
    if base_tree:
        body["base_tree"] = base_tree
    st, resp = api("POST", "/repos/%s/%s/git/trees" % (OWNER, REPO), body, tok=tok)
    if st != 201:
        raise RuntimeError("创建 tree 失败: %s %s" % (st, resp))
    new_tree = resp["sha"]

    msg = "publish: %s · %s · %d 张图" % (model, ("报备四城" if version == "B" else "全国"), len(tree))
    st, resp = api("POST", "/repos/%s/%s/git/commits" % (OWNER, REPO),
                   {"message": msg, "tree": new_tree, "parents": [base_sha] if base_sha else []},
                   tok=tok)
    if st != 201:
        raise RuntimeError("创建 commit 失败: %s %s" % (st, resp))
    new_commit = resp["sha"]

    if base_sha:
        st, resp = api("PATCH", "/repos/%s/%s/git/refs/heads/%s" % (OWNER, REPO, BRANCH),
                       {"sha": new_commit}, tok=tok)
    else:
        st, resp = api("POST", "/repos/%s/%s/git/refs" % (OWNER, REPO),
                       {"ref": "refs/heads/%s" % BRANCH, "sha": new_commit}, tok=tok)
    if st not in (200, 201):
        raise RuntimeError("更新引用失败: %s %s" % (st, resp))

    link = "https://github.com/%s/%s/tree/%s/%s/%s" % (OWNER, REPO, BRANCH, GH_PREFIX, model)
    print("推送成功 -> %s" % link)
    return link


def _build_task(sel_dir, model, version):
    import tempfile
    td = tempfile.mkdtemp(prefix="peiwen_pub_")
    tp = os.path.join(td, "task.json")
    json.dump({"sel_dir": sel_dir, "model": model, "version": version},
              open(tp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return tp


if __name__ == "__main__":
    sel = None
    model = None
    task = None
    # 极简参数解析（不引 argparse，保持 stdlib 零依赖）
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--sel":
            sel = args[i + 1]; i += 2
        elif a == "--model":
            model = args[i + 1]; i += 2
        elif not a.startswith("-"):
            task = a; i += 1
        else:
            i += 1
    if not task and sel and model:
        task = _build_task(sel, model, "N")
    if not task:
        print("用法: python publish_images_github.py <task.json>")
        print("       python publish_images_github.py --sel <发布图目录> --model <型号>")
        sys.exit(2)
    try:
        print(run(task))
    except Exception as e:
        print("发布失败:", e)
        sys.exit(1)
