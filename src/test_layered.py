# -*- coding: utf-8 -*-
"""最小验证：tkinter 窗口 + UpdateLayeredWindow 逐像素透明是否真的生效。

画一张：半透明蓝色圆 + 不透明红方块 + 完全透明四角。
跑 2 秒后自动退出；期间由外部截图验证。
"""
import os
import sys
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from PIL import Image, ImageDraw
import win_layered as WL

W, H = 260, 200
POS = (60, 60)

root = tk.Tk()
root.overrideredirect(True)
root.geometry("%dx%d+%d+%d" % (W, H, POS[0], POS[1]))
root.attributes("-topmost", True)
root.config(bg="#123456")
cv = tk.Canvas(root, width=W, height=H, highlightthickness=0, bd=0, bg="#123456")
cv.pack()
root.update_idletasks()
root.update()

hwnd = root.winfo_id()
# winfo_id 给的是 Tk 内部子窗口；取它的父窗口（真正的顶层窗口）
parent = WL.user32.GetParent(hwnd)
if parent:
    hwnd = parent

print("hwnd =", hwnd, "layered available:", WL.available())
surf = WL.LayeredSurface(hwnd)

img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.ellipse([20, 20, 140, 140], fill=(40, 110, 220, 130))     # 半透明蓝圆
d.rectangle([150, 40, 240, 130], fill=(230, 60, 60, 255))    # 不透明红块
d.ellipse([60, 100, 200, 180], outline=(255, 255, 255, 255), width=3)
ok = surf.paint(img, POS[0], POS[1])
print("paint ok =", ok)

# Tk 自己也要重绘一次，若分层窗口生效则会覆盖 Tk 的内容
def tick(n=0):
    if n > 40:
        root.destroy()
        return
    surf.paint(img, POS[0], POS[1])
    root.after(50, lambda: tick(n + 1))

tick()
root.mainloop()
print("done")
