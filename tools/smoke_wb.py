# -*- coding: utf-8 -*-
"""smoke_wb.py —— 实机冒烟：启动桌宠几秒，导出真实渲染帧后自动退出

用法：
    python tools/smoke_wb.py [毫秒] [导出目录]

不做任何交互，用于确认「窗口能建起来 / 贴图能渲染 / 数据能读到」。
"""

from __future__ import annotations

import os
import sys

MS = int(sys.argv[1]) if len(sys.argv) > 1 else 4000
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DUMP = sys.argv[2] if len(sys.argv) > 2 else os.path.join(ROOT, "docs", "smoke")

os.environ["PET_DUMP_DIR"] = DUMP
os.environ["PET_TEST_FAST"] = "1"
sys.path.insert(0, os.path.join(ROOT, "src"))

import pet                                     # noqa: E402


def main():
    app = pet.PetApp()
    force = os.environ.get("PET_FORM")
    if force:
        app._set_form(force)
        print("[smoke] 强制形态 =", force, flush=True)
    print("[smoke] stats: has_data=%s today=%.2f turn=%.2f ctx=%d/%d"
          % (app.stats.has_data, app.stats.today_credit, app.stats.turn_credit,
             app.stats.context_used, app.stats.context_size), flush=True)
    app.root.after(MS, app._quit)
    app.run()
    n = len([f for f in os.listdir(DUMP)]) if os.path.isdir(DUMP) else 0
    print("[smoke] 导出 %d 帧 -> %s" % (n, DUMP))


if __name__ == "__main__":
    main()
