# -*- coding: utf-8 -*-
"""prep_sprites.py —— 把「单色底的角色图」处理成可用的桌宠差分素材

处理链：
    1. 从四角估计背景色
    2. 从图像边框做泛洪（flood fill），只抠掉与边框连通的背景
       —— 这样角色身上大片浅色（白围裙、浅色头发）不会被误删
    3. 按色差羽化边缘，去白边
    4. 裁到角色包围盒 + 统一画布尺寸（保持比例、底部对齐）
    5. 输出透明 PNG

用法：
    python prep_sprites.py in.webp out.png
    python prep_sprites.py --all            # 处理 ref/whalechan 下全部图
"""
from __future__ import annotations

import glob
import os
import sys

import numpy as np
from PIL import Image, ImageFilter, ImageDraw, ImageChops

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def estimate_bg(arr):
    h, w = arr.shape[:2]
    k = max(4, min(h, w) // 40)
    corners = np.concatenate([
        arr[:k, :k].reshape(-1, 3), arr[:k, -k:].reshape(-1, 3),
        arr[-k:, :k].reshape(-1, 3), arr[-k:, -k:].reshape(-1, 3)])
    return np.median(corners, axis=0)


def cutout(img: Image.Image, tol=26.0, feather=1.4, grow=1):
    """返回 (rgba, mask)，mask 为 255=角色"""
    rgb = np.asarray(img.convert("RGB"), dtype=np.float32)
    h, w = rgb.shape[:2]
    bg = estimate_bg(rgb)
    dist = np.sqrt(((rgb - bg) ** 2).sum(axis=2))

    # 边缘连通泛洪（用低分辨率做连通性判断，再放大，够快也够准）
    small = (dist < tol).astype(np.uint8)
    sm = Image.fromarray(small * 255, "L")
    # 用形态学膨胀 + 逐行扫描近似连通域：这里直接用 PIL 的 floodfill
    flood = Image.new("L", (w, h), 0)
    fd = ImageDraw.Draw(flood)
    seeds = []
    step = max(2, min(w, h) // 128)
    for x in range(0, w, step):
        seeds += [(x, 0), (x, h - 1)]
    for y in range(0, h, step):
        seeds += [(0, y), (w - 1, y)]
    bgmask = sm.copy()
    for s in seeds:
        if bgmask.getpixel(s) > 127 and flood.getpixel(s) == 0:
            ImageDraw.floodfill(bgmask, s, 128, thresh=10)

    bg_only = bgmask.point(lambda v: 255 if v == 128 else 0)
    if grow:
        bg_only = bg_only.filter(ImageFilter.MaxFilter(grow * 2 + 1))
    fg = ImageChops.invert(bg_only)
    if feather:
        fg = fg.filter(ImageFilter.GaussianBlur(feather))

    out = img.convert("RGBA")
    out.putalpha(fg)
    return out, fg


def despill(rgba: Image.Image, bg_color, strength=0.85):
    """去掉边缘残留的背景色（半透明像素往角色色偏）"""
    a = np.asarray(rgba, dtype=np.float32)
    alpha = a[:, :, 3:4] / 255.0
    bg = np.array(bg_color, dtype=np.float32).reshape(1, 1, 3)
    # 反推：观测色 = 角色色*a + 背景色*(1-a)  =>  角色色 = (观测 - 背景*(1-a))/a
    safe = np.clip(alpha, 0.15, 1.0)
    rgb = (a[:, :, :3] - bg * (1 - alpha)) / safe
    rgb = np.clip(rgb, 0, 255)
    blend = np.clip((1 - alpha) * strength, 0, 1)
    a[:, :, :3] = a[:, :, :3] * (1 - blend) + rgb * blend
    return Image.fromarray(a.astype(np.uint8), "RGBA")


def trim(rgba: Image.Image, pad_ratio=0.06):
    a = np.asarray(rgba.split()[3])
    ys, xs = np.where(a > 8)
    if len(xs) == 0:
        return rgba
    x0, x1, y0, y1 = xs.min(), xs.max(), ys.min(), ys.max()
    p = int(max(x1 - x0, y1 - y0) * pad_ratio)
    box = (max(0, x0 - p), max(0, y0 - p),
           min(rgba.width, x1 + p + 1), min(rgba.height, y1 + p + 1))
    return rgba.crop(box)


def process(path, out_path, tol=26.0, feather=1.4, max_side=520):
    img = Image.open(path)
    bg = estimate_bg(np.asarray(img.convert("RGB"), dtype=np.float32))
    rgba, _ = cutout(img, tol=tol, feather=feather)
    rgba = despill(rgba, bg)
    rgba = trim(rgba)
    if max(rgba.size) > max_side:
        k = max_side / max(rgba.size)
        rgba = rgba.resize((max(1, int(rgba.width * k)), max(1, int(rgba.height * k))),
                           Image.LANCZOS)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    rgba.save(out_path)
    return rgba.size, tuple(int(v) for v in bg)


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--all":
        src = sys.argv[2] if len(sys.argv) > 2 else os.path.join(ROOT, "ref", "whalechan")
        dst = os.path.join(ROOT, "ref", "cut")
        for f in sorted(glob.glob(os.path.join(src, "*.webp")) +
                        glob.glob(os.path.join(src, "*.png"))):
            try:
                size, bg = process(f, os.path.join(dst, os.path.splitext(os.path.basename(f))[0] + ".png"))
                print("%-58s -> %s  bg=%s" % (os.path.basename(f)[:56], size, bg))
            except Exception as e:
                print("FAIL", f, e)
    else:
        size, bg = process(sys.argv[1], sys.argv[2])
        print("ok", size, "bg", bg)
