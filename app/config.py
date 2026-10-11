# -*- coding: utf-8 -*-
"""全局常量（2026-10-11 拆分一期：从 main.py 原样搬出，纯搬运零行为变化）。
依赖方向：config 是依赖图根，不 import 任何本项目模块。"""

APP_NAME = "型号查询"
APP_VERSION = "2.7.25"
APP_ID = "ModelLookup"

# payload 结构版本：与 parse_kdocs.py 产出 meta.pv 对齐。
# 改了 rows 的字段结构（加字段/改含义）就 +1，老缓存自动失效换用包内数据。
PAYLOAD_VER = 26

ONLINE_SHARE = "https://www.kdocs.cn/l/ch7gwGTP9De4"
FILE_SHARE = "ch7gwGTP9De4"
FILE_ID = "548020345856"

PRICE_SHARE = "https://www.kdocs.cn/l/cficbK9Drrdp"
PRICE_SHARE_CODE = "cficbK9Drrdp"

SOURCES = [
    {"kind": "clean", "name": "公布版 · 滞销样机清理明细",
     "share": FILE_SHARE, "fileId": FILE_ID, "url": ONLINE_SHARE},
    {"kind": "price", "name": "2026年10月价格表 · LYDF（型号价格表 / 美的）",
     "share": PRICE_SHARE_CODE, "fileId": "", "url": PRICE_SHARE},
]

CLEAN_XLSX = "data.xlsx"
PRICE_XLSX = "price.xlsx"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# ------------------------------------------------------------------ 补贴表（深圳以旧换新集团商品中心）
# 金山分享链接：https://www.kdocs.cn/l/cid8gbTzhB4c（不是公开表 —— 需要金山登录，
# 在软件里登录一次 kdocs 即可，cookie 是持久化的，以后不用再登）。
# 解析：按表头找「型号 / 商品编码 / 零售价」三列，型号归一化后与主表匹配。
SUBSIDY_SHARE = "https://www.kdocs.cn/l/cid8gbTzhB4c"
SUBSIDY_XLSX = "subsidy.xlsx"
SUBSIDY_FID_CACHE = "subsidy_fileid.txt"

# ------------------------------------------------------------------ 云端中转（Cloudflare Worker）
# 2026-10-08 用户口径：用友库存「我更新了全员跟着更新」——
#   管理号的机器拉用友 → 整包上传 worker（KV stocklive）；子账号不碰用友，直接从云端取。
#   子账号从此**不用登用友**；管理号机器仍是唯一的用友登录点。
CLOUD_HOST = "https://auth.baozang.kdns.fr"
_CLOUD_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
             " (KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# ------------------------------------------------------------------ 用友 YonSuite：实时库存（走内嵌窗口，登录一次一直用）

# 数据接口（2026-10-07 实测打通）：
#   POST /yonbip-scm-stock/report/list  condition 给空 = 默认查询（全仓库类型 ~8161 行）
#   必带头：Domain-Key: ustock + X-XSRF-TOKEN（取自 XSRF-TOKEN cookie）
#   在「已经是 c3.yonyoucloud.com 域」的窗口里跑同步 XHR —— 同源自动带登录态，
#   登录一次存在 WebView2 用户目录里，以后点更新直接拉，不用再登录。
# ⚠ 入口必须走带 service 参数的 CAS（sysid=diwork & service=c2/login）：
#   直接开 c3 的话，登录后缺 diwork 的 SSO 票据链，页面能看但 API 全 401（实测踩过）。
YY_CAS_ENTRY = ("https://euc.yonyoucloud.com/cas/login?sysid=diwork&multiCas=true"
                "&service=https%3A%2F%2Fc2.yonyoucloud.com%2Flogin")
YY_LIST_PATH = "/yonbip-scm-stock/report/list?locale=zh_CN&serviceCode=stock_stockanalysislist&terminalType=1"
YY_GROUP_SCHEMA = "2118595716194503889"
YY_TPLID = "2159497746319933444"
YY_K_MODEL = "oid_userDefine_2159497746314166282"   # 型号
YY_K_QTY = "currentqty"                             # 现存量
YY_K_AVAIL = "availableqty"                         # 可用量
YY_K_WHTYPE = "oid_userDefine_2169743811705569285"  # 仓库类型
YY_K_WH = "warehouse_name"                          # 仓库名
YY_K_SKUNAME = "productsku_skuName"                 # 物料名称
YY_PAGE_SIZE = 1000
YY_PAGE_MAX = 15            # 安全上限（8161 行 / 1000 ≈ 9 页）
YY_STOCK_FILE = "stock_live.json"

# 免登录（2026-10 三次改版）：
# 之前：CAS 的 TGC 是会话级 cookie，软件一关就没了 → 每次重开都要重新登录。
# 现在：登录成功后把窗口里的全部 cookie（含 HttpOnly 的会话 cookie）抓下来存盘，
#       之后拉库存直接用 urllib 带 Cookie 头访问 c3 API —— 不开窗、不用登录态。
#       会话在服务器端过期时（数天~数周不等）才需要再登一次。
YY_COOKIE_FILE = "yy_cookies.json"
YY_API_HOST = "https://c3.yonyoucloud.com"

YY_USER = ""     # 用友登录账号（留空 = 从云端/本地配置取；也可写 wp()/yy_auth.json）
YY_PASS = ""

# 固定端口：localStorage 是按 origin 隔离的，端口一变用户标的「已发布」「我的定价」就全丢
PORTS = [17321, 17322, 17323, 17324, 17325, 17326, 17327, 17328]
HOST = "127.0.0.1"

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
