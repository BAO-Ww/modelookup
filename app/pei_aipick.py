# -*- coding: utf-8 -*-
"""闲鱼配文 · AI 选图+去黑底 子进程入口

调用方式：python ai_pick_runner.py <task.json>
- 在 AppData/Python313（装了 rembg 的解释器）里跑
- 进程独立：GUI 主进程是 frozen exe 不带 rembg
- 完成后把结果写到 task['sel_dir']/01_AI_official.jpg 等
"""
import os, sys, json, time, shutil, traceback


def main():
    if len(sys.argv) < 2:
        print("用法: python ai_pick_runner.py <task.json>")
        return
    task_path = sys.argv[1]
    payload = json.load(open(task_path, encoding="utf-8"))
    img_dir = payload["img_dir"]
    sel_dir = payload["sel_dir"]
    info = payload["info"]
    top_k = int(payload.get("top_k", 6))

    # 找 core 模块路径
    base = os.path.dirname(os.path.abspath(__file__))
    if not os.path.exists(os.path.join(base, "pei_core.py")):
        # 拷贝出来的情况：从 info['dir'] 所在目录同位置找
        base = os.path.dirname(task_path)
    sys.path.insert(0, base)
    import pei_core as core  # noqa: E402

    print("=" * 50)
    print("闲鱼配文 · AI 选图+去黑底")
    print("源图目录: %s" % img_dir)
    print("目标目录: %s" % sel_dir)
    print("=" * 50)

    # 检查 rembg
    rembg_ok = False
    try:
        import rembg  # noqa: F401
        rembg_ok = True
        print("rembg: 可用（去背景精度高）")
    except Exception:
        print("rembg: 不可用（用 PIL 兜底，可能效果一般）")

    # 清空旧的 AI 产物
    if os.path.isdir(sel_dir):
        for f in list(os.listdir(sel_dir)):
            if f.startswith("AI_") or f.startswith("0_主图"):
                try:
                    os.remove(os.path.join(sel_dir, f))
                except Exception:
                    pass

    # 选图
    t0 = time.time()
    picks = core.select_topk(img_dir, k=top_k)
    if not picks:
        print("✗ 没挑到合格的图（可能图全太小或都带二维码）")
        with open(task_path, "w", encoding="utf-8") as f:
            payload["result"] = {"ok": False, "error": "no_picks"}
            json.dump(payload, f, ensure_ascii=False)
        return
    print(f"✓ 选图完成，{len(picks)} 张：")
    for fn, sc, r in picks:
        print(f"  - {fn}  score={sc:.1f}  reason={r}")

    # 去黑底
    saved = 0
    for i, (fn, sc, reason) in enumerate(picks):
        src = os.path.join(img_dir, fn)
        dst = os.path.join(sel_dir, "%02d_AI_official.jpg" % (i + 1))
        print(f"[{i+1}/{len(picks)}] 处理 {fn} ...", end=" ", flush=True)
        buf = core.normalize_bg_to_bytes(src, force=False)
        if buf:
            with open(dst, "wb") as f:
                f.write(buf)
            saved += 1
            print(f"OK ({'rembg' if rembg_ok else 'PIL兜底'})")
        else:
            shutil.copy(src, dst)
            print("已够白（保留原图）")
    # 拼版主图
    try:
        cover = os.path.join(sel_dir, "0_主图_卖点拼版.jpg")
        core.make_cover(os.path.join(img_dir, picks[0][0]), info, cover)
        print("✓ 拼版主图已生成")
    except Exception as e:
        print("✗ 拼版主图失败：%s" % e)

    payload["result"] = {
        "ok": True,
        "saved": saved,
        "picks": [{"filename": fn, "score": sc, "reason": r} for fn, sc, r in picks],
        "elapsed_sec": round(time.time() - t0, 1),
    }
    with open(task_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print("=" * 50)
    print("完成 · 耗时 %.1fs · 已写入 %s" % (time.time() - t0, sel_dir))
    try:
        input("按回车退出...")
    except EOFError:
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("运行出错：%s" % e)
        traceback.print_exc()
        try:
            input("按回车退出...")
        except EOFError:
            pass
