# -*- coding: utf-8 -*-
"""build_anim.py —— 把 8x11 精灵图集切好的帧整理成桌宠可用的动画状态

上游素材：ChatGPT Pet v2 图集（192x208 格，9 组动作 + 16 方向）。
本脚本只取 9 组动作，做两件上游没做的事：

1. **按状态求并集 bbox 再裁剪**。逐帧单独裁剪会让角色每帧位移不同，
   播放时整体"抖"。用整组并集裁剪，保证同一状态内画布一致。
2. 生成 ``assets/anim/index.json``，记录帧序列与每帧时长（毫秒）。

用法：
    python tools/build_anim.py --src <frames根目录> --out <assets/anim>
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

from PIL import Image

# 状态名 -> (源目录, [帧时长 ms])；时长抄自图集 pet.json 的 frame_durations_ms
STATES = {
    "idle":   ("idle",          [280, 110, 110, 140, 140, 320]),
    "busy":   ("running",       [120, 120, 120, 120, 120, 220]),
    "wait":   ("waiting",       [150, 150, 150, 150, 150, 260]),
    "review": ("review",        [150, 150, 150, 150, 150, 280]),
    "jump":   ("jumping",       [140, 140, 140, 140, 280]),
    "greet":  ("waving",        [140, 140, 140, 280]),
    "sad":    ("failed",        [140, 140, 140, 140, 140, 140, 140, 240]),
    "walk_r": ("running-right", [120, 120, 120, 120, 120, 120, 120, 220]),
    "walk_l": ("running-left",  [120, 120, 120, 120, 120, 120, 120, 220]),
}


def union_bbox(images):
    left = top = 10 ** 9
    right = bottom = -1
    for im in images:
        bb = im.getbbox()
        if not bb:
            continue
        left = min(left, bb[0])
        top = min(top, bb[1])
        right = max(right, bb[2])
        bottom = max(bottom, bb[3])
    if right <= left or bottom <= top:
        return None
    return (left, top, right, bottom)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="切好帧的根目录（含 idle/ running/ ...）")
    ap.add_argument("--out", required=True, help="输出目录 assets/anim")
    ap.add_argument("--pad", type=int, default=2, help="裁剪后四周保留的透明边（像素）")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    index = {"version": 1, "states": {}}

    for name, (src_dir, durations) in STATES.items():
        d = os.path.join(args.src, src_dir)
        if not os.path.isdir(d):
            print("[skip] 缺目录 %s" % d)
            continue
        files = sorted(f for f in os.listdir(d) if f.lower().endswith(".png"))
        if not files:
            print("[skip] %s 里没有 png" % d)
            continue
        images = [Image.open(os.path.join(d, f)).convert("RGBA") for f in files]
        bb = union_bbox(images)
        if bb is None:
            print("[skip] %s 全透明" % src_dir)
            continue
        l, t, r, b = bb
        l = max(0, l - args.pad)
        t = max(0, t - args.pad)
        r = min(images[0].width, r + args.pad)
        b = min(images[0].height, b + args.pad)

        out_dir = os.path.join(args.out, name)
        if os.path.isdir(out_dir):
            shutil.rmtree(out_dir)
        os.makedirs(out_dir, exist_ok=True)

        frames = []
        for i, im in enumerate(images):
            cropped = im.crop((l, t, r, b))
            fn = "%02d.png" % i
            cropped.save(os.path.join(out_dir, fn))
            frames.append(fn)

        durs = list(durations[:len(frames)])
        while len(durs) < len(frames):
            durs.append(durs[-1])

        index["states"][name] = {
            "frames": frames,
            "durations": durs,
            "size": [r - l, b - t],
            "loop": True,
        }
        print("[ok] %-7s %2d 帧  裁剪 %dx%d  (原格 192x208)"
              % (name, len(frames), r - l, b - t))

    with open(os.path.join(args.out, "index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=1)
    total = sum(len(s["frames"]) for s in index["states"].values())
    print("\n写出 %d 个状态 / %d 帧 -> %s" % (len(index["states"]), total, args.out))


if __name__ == "__main__":
    sys.exit(main())
