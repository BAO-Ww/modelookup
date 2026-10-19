# -*- coding: utf-8 -*-
"""闲鱼配文 · 一键发布到闲鱼（半自动：自动填好 + 用户最后点发布）

依赖：pip install playwright && playwright install chromium

目标页：goofish.com/publish（闲鱼新版"发闲置"页）
- 无独立"标题"输入框：闲鱼从描述第一行自动提炼标题，所以把标题拼到描述首行
- 图片：点"+ 添加图片"露出 input[type=file] 后 set_input_files
- 定位：优先用可见文案/占位符（get_by_placeholder / get_by_text），不猜 CSS 类名
"""
import os, sys, json, time, traceback

PUBLISH_URL = "https://www.goofish.com/publish"


def _log(s):
    print(s, flush=True)


def _is_logged_in(page):
    try:
        text = page.content()
    except Exception:
        return False
    bad = ("扫一扫登录", "扫码登录", "请先登录", "请登录", "登录后可发布",
           "非法访问", "请使用正常浏览器")
    return not any(b in text for b in bad)


def _find_desc(page, timeout=20000):
    """定位描述输入区（textarea 或 contenteditable），按占位符/可见文案匹配"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for sel in [
            page.get_by_placeholder("描述一下"),            # 新版占位符
            page.get_by_placeholder("宝贝描述"),
            'textarea[placeholder*="描述"]',
            'div[contenteditable="true"]',
            'textarea',
            '[role="textbox"]',
        ]:
            try:
                loc = sel.first
                if loc.count() > 0 and loc.is_visible():
                    return loc
            except Exception:
                continue
        time.sleep(0.5)
    return None


def _fill(loc, value):
    """向 textarea / contenteditable / input 写入内容"""
    loc.click()
    time.sleep(0.3)
    try:
        # 全选清空，再输入
        loc.press("Control+A")
        loc.press("Backspace")
    except Exception:
        pass
    try:
        # contenteditable 用键盘输入；input/textarea 用 fill 更稳
        loc.fill(value)
        return True
    except Exception:
        pass
    try:
        loc.type(value, delay=5)
        return True
    except Exception:
        try:
            loc.press_sequentially(value, delay=5)
            return True
        except Exception:
            return False


def _upload_images(page, imgs, log=_log):
    """上传图片：先点添加图片按钮露出 file input，再 set_input_files"""
    # 尝试直接找 file input
    fi = page.locator('input[type="file"]').first
    if fi.count() > 0 and fi.is_visible():
        try:
            fi.set_input_files(imgs)
            log("直接命中 file input：%d 张" % len(imgs))
            return True
        except Exception as e:
            log("直接 set_input_files 失败：%s" % e)
    # 点"添加图片"按钮
    for txt in ["＋添加图片", "+添加图片", "添加图片", "添加圖片", "上传图片", "添加"]:
        try:
            btn = page.get_by_text(txt, exact=False).first
            if btn.count() > 0 and btn.is_visible():
                btn.click()
                time.sleep(1.2)
                break
        except Exception:
            continue
    # 按钮点击后再找 file input（可能是隐藏的 input）
    fi = page.locator('input[type="file"]').first
    if fi.count() > 0:
        try:
            fi.set_input_files(imgs)
            log("已通过按钮+file input 上传 %d 张" % len(imgs))
            return True
        except Exception as e:
            log("set_input_files 失败：%s" % e)
    # 兜底：所有 file input 都试一遍
    all_fi = page.locator('input[type="file"]')
    n = all_fi.count()
    if n > 0:
        for i in range(min(n, 6)):
            try:
                all_fi.nth(i).set_input_files([imgs[0]])
                time.sleep(0.6)
                log("file input[%d] 上传成功" % i)
                return True
            except Exception:
                continue
    log("未找到任何文件上传控件（可能页面未加载完或已改版）")
    return False


def main_from_task(task_path):
    payload = json.load(open(task_path, encoding="utf-8"))
    sel_dir = payload["sel_dir"]
    version = payload.get("version", "B")
    info = payload["info"]
    desc = payload["desc"]
    titles = payload["titles"]

    imgs = sorted([os.path.join(sel_dir, f) for f in os.listdir(sel_dir)
                   if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))])

    # 标题放描述首行 -> 闲鱼自动提炼为商品标题
    title_to_use = titles[1] if len(titles) > 1 else titles[0]
    full_desc = title_to_use + "\n\n" + (desc or "")

    _log("=" * 50)
    _log("闲鱼配文 · 一键发布到闲鱼")
    _log("型号: %s" % info.get("model"))
    _log("品牌: %s" % info.get("brand"))
    _log("官网价: %s" % (("¥" + format(info.get("price", 0), ",")) if info.get("price") else "—"))
    _log("话术版本: %s" % ("报备版" if version == "B" else "全国版"))
    _log("图片张数: %d" % len(imgs))
    _log("=" * 50)

    profile_dir = os.path.join(os.path.expanduser("~"), "XianyuPeiwenProfile")
    os.makedirs(profile_dir, exist_ok=True)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _log("未检测到 playwright，无法自动填表。")
        _log("请在弹出的浏览器里手动操作后点【立即发布】。")
        try:
            import webbrowser
            webbrowser.open(PUBLISH_URL)
            input("已用默认浏览器打开发布页，按回车退出...")
        except Exception:
            pass
        return

    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            profile_dir, headless=False,
            args=["--no-sandbox", "--disable-blink-features=automation-controlled"])
        page = browser.pages[0] if browser.pages else browser.new_page()
        _log("打开发布页：%s" % PUBLISH_URL)
        page.goto(PUBLISH_URL, wait_until="domcontentloaded")
        time.sleep(3)

        if not _is_logged_in(page):
            _log("\n" + "!" * 50)
            _log("检测到未登录（或触发风控）。请在浏览器窗口中扫码登录闲鱼。")
            _log("登录后会自动继续；本窗口保持开着。")
            _log("!" * 50 + "\n")
            for _ in range(150):
                time.sleep(2)
                try:
                    if _is_logged_in(page):
                        _log("登录成功 ✓")
                        break
                except Exception:
                    pass
            else:
                _log("等待登录超时（5 分钟）。")
                try:
                    browser.close()
                except Exception:
                    pass
                return

        # 等发布表单出现
        _log("等待发布表单 ...")
        desc_loc = _find_desc(page)
        if not desc_loc:
            _log("✗ 未找到描述输入框（页面结构可能已变或未加载），请手动填写。")
        else:
            ok = _fill(desc_loc, full_desc)
            _log(("✓ 描述已填（含标题首行）" if ok else "✗ 描述填写失败，请手动") + "\n  首行标题: " + title_to_use)

        # 图片上传
        _log("上传图片 ...")
        _upload_images(page, imgs)

        # 等图片/内容渲染，截图给用户看结果
        time.sleep(3)
        try:
            preview = os.path.join(os.path.dirname(task_path), "_publish_preview.png")
            page.screenshot(path=preview, full_page=True)
            _log("\n截图已保存：%s" % preview)
        except Exception as e:
            _log("截图失败：%s" % e)

        _log("\n" + "=" * 50)
        _log("已尝试自动填好描述（含标题）+ 上传图片。")
        _log("请在浏览器里检查：")
        _log("  1) 选好【分类】（冰箱/洗衣机/家电 ...）")
        _log("  2) 确认标题（闲鱼从描述首行自动提炼，可手动改）")
        _log("  3) 检查图片是否上传成功")
        _log("  4) 填好【售价】")
        _log("  5) 点【立即发布】")
        _log("=" * 50)
        try:
            input("\n浏览器保持开着。按回车关闭本窗口（浏览器不会关）...")
        except EOFError:
            pass


if __name__ == "__main__":
    if len(sys.argv) < 2:
        _log("用法: python publish_to_xianyu.py <task.json>")
        try:
            input("按回车退出...")
        except EOFError:
            pass
        sys.exit(1)
    try:
        main_from_task(sys.argv[1])
    except Exception as e:
        _log("运行出错：%s" % e)
        traceback.print_exc()
        try:
            input("按回车退出...")
        except EOFError:
            pass
