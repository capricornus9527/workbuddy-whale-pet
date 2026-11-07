# -*- coding: utf-8 -*-
"""live_check.py —— 实机连拍：验证「差分切换是否顺滑、有没有大小跳变」

做法：
    1. 用 PET_TEST_FAST=1 把桌宠节奏压缩（空闲差分 2~3.6 秒就换一次）
    2. 起进程后每 120ms 全屏截一张，连拍 34 张
    3. 只在宠物窗口区域内做帧间差异统计：
          - 最大帧间差异 / 中位帧间差异   → 越小越顺（>3 说明有突跳）
          - 突跳帧数
          - 每帧「内容高度」序列          → 检查有没有小人/大人式跳变
"""
from __future__ import annotations

import glob
import os
import subprocess
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)



def main():
    py = os.path.join(ROOT, "tools", "venv-tk", "Scripts", "python.exe")
    env = dict(os.environ, PET_TEST_FAST="1")     # 压缩节奏，便于抓到切换过程

    # ---- 直接分析桌宠自己导出的每一帧（不受桌面背景干扰）
    from PIL import Image
    dump = os.path.join(ROOT, "ref", "dump")
    os.makedirs(dump, exist_ok=True)
    t0 = time.time()   # 用 mtime 判定「本次新增」：桌宠每次启动都从 00000.png 重新
                       # 编号并覆盖旧帧，靠文件总数增长会算错（连跑两次必失败）
    env2 = dict(env, PET_DUMP_DIR=dump)
    p2 = subprocess.Popen([py, os.path.join(HERE, "pet.py")], cwd=ROOT, env=env2,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(14)
    try:
        import subprocess as _sp
        _sp.run(["taskkill", "/T", "/F", "/PID", str(p2.pid)],
                capture_output=True, timeout=10)
    except Exception:
        pass
    try:
        p2.communicate(timeout=4)
    except Exception:
        pass

    files = sorted(f for f in glob.glob(os.path.join(dump, "*.png"))
                   if os.path.getmtime(f) >= t0 - 1)
    print("本次导出帧数:", len(files))
    if len(files) < 10:
        print("帧太少，无法分析")
        return 1
    arrs = []
    for f in files:
        try:
            arrs.append(np.asarray(Image.open(f).convert("RGBA"), dtype=np.int16))
        except Exception:
            continue      # taskkill 时可能留下写了一半的帧，跳过
    if len(arrs) < 10:
        print("可读帧太少，无法分析")
        return 1
    # 信息条行数变化会让窗口高度变化，先统一裁到最小公共尺寸再比较
    mh = min(a.shape[0] for a in arrs)
    mw = min(a.shape[1] for a in arrs)
    arrs = [a[:mh, :mw] for a in arrs]
    # 信息条行数变化会让窗口高度变化，先统一裁到最小公共尺寸再比较
    mh = min(a.shape[0] for a in arrs)
    mw = min(a.shape[1] for a in arrs)
    arrs = [a[:mh, :mw] for a in arrs]

    # 1) 轮廓尺寸是否稳定（有没有小人/大人跳变）
    boxes = []
    for a in arrs:
        m = a[:, :, 3] > 16
        ys, xs = np.where(m)
        boxes.append((int(ys.max() - ys.min()) if len(ys) else 0,
                      int(xs.max() - xs.min()) if len(xs) else 0))
    hh = [b[0] for b in boxes]
    ww = [b[1] for b in boxes]
    h_span = (max(hh) - min(hh)) / max(1, int(np.median(hh))) * 100
    w_span = (max(ww) - min(ww)) / max(1, int(np.median(ww))) * 100
    print("轮廓高度 中位 %d，波动 %.1f%%" % (int(np.median(hh)), h_span))
    print("轮廓宽度 中位 %d，波动 %.1f%%" % (int(np.median(ww)), w_span))

    # 2) 逐帧差异：切换时应该平滑上升再回落，绝不能出现单帧大跳
    ds = [float(np.abs(arrs[i + 1] - arrs[i]).mean()) for i in range(len(arrs) - 1)]
    med = float(np.median(ds))
    print("帧间差异 中位 %.2f，最大 %.2f，最大/中位 %.2f"
          % (med, max(ds), max(ds) / max(1e-6, med)))
    print("突跳帧(>中位*8 且 >3): %d" % sum(1 for d in ds if d > max(3.0, med * 8)))

    ok = (w_span < 20 and h_span < 30 and max(ds) / max(1e-6, med) < 12)
    print("综合判定：", "通过 ✅" if ok else "不通过 ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
