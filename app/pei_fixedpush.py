#!/usr/bin/env python3
"""
推送闲鱼配文仓库（xianyu-peiwen）到 GitHub，不依赖 git 命令。
适用场景：沙箱环境下 git 不在 PATH，git push 被代理拦截。
"""
import os, sys, json, base64, urllib.request, urllib.error, time, re, shutil
from net import urlopen_ssl

REPO = "BAO-Ww/xianyu-peiwen"  # 目标仓库
REMOTE_BRANCH = "main"

def get_pat():
    # 1. 命令行参数
    if len(sys.argv) > 1:
        return sys.argv[1]
    # 2. 环境变量 GH_PAT
    env = os.environ.get("GH_PAT")
    if env:
        return env
    # 3. 上级目录的 _pat.txt
    pat_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "_pat.txt")
    if os.path.exists(pat_path):
        with open(pat_path, encoding="utf-8") as f:
            return f.readline().strip()
    raise RuntimeError("未找到 GitHub PAT。请提供 PAT：命令行参数、环境变量 GH_PAT，或在仓库父目录创建 _pat.txt")

def walk_files(root):
    """忽略临时/构建目录"""
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        # 跳过隐藏及构建目录
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in (
            "build", "dist", "__pycache__", "_build", "Debug", "Release",
            "node_modules", ".git", ".github", ".vscode"
        )]
        for fn in filenames:
            if fn.startswith(".") or fn.endswith((".pyc", ".pyo", ".log", ".png", ".jpg")):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace("\\", "/")
            files.append((rel, full))
    return files

def get_current_commit(pat):
    url = f"https://api.github.com/repos/{REPO}/git/refs/heads/{REMOTE_BRANCH}"
    try:
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {pat}",
            "User-Agent": "workbuddy"
        })
        resp = urlopen_ssl(req)
        data = json.load(resp)
        return data["object"]["sha"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # 分支不存在，需要创建初始提交
            return None
        raise

def create_blob(pat, content):
    url = f"https://api.github.com/repos/{REPO}/git/blobs"
    body = json.dumps({"encoding": "base64", "content": base64.b64encode(content).decode("ascii")})
    req = urllib.request.Request(url, data=body.encode("utf-8"), headers={
        "Authorization": f"Bearer {pat}",
        "User-Agent": "workbuddy",
        "Content-Type": "application/json"
    })
    resp = urlopen_ssl(req)
    data = json.load(resp)
    return data["sha"]

def create_tree(pat, base_tree_sha, entries):
    url = f"https://api.github.com/repos/{REPO}/git/trees"
    body = json.dumps({"base_tree": base_tree_sha, "tree": entries})
    req = urllib.request.Request(url, data=body.encode("utf-8"), headers={
        "Authorization": f"Bearer {pat}",
        "User-Agent": "workbuddy",
        "Content-Type": "application/json"
    })
    resp = urlopen_ssl(req)
    data = json.load(resp)
    return data["sha"]

def create_commit(pat, tree_sha, parent_sha, message):
    url = f"https://api.github.com/repos/{REPO}/git/commits"
    body = json.dumps({"tree": tree_sha, "parents": [parent_sha] if parent_sha else [],
                       "message": message})
    req = urllib.request.Request(url, data=body.encode("utf-8"), headers={
        "Authorization": f"Bearer {pat}",
        "User-Agent": "workbuddy",
        "Content-Type": "application/json"
    })
    resp = urlopen_ssl(req)
    data = json.load(resp)
    return data["sha"]

def update_ref(pat, commit_sha):
    url = f"https://api.github.com/repos/{REPO}/git/refs/heads/{REMOTE_BRANCH}"
    body = json.dumps({"sha": commit_sha, "force": False})
    req = urllib.request.Request(url, data=body.encode("utf-8"), headers={
        "Authorization": f"Bearer {pat}",
        "User-Agent": "workbuddy",
        "Content-Type": "application/json"
    })
    try:
        resp = urlopen_ssl(req)
        return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # 分支不存在，需要创建全新 ref
            url = f"https://api.github.com/repos/{REPO}/git/refs"
            body = json.dumps({"ref": f"refs/heads/{REMOTE_BRANCH}", "sha": commit_sha})
            req = urllib.request.Request(url, data=body.encode("utf-8"), headers={
                "Authorization": f"Bearer {pat}",
                "User-Agent": "workbuddy",
                "Content-Type": "application/json"
            })
            resp = urlopen_ssl(req)
            return json.load(resp)
        raise

def main():
    start = time.time()
    pat = get_pat()
    print(f"推送仓库 {REPO} ...")
    local_root = os.path.dirname(__file__)
    files = walk_files(local_root)
    if not files:
        print("没有需要提交的文件")
        return
    print(f"找到 {len(files)} 个文件")
    # 获取最新提交 SHA
    base = get_current_commit(pat)
    if base is None:
        print("仓库为空，需要创建初始提交")
        # 空仓必须先有一个基础树（至少一个文件）
        # 用 README.md 作为第一个文件
        readme_path = os.path.join(local_root, "README.md")
        if not os.path.exists(readme_path):
            with open(readme_path, "w", encoding="utf-8") as f:
                f.write("# 闲鱼配文\n\n海尔家电闲鱼发布工具，含 AI5张 方案。\n")
            files.append(("README.md", readme_path))
        base = None
    # 创建 blob
    entries = []
    for rel, full in files:
        with open(full, "rb") as f:
            content = f.read()
        blob_sha = create_blob(pat, content)
        entries.append({"path": rel, "mode": "100644", "type": "blob", "sha": blob_sha})
        print(f"\rblob {len(entries)}/{len(files)}", end="")
    print()
    # 创建树
    tree_sha = create_tree(pat, base, entries)
    print(f"tree: {tree_sha[:8]}")
    # 创建提交
    commit_message = f"v{time.strftime('%Y%m%d-%H%M')}: AI5张 集成冰箱/烟机双模板"
    commit_sha = create_commit(pat, tree_sha, base, commit_message)
    print(f"commit: {commit_sha[:8]}")
    # 更新引用
    result = update_ref(pat, commit_sha)
    elapsed = time.time() - start
    print(f"推送成功，耗时 {elapsed:.2f}s。")
    print(f"可访问：https://github.com/{REPO}/commit/{commit_sha}")

if __name__ == "__main__":
    main()