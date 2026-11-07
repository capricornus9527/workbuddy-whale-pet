# -*- coding: utf-8 -*-
"""smoke_test.py —— 端到端冒烟测试：真的把桌宠跑起来、截图、再关掉

流程：
    1. 子进程启动 pet.py
    2. 等若干秒，让动画跑起来
    3. 全屏截图（screencap，纯 ctypes）
    4. 把桌宠所在区域裁出来存成 ref/smoke_*.png 供人工检查
    5. 结束后杀掉进程，打印子进程 stdout/stderr（有异常一定看得见）
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = os.path.join(ROOT, "tools", "venv-tk", "Scripts", "python.exe")
if not os.path.exists(PY):
    # 还没跑过 setup.bat，或用了系统 Python：退回当前解释器，别抛晦涩的路径错误
    PY = sys.executable
sys.path.insert(0, HERE)

import screencap  # noqa: E402


def run(wait=6.0, tag="a"):
    p = subprocess.Popen([PY, os.path.join(HERE, "pet.py")],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, cwd=ROOT)
    time.sleep(wait)
    alive = p.poll() is None
    img = screencap.grab()
    out = os.path.join(ROOT, "ref", "smoke_%s_full.png" % tag)
    os.makedirs(os.path.dirname(out), exist_ok=True)   # ref/ 不入库，首次运行要自建
    img.save(out)
    print("alive:", alive, "| full shot:", out, img.size)
    # 按 pet.config.json 里记录的位置裁剪
    try:
        import json
        cfg = json.load(open(os.path.join(ROOT, "pet.config.json"), encoding="utf-8"))
        x, y = cfg.get("x", 1000), cfg.get("y", 600)
        box = (max(0, x - 60), max(0, y - 60),
               min(img.width, x + 340), min(img.height, y + 340))
        img.crop(box).save(os.path.join(ROOT, "ref", "smoke_%s_pet.png" % tag))
        px, py = cfg.get("panel_x"), cfg.get("panel_y")
        if isinstance(px, int) and isinstance(py, int):
            img.crop((max(0, px - 20), max(0, py - 20),
                      min(img.width, px + 360), min(img.height, py + 320))
                     ).save(os.path.join(ROOT, "ref", "smoke_%s_panel.png" % tag))
    except Exception as e:
        print("crop skip:", e)

    try:
        import subprocess as _sp
        _sp.run(["taskkill", "/T", "/F", "/PID", str(p.pid)],
                capture_output=True, timeout=10)
    except Exception:
        pass
    try:
        log = p.communicate(timeout=4)[0]
    except Exception:
        log = ""
    print("--- child output ---")
    print(log or "(无输出)")
    return alive, log


if __name__ == "__main__":
    tag = sys.argv[1] if len(sys.argv) > 1 else "a"
    wait = float(sys.argv[2]) if len(sys.argv) > 2 else 6.0
    run(wait, tag)
