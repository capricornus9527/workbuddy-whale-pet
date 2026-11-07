# -*- coding: utf-8 -*-
"""win_layered.py —— Windows 分层窗口（真·逐像素透明）工具

为什么需要它：
    tkinter 自带的 wm_attributes("-transparentcolor", c) 只能做「颜色键」抠图，
    抗锯齿边缘半透明像素会被合成到抠图色上，于是角色边缘会留一圈彩色毛边。
    这里改用 Win32 的 UpdateLayeredWindow 直接把 RGBA 位图交给系统合成，
    得到真正的逐像素 alpha —— 角色边缘干净，不留白边/粉边。

用法：
    win = LayeredSurface(hwnd)
    win.paint(pil_rgba_image, screen_x, screen_y)
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

from PIL import Image, ImageChops

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
GWL_EXSTYLE = -20
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
BI_RGB = 0
DIB_RGB_COLORS = 0


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
                ("SourceConstantAlpha", ctypes.c_byte), ("AlphaFormat", ctypes.c_byte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


_GetWindowLongPtr = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
_SetWindowLongPtr = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
_GetWindowLongPtr.restype = ctypes.c_ssize_t
_GetWindowLongPtr.argtypes = [wintypes.HWND, ctypes.c_int]
_SetWindowLongPtr.restype = ctypes.c_ssize_t
_SetWindowLongPtr.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]


def available() -> bool:
    """当前环境是否支持分层窗口"""
    try:
        hwnd = user32.GetDesktopWindow()
        return bool(hwnd) and bool(user32.UpdateLayeredWindow)
    except Exception:
        return False


class LayeredSurface:
    """把一个 tkinter 窗口变成可逐像素透明绘制的画布

    性能注意：DIB 段与屏幕 DC 都做复用（按尺寸缓存），每帧只做一次
    memmove + 一次 UpdateLayeredWindow。早期版本每帧 CreateDIBSection/
    DeleteObject 再加两次 ctypes 数组拷贝，25fps 下单核就吃掉 40%+。
    """

    def __init__(self, hwnd: int, lru: int = 64):
        self.hwnd = int(hwnd)
        ex = _GetWindowLongPtr(self.hwnd, GWL_EXSTYLE)
        _SetWindowLongPtr(self.hwnd, GWL_EXSTYLE,
                          ex | WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
        self._cache = {}
        self._lru_max = lru
        self._dib = None            # (w, h, hdc_mem, hbmp, ppv)
        self._hdc_screen = None
        self._bf = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        self._src_pt = wintypes.POINT(0, 0)
        self._last_key = object()

    def set_click_through(self, on: bool):
        ex = _GetWindowLongPtr(self.hwnd, GWL_EXSTYLE)
        ex = (ex | WS_EX_TRANSPARENT) if on else (ex & ~WS_EX_TRANSPARENT)
        _SetWindowLongPtr(self.hwnd, GWL_EXSTYLE, ex)

    def _premult_bgra(self, img, key):
        """RGBA -> 预乘 alpha 的 BGRA 字节（UpdateLayeredWindow 要求）"""
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        r, g, b, a = img.split()
        data = Image.merge("RGBA", (ImageChops.multiply(b, a),
                                    ImageChops.multiply(g, a),
                                    ImageChops.multiply(r, a), a)).tobytes()
        if len(self._cache) >= self._lru_max:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = data
        return data

    def _ensure_dib(self, w, h):
        d = self._dib
        if d is not None and d[0] == w and d[1] == h:
            return d
        self._free_dib()
        if self._hdc_screen is None:
            self._hdc_screen = user32.GetDC(0)
        if not self._hdc_screen:
            return None
        hdc_mem = gdi32.CreateCompatibleDC(self._hdc_screen)
        if not hdc_mem:
            return None
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h            # 负数 = 自上而下
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB
        ppv = ctypes.c_void_p()
        hbmp = gdi32.CreateDIBSection(self._hdc_screen, ctypes.byref(bmi),
                                      DIB_RGB_COLORS, ctypes.byref(ppv), None, 0)
        if not hbmp or not ppv.value:
            gdi32.DeleteDC(hdc_mem)
            return None
        gdi32.SelectObject(hdc_mem, hbmp)
        self._dib = (w, h, hdc_mem, hbmp, ppv)
        return self._dib

    def _free_dib(self):
        d = self._dib
        if not d:
            return
        try:
            gdi32.DeleteObject(d[3])
            gdi32.DeleteDC(d[2])
        except Exception:
            pass
        self._dib = None

    def paint(self, img, x: int, y: int, key=None, force=False) -> bool:
        """img: RGBA PIL.Image；x,y 为屏幕坐标（左上角）。key 用于缓存预乘结果。"""
        w, h = img.size
        sig = (key, x, y) if key is not None else None
        if not force and sig is not None and sig == self._last_key:
            return True                     # 画面与位置都没变，不必重画
        data = self._premult_bgra(img, key) if key is not None else None
        if data is None:
            if img.mode != "RGBA":
                img = img.convert("RGBA")
            r, g, b, a = img.split()
            data = Image.merge("RGBA", (ImageChops.multiply(b, a),
                                        ImageChops.multiply(g, a),
                                        ImageChops.multiply(r, a), a)).tobytes()

        dib = self._ensure_dib(w, h)
        if dib is None:
            return False
        ctypes.memmove(dib[4], data, w * h * 4)      # 单次拷贝
        size = wintypes.SIZE(w, h)
        dst_pt = wintypes.POINT(int(x), int(y))
        ok = user32.UpdateLayeredWindow(
            self.hwnd, self._hdc_screen, ctypes.byref(dst_pt), ctypes.byref(size),
            dib[2], ctypes.byref(self._src_pt), 0, ctypes.byref(self._bf), ULW_ALPHA)
        if ok and sig is not None:
            self._last_key = sig
        return bool(ok)


def root_hwnd(hwnd: int) -> int:
    """取得真正的顶层窗口句柄（tkinter 的 winfo_id 给的是内部子窗口）"""
    try:
        r = user32.GetAncestor(int(hwnd), 2)      # GA_ROOT
        return int(r) if r else int(hwnd)
    except Exception:
        return int(hwnd)


# ================================================================ 窗口枚举
class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


_EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_GWL_EXSTYLE = -20
_WS_EX_TOOLWINDOW = 0x00000080
_DWMWA_CLOAKED = 14


def window_rects(exclude=(), min_w=260, min_h=200):
    """枚举屏幕上「值得碰一下」的顶层窗口矩形。

    过滤掉：自己（exclude）、不可见、最小化、无标题、工具窗口、
    以及被 DWM 遮蔽（UWP 虚拟化）的窗口。
    结果按面积从大到小排序，物理引擎用它们做反弹。
    """
    out = []
    ex = set(int(h) for h in exclude if h)
    try:
        own_pid = ctypes.windll.kernel32.GetCurrentProcessId()
    except Exception:
        own_pid = 0

    def cb(hwnd, lparam):
        try:
            if int(hwnd) in ex:
                return True
            if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                return True
            if user32.GetWindowTextLengthW(hwnd) == 0:
                return True
            if _GetWindowLongPtr(hwnd, _GWL_EXSTYLE) & _WS_EX_TOOLWINDOW:
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if own_pid and pid.value == own_pid:
                return True                       # 自己的窗口全部跳过
            try:
                cloaked = ctypes.c_int(0)
                if ctypes.windll.dwmapi.DwmGetWindowAttribute(
                        hwnd, _DWMWA_CLOAKED, ctypes.byref(cloaked), 4) == 0 and cloaked.value:
                    return True
            except Exception:
                pass
            rc = _RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(rc)):
                return True
            w, h = rc.right - rc.left, rc.bottom - rc.top
            if w < min_w or h < min_h:
                return True
            out.append((float(rc.left), float(rc.top), float(rc.right), float(rc.bottom)))
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(_EnumProc(cb), 0)
    except Exception:
        return []
    out.sort(key=lambda r: -(r[2] - r[0]) * (r[3] - r[1]))
    return out[:24]


def move_window(hwnd: int, x: int, y: int, w: int, h: int):
    try:
        # 插到 HWND_TOPMOST(-1)：每次移动都重申置顶，
        # 否则 SetWindowPos 用 HWND_TOP(0) 会把它降到普通窗口层，
        # 被同样置顶的 WorkBuddy 主窗口压住。
        user32.SetWindowPos(int(hwnd), -1, int(x), int(y), int(w), int(h),
                            0x0010 | 0x0040)
    except Exception:
        pass


def reassert_topmost(hwnd: int):
    """不动位置，只把窗口重新提到 TOPMOST 层。

    WorkBuddy 主窗口本身也是置顶的，两边互相抢层时后调用者在上；
    桌宠静止时不再调用 move_window，就会被压住，所以需要定期重申一次。
    """
    try:
        user32.SetWindowPos(int(hwnd), -1, 0, 0, 0, 0,
                            0x0001 | 0x0002 | 0x0010)
    except Exception:
        pass


def dpi_scale() -> float:
    """主屏 DPI 缩放（1.0 = 100%）"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass
    try:
        hdc = user32.GetDC(0)
        dpi = gdi32.GetDeviceCaps(hdc, 88)        # LOGPIXELSX
        user32.ReleaseDC(0, hdc)
        return max(1.0, dpi / 96.0)
    except Exception:
        return 1.0


def screen_size():
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass
    return (user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))


def work_area():
    """返回主屏工作区 (left, top, right, bottom)，排除任务栏"""
    import ctypes.wintypes as wt

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", RECT),
                    ("rcWork", RECT), ("dwFlags", ctypes.c_ulong)]

    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass
    hmon = user32.MonitorFromPoint(wt.POINT(0, 0), 1)   # MONITOR_DEFAULTTOPRIMARY
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
        return (mi.rcWork.left, mi.rcWork.top, mi.rcWork.right, mi.rcWork.bottom)
    w, h = screen_size()
    return (0, 0, w, h)


if __name__ == "__main__":
    import PIL.Image as I
    print("layered available:", available())
    print("screen:", screen_size())
    print("work area:", work_area())
    img = I.new("RGBA", (40, 40), (255, 0, 0, 128))
    print("paint smoke test needs a real hwnd; skipped")
