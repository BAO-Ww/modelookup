# 回滚手册（ModelLookup）

> 一页纸。出问题的版本想把大家退回去时看这里。
> 判定标准：装完新版打不开 / 核心功能（宝、婷、发布图）坏了 / 一装就崩，才回滚；
> 单张图不好看、某个型号数据错，重跑那个型号就行，不用回滚。

## 先搞清楚三件事

1. **版本从哪来**：客户端 `/api/appcheck` → worker `/appcheck` → GitHub **`/releases/latest`**。
   所以「哪个 Release 是 latest」就是「大家会被推到哪一版」。
2. **只有升级没有降级**：客户端 `newer = 云端版本 > 本机版本`（`api_get.py` `/api/appcheck`）。
   把 latest 切回旧 tag 后，**已经装上新版的机器不会自动退回**（它认为没新版），只能手动装旧包。
3. **图片库不会被装包删**：`.iss` 没有 `[InstallDelete]`，升降级都不动 `图片库` / `D:\B\闲鱼图片`。

## 三种回滚，按场景选

| 场景 | 用哪种 | 耗时 | 效果 |
|---|---|---|---|
| 刚发完一小时，别人电脑可能自动更新了 | **A. 切 latest（云端拦截）** | 1 分钟 | 新机器不再被推坏版本；已装新版的要手动退 |
| 自己几台机器装完坏了 | **C. 手动装旧包** | 5 分钟 | 本机回旧版，最省事 |
| 坏版本已扩散，要"推一个新版把大家带回旧功能" | **B. 还原旧内容 + 升版本号重发** | 30-40 分钟 | 所有人点更新后回到旧功能（版本号仍是新的） |

## A. 切 latest（最快，不构建）

在 `modelookup` 目录跑（token 在 `_pat.txt`）：

```python
import json, urllib.request

tok = open("_pat.txt").read().strip().splitlines()[0]
def api(url, method="GET", body=None):
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bearer " + tok, "User-Agent": "rollback",
        "Accept": "application/vnd.github+json", "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=30))

R = "https://api.github.com/repos/BAO-Ww/modelookup"
cur = api(R + "/releases/latest")
print("当前 latest =", cur["tag_name"])                      # 例如 v3.0.42
tgt = api(R + "/releases/tags/v3.0.40")                      # 想退到的那一版
api(R + "/releases/%d" % tgt["id"], "PATCH", {"make_latest": "true"})
print("切后 latest =", api(R + "/releases/latest")["tag_name"])
```

验证：用本机 token 打 `/appcheck?plat=win`，应回旧 tag：

```python
import json, os, urllib.request
tok = json.load(open(os.path.expandvars(r"%LOCALAPPDATA%\ModelLookup\auth_token.json"), encoding="utf-8"))
tok = tok.get("token") or list(tok.values())[0]
req = urllib.request.Request("https://auth.tingjiabaobao.de5.net/appcheck?plat=win",
                             headers={"User-Agent": "Mozilla/5.0", "X-Token": tok})
print(json.load(urllib.request.urlopen(req, timeout=20)))
```

> 注意：`make_latest` 的值是**字符串** `"true"`，不是布尔。切完 GitHub 会自动把原 latest 降级。

## C. 手动装旧包（自用最常用）

1. 打开 https://github.com/BAO-Ww/modelookup/releases ，选要退的 tag：
   - Windows：`ModelLookup-Setup-<版本>.exe`
   - Mac：`ModelLookup-mac-<版本>.dmg`（或 .zip）
2. **先退出软件**（托盘也要退），再装——直接覆盖安装到原目录，数据/图片库原样保留。
3. 装完**别再点"检查更新"**（latest 还没切的话它会又把你推回新版）。要彻底防，先做 A。

Mac 补充：装完双击打不开（Gatekeeper）时——

```bash
xattr -cr /Applications/ModelLookup.app
open /Applications/ModelLookup.app
```

## B. 还原旧内容 + 升版本号重发（真正让所有人"更新"回旧功能）

本仓库不是普通 git 流（推送走 `_push.py` 白名单 + GitHub API），所以「回滚」= **把旧版文件内容放回工作区，再推一次**：

1. 下旧 tag 的源码包并在临时目录解压：

```bash
curl -L -o /tmp/old.zip https://github.com/BAO-Ww/modelookup/archive/refs/tags/v3.0.40.zip
```

2. 把出问题的文件逐个从解压目录拷回来（**只拷怀疑的文件，别整目录覆盖**，免得把新数据/新模板带回去）。
3. `app/_buildver.py` 升版本号（永远是当前最新 +1，例如 `3.0.43`），`_push.py` 提交信息写明「回滚 v3.0.4x 的某某改动」。
4. 系统 Python 全路径跑：

```bash
"C:/Users/Administrator/AppData/Local/Programs/Python/Python313/python.exe" _push.py
```

5. 盯 CI 三绿 → 等 tag → 查四资产 → 用带 token 的 `/appcheck` 验版本正确。

> 为什么必须升版本号：tag = 最新 v* 的 patch+1 由 CI 算，不升号推上去 CI 会打同一个号或跳过。
> 为什么不能只"改 latest"：客户端只升级不降级，已装坏版本的机器永远收不到。

## 数据回滚（跟程序回滚分开看）

云端每个自然日第一次请求会自动打快照 `snap_YYYYMMDD`（users + pub_/pubp_/xy_ + 图池，留最近 7 份）。
误删/写坏业务数据时用运维口恢复：

```
POST https://auth.tingjiabaobao.de5.net/admin/restore   {"snap":"20261103"}
```

需要 admin 权限的 token，别在公开环境贴命令。

## 历史坑（回滚时容易踩）

- **Mac 资产不在 Release 里**：CI 的「追加 Mac 资产」步骤撞过 GitHub 500 两次，我是手工从 Actions artifact 搬到 Release 的（流程：HEAD 拿 302 的 Location → 裸 curl 分块 Range 下 → `POST uploads.github.com/.../assets?name=`）。回滚时若 Mac 包缺失，照这个流程补，或直接用旧 Release 里现成的包。
- **`/appcheck` 参数是 `plat` 不是 `os`**，传错会拿到 win 资产。
- **worker 是边缘缓存 + KV 记账**（`appdl_warm`）：切 latest 后第一次下载会冷，属正常，别以为是没生效。
- **别在回滚时顺手改别的**：一次只动导致问题的那几行，改动越小越好查。
