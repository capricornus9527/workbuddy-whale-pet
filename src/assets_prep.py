# -*- coding: utf-8 -*-
"""assets_prep.py —— 把下载来的鲸鱼娘差分贴图处理成桌宠可直接用的素材

来源：https://github.com/Vast-Beyond-Space/pet-app  （女仆装贴图，CC BY-NC-SA 4.0）
      这批图本来就是透明底 PNG，所以这里只做「裁剪 + 等比缩放 + 命名归一」，
      不做抠图（避免二次损失）。

处理内容：
    1. 按 alpha 通道裁掉四周空白（顺带解决不同图留白不一致导致的对不齐）
    2. 等比缩放到最长边 <= MAX_SIDE
    3. 统一改成 ASCII 文件名（避免不同系统/编码下的路径问题）
    4. 输出 assets/pet/*.png 与 assets/pet/index.json
"""
from __future__ import annotations

import json
import os
import shutil
import sys

import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "ref", "petapp")
DST = os.path.join(ROOT, "assets", "pet")
MAX_SIDE = 460

# 中文名 -> ASCII 名
NAME_MAP = {
    "pet": "base",
    "mood_兴奋": "excited",
    "mood_好奇": "curious",
    "mood_害羞": "shy",
    "mood_惊讶": "surprised",
    "mood_撒娇": "coquettish",
    "mood_无语": "speechless",
    "mood_生气": "angry",
    "mood_难过": "sad",
    "mood_鼓励": "encouraging",
    "浮窗_发呆": "dazed",
    "浮窗_吃饭": "eating",
    "浮窗_吃饼干": "cookie",
    "浮窗_嘴馋": "craving",
    "浮窗_工作": "working",
    "浮窗_生气": "angry_scene",
    "浮窗_睡觉": "sleeping",
    "被拖动": "dragged",
}

# 全身 / 特写分组（供界面决定摆放尺度）
FULL_BODY = {"excited", "curious", "shy", "surprised", "speechless",
             "angry", "sad", "encouraging", "dragged"}


def alpha_bbox(im: Image.Image, thresh=8):
    a = np.asarray(im.split()[3])
    ys, xs = np.where(a > thresh)
    if len(xs) == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def head_width(im: Image.Image, lo=0.05, hi=0.40):
    """估算「头宽」：取画面上部 5%~40% 行区间里，每行不透明像素数的最大值。

    为什么需要它：
        直接按外框等比缩放，会让「全身图」和「胸像图」在桌面上看起来一会儿大
        一会儿小（同一个角色，外框比例 0.56 vs 1.00，差近一倍）。
        改成按头宽归一后，所有贴图的角色头部在屏幕上大小一致，切换就不会
        有「小人 / 大人」的跳变了。
    """
    a = np.asarray(im.split()[3]) > 16
    H, W = a.shape
    if H < 8:
        return W
    seg = a[int(H * lo):max(int(H * lo) + 1, int(H * hi))]
    if seg.size == 0:
        return W
    return int(seg.sum(axis=1).max())


def process(src_path, out_path, pad_ratio=0.02):
    im = Image.open(src_path).convert("RGBA")
    bb = alpha_bbox(im)
    if bb:
        p = int(max(bb[2] - bb[0], bb[3] - bb[1]) * pad_ratio)
        bb = (max(0, bb[0] - p), max(0, bb[1] - p),
              min(im.width, bb[2] + p), min(im.height, bb[3] + p))
        im = im.crop(bb)
    if max(im.size) > MAX_SIDE:
        k = MAX_SIDE / max(im.size)
        im = im.resize((max(1, round(im.width * k)), max(1, round(im.height * k))),
                       Image.LANCZOS)
    im.save(out_path, optimize=True)
    return im


def classify(im):
    """按构图把贴图分组：full=全身立绘 / portrait=胸像·场景

    同一组内构图一致，桌宠同一时刻只用一组，才能彻底消除形态跳变。
    """
    aspect = im.width / im.height
    return "full" if aspect < 0.62 else "portrait"


def main():
    if not os.path.isdir(SRC):
        print("缺少素材目录:", SRC)
        return 1
    os.makedirs(DST, exist_ok=True)
    index = {}
    total = 0
    for f in sorted(os.listdir(SRC)):
        if not f.lower().endswith(".png"):
            continue
        stem = os.path.splitext(f)[0]
        name = NAME_MAP.get(stem)
        if not name:
            print("跳过未映射:", f)
            continue
        out = os.path.join(DST, name + ".png")
        im = process(os.path.join(SRC, f), out)
        hw = head_width(im)
        form = classify(im)
        index[name] = {
            "file": name + ".png",
            "size": list(im.size),
            "head_w": hw,
            "head_ratio": round(hw / im.width, 4),
            "form": form,
            "full_body": name in FULL_BODY,
            "source": f,
        }
        total += os.path.getsize(out)
        print("%-14s <- %-16s %-12s %-6s head/width=%.3f" % (name, f, "%dx%d" % im.size,
                                                             form, hw / im.width))
    with open(os.path.join(DST, "index.json"), "w", encoding="utf-8") as fp:
        json.dump({
            "sprites": index,
            "license": "CC BY-NC-SA 4.0",
            "license_url": "https://creativecommons.org/licenses/by-nc-sa/4.0/legalcode.zh-hans",
            "commercial_use": False,
            "source": "https://github.com/Vast-Beyond-Space/pet-app",
            "source_path": "electron-lite/img/女仆装/",
            # 授权链（完整说明见仓库根目录 NOTICE.md）。
            # CC BY-NC-SA 4.0 要求：署名、非商业性使用、衍生作品相同方式共享。
            "attribution": [
                {"role": "原创角色「溟月」(2025-06)",
                 "author": "上善无形",
                 "url": "https://space.bilibili.com/4456176"},
                {"role": "「女仆鲸鱼娘」女仆装版本 (2026-04-25)",
                 "author": "ZipZipPipe",
                 "url": "https://space.bilibili.com/4168597"},
                {"role": "贴图整理公开与许可声明",
                 "author": "Vast-Beyond-Space",
                 "url": "https://github.com/Vast-Beyond-Space/pet-app"},
            ],
            "notice": "这些贴图不是本项目原创，仅限非商业使用；衍生作品须以 CC BY-NC-SA 4.0 发布。",
        }, fp, ensure_ascii=False, indent=2)
    forms = {}
    for n, i in index.items():
        forms.setdefault(i["form"], []).append(n)
    for k, v in forms.items():
        print("分组 %-9s %d 张: %s" % (k, len(v), " ".join(sorted(v))))
    print("共 %d 张，处理后合计 %.1f MB" % (len(index), total / 1e6))
    return 0


if __name__ == "__main__":
    sys.exit(main())
