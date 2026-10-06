# 型号查询 · 隆阳电器（公布版）

把「金山文档 · 公布版-滞销样机清理明细」在线表变成一台**独立窗口运行的桌面软件**：
双击就开（自带 WebView2 内核，**不弹浏览器**），输型号或品类就能看到清理价、库存、仓库分布、
全国可发口径，并内置闲鱼定价计算器与「已发布」标记。

## 交付物

| 文件 | 说明 |
| --- | --- |
| `ModelLookup-Setup-2.0.exe` | **安装包**（推荐）。装到 `%LOCALAPPDATA%\Programs\ModelLookup`，只装给当前用户、**不需要管理员**；带开始菜单 / 桌面快捷方式与标准卸载器 |
| `ModelLookup.exe` | 免安装的绿色版（同一份程序，21MB 单文件，拷到哪都能跑） |
| `型号查询-公布版.html` | 查询页的单文件离线版（可直接双击、发给别人，但没有在线更新能力） |

## 软件里能做什么

**窗口形态**：原生标题栏 + 原生菜单（数据 / 帮助），没有地址栏、没有标签页、没有浏览器外壳。

| 能力 | 说明 |
| --- | --- |
| 打开即全量 | 启动直接把 443 个型号列出来，不再是空白页，往下滚继续加载 |
| 型号检索 | 支持片段、大小写、全角半角、空格、颜色后缀（`BCD-550WGCFDM4WHU1(棕)` 也能命中本体） |
| 品类检索 | 直接搜「冰箱」「洗衣机」，或点品类标签 |
| 品牌 / 品类筛选 | 卡萨帝 / 海尔 / 统帅 / 科沃斯 … |
| 清理方式 | 全部 / 只滞销 / 只样机 |
| 快捷开关 | 只看有货 · 只看可发全国 · 只看未发布 |
| 清理价区间 | 自己输入 min~max，按该型号**最低清理价**筛 |
| 全国可发 | 保留原始业务口径并上色：可发 / 不可发 / 待确认（**不是布尔**，原文照显） |
| 库存 | 可用量、现存量、在库合计 + 「仓库明细」展开到各仓 |
| 闲鱼定价计算器 | 输入你的定价 → 平台费 1.6% → 实挣（定价 − 定价×1.6% − 成本），成本可在滞销价 / 样机价间切换 |
| 标记已发布 | 防重复上架 |
| 我的定价自动保存 | 输入过的闲鱼价按型号记住 |
| 补图闭环 | 缺图卡片点「＋ 补图」贴本地图片，可「导出我补的图」交回重烤进页面 |

> 「标记已发布」「我的定价」「我补的图」存在本机 WebView2 配置里，换电脑不共享。
> 程序用**固定端口 17321**，就是为了保证这些记录不会因为端口变化而丢。

## 更新数据

数据源：金山文档在线表 <https://www.kdocs.cn/l/ch7gwGTP9De4>（分享码 `ch7gwGTP9De4` / fileId `548020345856`）

两种方式：

1. **窗口里点「↻ 从金山文档更新数据」**
   - 打开在线表页面后先**静默重试约 20 秒**：该表是公开分享的，匿名会话往往就能拿到下载直链，用户什么都不用做。
   - 拿不到才弹出「登录金山文档」窗口让人扫码，登录成功后**自动继续、不用点任何按钮**。
   - 登录态留在本机 WebView2 配置里，之后都是静默更新。
2. **点「📂 用本地 Excel 更新」** —— 自己下载 xlsx 传进来（在线表登不上时的兜底）。

**核心取数技巧**：不搬运 cookie，而是在**已经处于 `www.kdocs.cn` 域的 WebView2 窗口里跑同步 XHR**
`GET /api/v3/office/file/548020345856/download`，同源自动带登录态，拿到 `download_url` 后
**免 cookie 直接下载**（实测 3,736,892 字节，`PK` 头）。这样换任何一台电脑、装完就能用。

## 目录结构

```
modelookup/
├─ app/
│  ├─ main.py                # 全部逻辑：xlsx 解析 + 页面渲染 + 金山下载 + 本地服务 + WebView2 窗口层
│  ├─ ModelLookup.spec       # PyInstaller 配置
│  ├─ make_icon.py           # 程序化生成 icon.ico（≤128 严格 BMP 条目）
│  └─ assets/                # 打包进 exe 的资源
│     ├─ template.html       # 查询页模板（唯一前端源，含 /*__DATA__*/ 占位）
│     ├─ payload.json        # 初始数据（型号 + 图）
│     ├─ images_cache.json   # 图库（更新时按型号重新匹配）
│     └─ icon.ico
├─ installer/
│  ├─ modelookup.iss               # Inno Setup 脚本
│  ├─ ChineseSimplified.isl        # 简体中文「部分翻译」覆盖层
│  └─ MicrosoftEdgeWebview2Setup.exe  # WebView2 离线引导器（缺运行时的电脑才装）
├─ parse_kdocs.py            # 在线表 xlsx → _payload_kdocs.json
├─ build_kdocs.py            # payload + 模板 → 单文件 HTML，并同步 app/assets/
├─ fetch_images_kdocs.py     # 官网机型库补图
├─ fetch_missing.py          # 海尔商城补图
├─ template_kdocs.html       # 查询页模板（= app/assets/template.html 的源）
├─ .github/
│  ├─ workflows/build-installer.yml  # CI：自动打包 exe + 安装包，发 Release
│  ├─ scripts/stage.py               # 整理产物（用 Python 写中文文件名，避免编码坏）
│  └─ release-notes.md               # Release 说明正文
├─ requirements-build.txt    # CI / 本机打包依赖
├─ _push.py / _release.py    # 走 GitHub API 推代码 / 发 Release（沙箱走不了 git push）
├─ _ci.py                    # 查/等/触发 Actions（python _ci.py --wait / --dispatch v2.1）
└─ 重建型号查询.bat           # 换月时把新 xlsx 拖上去即可
```

## 让 GitHub 自动打包（推荐，正式出包走这条路）

推上去之后**不用本机编译**，GitHub Actions 在 Windows runner 上自动：
装 Python → 装依赖 → PyInstaller 打包 → 跑成品自检 → 装 Inno Setup → 编译安装包 → 发 Release。

工作流：`.github/workflows/build-installer.yml`

| 触发方式 | 结果 |
| --- | --- |
| 手动点 **Run workflow** 并填版本号（如 `v2.1`） | 构建 + **发 Release** |
| 推 `v*` 标签 | 构建 + **发 Release** |
| 推 `main` | 只构建、上传 Artifact（**不发 Release**），用来验证能编过 |

发出来的 Release 里会有两个附件：`型号查询-安装包-vX.Y.exe`（安装包）和 `ModelLookup.exe`（绿色版）。

> ⚠ 两点必须记住，都是踩过的坑：
> 1. `actions/setup-python` 的 `cache: pip` **必须配 `cache-dependency-path: requirements-build.txt`**，否则它去找不存在的 `requirements.txt` 会直接挂。
> 2. runner 控制台是 **cp1252**，Python 往 stdout 打中文会 `UnicodeEncodeError`。工作流里设了 `PYTHONIOENCODING=utf-8` / `PYTHONUTF8=1`，整理产物的那一步也交给 `.github/scripts/stage.py` 做（Python 源码是 UTF-8，中文文件名不会坏）。

## 本机手工重建（改数据时用）

```bash
# 1) 在线表 xlsx → 数据
python parse_kdocs.py "D:\...\公布版-滞销样机清理明细-YYYY-MM-DD.xlsx"

# 2) 补图（可选）
python fetch_images_kdocs.py      # 官网机型库
python fetch_missing.py           # 海尔商城

# 3) 生成单文件 HTML，并同步 app/assets/
python build_kdocs.py

# 4) 推上去让 GitHub 出包，或者本机自己编：
python _push.py                                  # 推送 → CI 自动打包发包
# 或者本机编：
cd app && python -m PyInstaller --noconfirm --clean --distpath dist --workpath build ModelLookup.spec
cd ../installer && "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" modelookup.iss
```

依赖：`openpyxl`（解析表格）、`pywebview` + `pythonnet`（窗口层，Windows 走 WebView2）、
`Pillow`（仅 `make_icon.py` 生成图标时要，exe 本身不需要）。
运行时需要 **Microsoft Edge WebView2 运行时**（Win11 自带；Win10 多数已随 Edge 装上，装不到安装包会静默补装）。

## 打包踩过的坑（都已在代码/配置里注释）

- `openpyxl` / `webview` 必须**模块顶层 import**。放函数体内会被 PyInstaller 判成「延迟/可选」而不打包，spec 的 `hiddenimports` 也救不回来。
- **千万别 excludes `webview.platforms.win32`**：winforms 顶层就 `from webview.platforms import win32`，排掉它会让 `import webview.platforms.winforms` 失败，而 pywebview 会**误导性地报「You must have pythonnet installed」**。自检里专门留了一条 winforms 导入探针防回归。
- Inno 的 `PrivilegesRequiredOverridesAllowed=dialog` 会让安装包尝试提权，在受限环境里会**静默退出、什么都不装**。免管理员安装就别开它。
- `webview.create_window()` 必须在 `webview.start()` **之前**调用 —— 所以扫码登录窗口是启动时就建好、`hidden=True` 藏着，需要时才 `show()`。
- `GetCookiesAsync(self.url)` 只能拿**当前窗口域**的 cookie，且返回的是 `http.cookies.SimpleCookie`（`{name: Morsel}`），不是 dict。
- WebView2 是独立进程合成的，`PrintWindow` 抓不到内容；GUI 冒烟截图必须抓**屏幕区域**。

## 已知限制

- 少数型号没有官方整机图（配件、门体、已停产渠道专供款），页面显示「＋ 补图」占位（当前 338/443 = 76.3%）。
- 清理价 / 库存取自在线表快照，实际以 ERP 实时数据为准。
- 在线表文档标题仍是「公布版-**8**月份…」，但里面的 sheet 是 `10月滞销清理明细` / `10月样机清理明细`，数据确实是 10 月的（文档没改名而已）。
