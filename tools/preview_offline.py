# -*- coding: utf-8 -*-
"""preview_offline.py —— 不启动窗口，直接合成「角色 + 信息条」预览图

用途：改完渲染逻辑后先看一眼再实机跑，避免反复弹窗。
两种形态各出一张，方便对比动画帧与静态差分的观感差异。
"""

from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from PIL import Image                      # noqa: E402
import barlib                              # noqa: E402
import wb_usage                            # noqa: E402

SPRITE_DIR = os.path.join(ROOT, "assets", "pet")
ANIM_DIR = os.path.join(ROOT, "assets", "anim")

BOX_W, BOX_H = 180, 186
PAD_X, PAD_TOP, PAD_BOT = 16, 14, 6
GAP = 4
S = 1.6                                    # 模拟 160% DPI 下的实际观感

ACCENT = (96, 158, 245)


def fit(im, bw, bh):
    k = min(bw / float(im.width), bh / float(im.height))
    return im.resize((max(1, int(round(im.width * k))),
                      max(1, int(round(im.height * k)))), Image.LANCZOS)


def head_norm(im, info, bw, bh, head_target):
    k = min(bw / float(im.width), bh / float(im.height))
    hw = max(1.0, info.get("head_w", im.width * 0.7)) * k
    k *= max(0.62, min(1.35, head_target / hw))
    k = min(k, bw / float(im.width), bh / float(im.height))
    return im.resize((max(1, int(round(im.width * k))),
                      max(1, int(round(im.height * k)))), Image.LANCZOS)


def compose(form, stats, index, anim, frame=0):
    s = S
    bw, bh = int(BOX_W * s), int(BOX_H * s)
    pad_x = max(2, int(PAD_X * s))
    pad_top = max(2, int(PAD_TOP * s))
    pad_bot = max(2, int(PAD_BOT * s))
    win_w = bw + pad_x * 2

    ctx = {
        "stats": stats, "peak": False,
        "period": stats.display_model or "WorkBuddy",
        "period_sub": "今日 %.2f 积分 · %d 轮" % (stats.today_credit, stats.turns_today),
        "cpu": 12.0, "mem": 58.0, "clock": "15:40",
    }
    ctx["quota_left"] = 4158.06
    ctx["quota_expire_left"] = 3658.06
    ctx["quota_expire"] = "2026-11-30"
    ctx["quota_status"] = "已同步"
    ctx["quota_synced_at"] = 1793606400
    bar = barlib.BarRenderer(s).render(
        ctx, [["session"], ["speed", "cache", "cost"]],
        win_w - int(6 * s), ACCENT)

    bubble = barlib.BubbleRenderer(s)
    bubble_h = max(0, bubble.height() - pad_top)
    area_h = pad_top + bubble_h + bh + pad_bot
    win_h = area_h + int(round(GAP * s)) + bar.height
    canvas = Image.new("RGBA", (win_w, win_h), (0, 0, 0, 0))

    if form == "anim":
        st = anim["states"]["idle"]
        f = st["frames"][frame]
        src = Image.open(os.path.join(ANIM_DIR, "idle", f)).convert("RGBA")
        char = fit(src, bw, bh)
    else:
        info = index["base"]
        src = Image.open(os.path.join(SPRITE_DIR, info["file"])).convert("RGBA")
        target = bw * 0.62
        char = head_norm(src, info, bw, bh, target)

    body = bubble.render_body(ctx)
    bx = max(0, (win_w - body.width) // 2)
    canvas.alpha_composite(body, (bx, 0))
    canvas.alpha_composite(bubble.render_dots(body.width, 0.0), (bx, body.height))

    x = (win_w - char.width) // 2
    y = max(0, area_h - char.height)
    canvas.alpha_composite(char, (x, y))
    canvas.alpha_composite(bar, ((win_w - bar.width) // 2, area_h + int(round(GAP * s))))
    return canvas


def main():
    with open(os.path.join(SPRITE_DIR, "index.json"), encoding="utf-8") as f:
        index = json.load(f)["sprites"]
    with open(os.path.join(ANIM_DIR, "index.json"), encoding="utf-8") as f:
        anim = json.load(f)

    stats = wb_usage.UsageTracker().poll()
    print("统计：今日 %.2f 积分 / 本轮 %.2f / 上下文 %d/%d / %d 轮"
          % (stats.today_credit, stats.turn_credit,
             stats.context_used, stats.context_size, stats.turns_today))

    outs = []
    for form in ("anim", "portrait"):
        img = compose(form, stats, index, anim)
        bg = Image.new("RGBA", (img.width + 40, img.height + 40), (18, 22, 34, 255))
        bg.alpha_composite(img, (20, 20))
        p = os.path.join(ROOT, "docs", "preview-%s.png" % form)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        bg.convert("RGB").save(p)
        outs.append(p)
        print("写出", p, img.size)

    # 并排对照
    imgs = [Image.open(p).convert("RGB") for p in outs]
    h = max(i.height for i in imgs)
    w = sum(i.width for i in imgs) + 20 * (len(imgs) + 1)
    sheet = Image.new("RGB", (w, h + 40), (18, 22, 34))
    x = 20
    for i in imgs:
        sheet.paste(i, (x, 20))
        x += i.width + 20
    p = os.path.join(ROOT, "docs", "preview-compare.png")
    sheet.save(p)
    print("写出", p, sheet.size)


if __name__ == "__main__":
    main()
