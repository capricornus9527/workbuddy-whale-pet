# -*- coding: utf-8 -*-
"""sysmon.py —— 纯 ctypes 的 CPU / 内存监控（不装 psutil）

CPU 用 GetSystemTimes 的两次采样差值算；
内存用 GlobalMemoryStatusEx。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def _ft(v: FILETIME) -> int:
    return (v.dwHighDateTime << 32) | v.dwLowDateTime


class SysMon:
    def __init__(self):
        self._prev = None

    def cpu_percent(self) -> float:
        idle, kern, user = FILETIME(), FILETIME(), FILETIME()
        if not kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern),
                                       ctypes.byref(user)):
            return -1.0
        cur = (_ft(idle), _ft(kern) + _ft(user))
        if self._prev is None:
            self._prev = cur
            return -1.0
        di = cur[0] - self._prev[0]
        dt = cur[1] - self._prev[1]
        self._prev = cur
        if dt <= 0:
            return -1.0
        return max(0.0, min(100.0, (1.0 - di / dt) * 100.0))

    def mem_percent(self) -> float:
        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if not kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return -1.0
        return float(st.dwMemoryLoad)

    def mem_used_gb(self) -> float:
        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if not kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return -1.0
        return (st.ullTotalPhys - st.ullAvailPhys) / (1024 ** 3)


if __name__ == "__main__":
    import time
    m = SysMon()
    m.cpu_percent()
    time.sleep(0.5)
    print("CPU %.1f%%  内存 %.0f%% (%.1f GB 已用)"
          % (m.cpu_percent(), m.mem_percent(), m.mem_used_gb()))
