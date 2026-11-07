# -*- coding: utf-8 -*-
"""preview_bubble.py —— 单独渲染头顶气泡，不开窗口，方便快速调版式

用法：python tools/preview_bubble.py [输出png]
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from PIL import Image                      # noqa: E402
import barlib                              # noqa: E402
import wb_usage                            # noqa: E402

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "docs", "bubble-preview.png")
S = 2.2


def main():
    st = wb_usage.Stats()
    st.today_credit = 88.0
    ctx = {
        "stats": st,
        "quota_left": 4158.06,
        "quota_expire_left": 1496.99,
        "quota_expire": "2026-11-30 20:48:52",
        "quota_status": "已同步",
        "quota_synced_at": 1793606400,
    }
    b = barlib.BubbleRenderer(S)
    body = b.render_body(ctx)
    dots = b.render_dots(body.width, 0.0)
    W = body.width
    H = body.height + dots.height
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    canvas.alpha_composite(body, (0, 0))
    canvas.alpha_composite(dots, (0, body.height))

    bg = Image.new("RGBA", (W + 60, H + 60), (22, 28, 48, 255))
    bg.alpha_composite(canvas, (30, 30))
    bg.convert("RGB").save(OUT)
    print("写成", OUT, bg.size)


if __name__ == "__main__":
    main()
