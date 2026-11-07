# -*- coding: utf-8 -*-
"""pet.py —— DeepSeek 鲸鱼娘桌宠 · WorkBuddy 版

一只常驻 Windows 桌面右下角的 DeepSeek 鲸鱼娘（社区昵称「大肥鱼」），
读取本机 WorkBuddy 的会话记录与用量数据库，实时显示今日积分、本轮消耗、
上下文余量与输出速率。

改造自 XT-nya/deepseek-dafeiyu-pet（MIT），主要差异：

    * 数据源由 DeepSeek Harness 的 zstd 日志改为 WorkBuddy 的
      ``workbuddy.db`` + ``projects/*/*.jsonl``（见 wb_usage.py）
    * 新增「逐帧动画」形态：57 帧 / 9 组动作，来自 8x11 精灵图集
    * 口径由「峰谷计价 + 折算金额」改为「积分 + 上下文占用」

针对「形态乱跳 / 切换生硬 / 呼吸晃眼」的四个解法：

    1. 形态统一  —— 贴图按【头宽】归一缩放，而不是按外框等比。
       外框等比会让全身图(比例0.56)和胸像图(比例1.00)在屏幕上差近一倍，
       看起来就是「小人↔大人」乱切。改按头宽对齐后，角色头部尺寸恒定。
       同时默认只启用同一套构图（portrait 胸像组），进一步杜绝跳变。
    2. 切换缓慢  —— 语义状态 + 最小停留 6s；空闲差分 28~60 秒才换一次。
    3. 淡入顺滑  —— 620ms 交叉淡入，smoothstep 曲线，约 15 帧过渡。
    4. 呼吸柔和  —— 幅度 ±0.4%、上下 1.6 逻辑像素、周期 3.6s、25fps。

另参考 pet-app 补了两条「活起来」的功能：沿底边散步、拖拽后抛物线回落。

交互：拖拽移动 · 单击戳一下 · 双击详情卡 · 悬停偷看 · 右键菜单
用法： python src\\pet.py
"""

from __future__ import annotations

import json
import math
import os
import random
import sys
import threading
import time
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PIL import Image, ImageOps, ImageTk          # noqa: E402

import win_layered as WL                          # noqa: E402
import pricing                                    # noqa: E402
import wb_usage                                   # noqa: E402
import wb_quota                                   # noqa: E402
import wb_balance                                 # noqa: E402
import sysmon                                     # noqa: E402
import barlib                                     # noqa: E402
import physics                                    # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPRITE_DIR = os.path.join(ROOT, "assets", "pet")
ANIM_DIR = os.path.join(ROOT, "assets", "anim")
CONFIG = os.path.join(ROOT, "pet.config.json")

ACCENT_PEAK = (255, 122, 122)
ACCENT_VALLEY = (96, 220, 176)
ACCENT_BUSY = (96, 158, 245)

# ---------------------------------------------------------------- 版式（逻辑像素）
# BOX 是「贴图适配目标」；窗口在它四周留 PAD，保证旋转/呼吸放大永远不会裁切贴图
BOX_W, BOX_H = 180, 186
PAD_X, PAD_TOP, PAD_BOT = 16, 14, 6
GAP = 4
SLEEP_AFTER = 240.0

# ---------------------------------------------------------------- 节奏（全部放慢）
FPS = 60                      # 144Hz 屏也能跑满；实测单核 ~2%
# 差分切换过渡：瞬间 / 140ms / 320ms（交叉淡入会让两张图短暂重叠，默认不用）
TRANSITIONS = {"instant": 0.0, "quick": 0.14, "smooth": 0.32}
MIN_DWELL = 6.0               # 任何差分最短停留
IDLE_DWELL = (28.0, 60.0)     # 空闲差分多久换一次
SPECIAL_DWELL = 10.0          # 特殊状态停留
CHAIN_P = 0.02                # 「吃白饭」小剧场概率
if os.environ.get("PET_TEST_FAST"):        # 自动化测试用：把节奏压缩，方便抓切换过程
    IDLE_DWELL = (2.2, 3.6)
    SPECIAL_DWELL = 2.0

# ---------------------------------------------------------------- 呼吸
BREATH_PERIOD = 3.6           # 一个呼吸周期的秒数
BREATH_SCALE = 0.0045         # ±0.45%
BREATH_LIFT = 1.6             # ±1.6 逻辑像素
BREATH_STEPS = 32             # 每周期采样数（决定平滑度，不是帧率！）

DEFAULTS = {
    "sprite_scale": 1.0,
    "form": "portrait",            # portrait(胸像·精细) | full(全身立绘) | anim(逐帧动画)
    "activity": "stroll",          # still | stroll | roam | gravity
    "phys": {"throw": True, "bounce": True, "collide": True,
             "swing": True, "flip": True},
    "bar_mode": "always",          # always | auto(悬停) | hidden
    "bar_rows": [["period"], ["speed", "cache", "cost"]],
    "module_on": {"period": True, "speed": True, "throughput": False,
                  "cache": True, "cost": True, "cost_rate": False,
                  "session": False, "sys": False, "calls": False, "clock": False},
    "quiet": False,
    "bubble_on": True,             # 头顶思考泡
    "quota_left": None,            # 上次同步的真实积分；离线时保留
    "quota_expire_left": None,     # 最近到期那一组的剩余积分
    "quota_expire": "",            # 最近到期时间
    "quota_clipboard": True,       # 自动从剪贴板识别额度
    "breathing": True,
    "transition": "instant",     # instant | quick | smooth
    "hover_expr": False,         # 鼠标悬停时换表情（有人喜欢有人烦，默认关）
    "card_open": False,
    "model": pricing.DEFAULT_MODEL,
    "autostart": False,
}

# ---------------------------------------------------------------- 语义 -> 差分池
SEMANTIC = {
    "portrait": {
        "idle":  ["dazed", "base", "coquettish"],
        "busy":  ["working"],
        "happy": ["cookie", "eating"],
        "want":  ["craving"],
        "peak":  ["angry_scene"],
        "shy":   ["coquettish"],
    },
    "full": {
        "idle":  ["curious", "encouraging", "speechless"],
        "busy":  ["curious"],
        "happy": ["excited"],
        "want":  ["curious"],
        "peak":  ["sad", "surprised"],
        "shy":   ["shy"],
        "angry": ["angry"],
    },
    # 逐帧动画形态：每帧来自 assets/anim，播放时长按图集元数据
    "anim": {
        "idle":  ["a:idle"],
        "busy":  ["a:busy"],
        "happy": ["a:jump"],
        "want":  ["a:wait"],
        "peak":  ["a:sad"],
        "shy":   ["a:greet"],
    },
}
# 两种形态都能用的通用差分（躺平睡觉 / 被拎起来）——头宽已归一，不会跳大小
UNIVERSAL = {"sleep": "sleeping", "drag": "dragged"}

CHAIN_RICE = [("want", 3.4), ("happy", 3.8), ("happy", 3.8)]   # 嘴馋 → 吃 → 吃


def load_cfg():
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    try:
        with open(CONFIG, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    cfg["module_on"] = {**DEFAULTS["module_on"], **(cfg.get("module_on") or {})}
    cfg["phys"] = {**DEFAULTS["phys"], **(cfg.get("phys") or {})}
    return cfg


def smoothstep(t):
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


class PetApp:
    # ============================================================ 初始化
    def __init__(self):
        self.cfg = load_cfg()
        self.scale = WL.dpi_scale()

        with open(os.path.join(SPRITE_DIR, "index.json"), encoding="utf-8") as f:
            self.index = json.load(f)["sprites"]
        try:
            with open(os.path.join(ANIM_DIR, "index.json"), encoding="utf-8") as f:
                self.anim = json.load(f).get("states", {})
        except Exception:
            self.anim = {}
        self._anim_frame = 0
        self._anim_ms = 0.0
        self._anim_state = ""
        # 先显示上次成功快照，再通过宿主 WBIPC 同步真实余额与积分包
        self.quota_left = self.cfg.get("quota_left")
        self.quota_expire_left = self.cfg.get("quota_expire_left")
        self.quota_expire = self.cfg.get("quota_expire", "")
        self.quota_groups = self.cfg.get("quota_groups") or []
        self._dot_phase = 0.0
        self._dot_bucket = 0
        self._clip_last = None
        self._balance_result = None
        self._balance_seen = None
        self._balance_wake = threading.Event()
        self.quota_status = "连接中"
        self.quota_synced_at = self.cfg.get("quota_synced_at", 0)
        self.quota_error = ""

        self.tracker = wb_usage.UsageTracker()
        self.stats = wb_usage.Stats()
        self.sysmon = sysmon.SysMon()
        self.sysmon.cpu_percent()
        self.cpu, self.mem = -1.0, -1.0

        self.bar = barlib.BarRenderer(self.scale)
        self.card = barlib.CardRenderer(self.scale)

        self.sprite = "base"
        self._sem_key = ""
        self._sprite_until = 0.0
        self._forced = None
        self._chain = []
        self.dragging = False
        self.hover = False
        self.bucket = 0
        self._fade = 0
        self._prev = None
        self.cache = {}
        self._bar_gen = 0
        self._bar_tick = 0
        self.x = self.y = 0
        self.base_y = 0
        # 自动化验证用：把每一帧实际渲染结果导出，供 live_check 分析
        self.dump_dir = os.environ.get("PET_DUMP_DIR")
        self._dump_i = 0
        self.trace = bool(os.environ.get("PET_TRACE"))
        self._trace_tick = 0
        self._prof_t = [0.0, 0]

        self._build_window()

        # ---- 物理引擎（参考 pet-app 的 float.js）
        self.phys = physics.PetPhysics(
            WL.work_area, lambda: (self.win_w, self.win_h), self.scale)
        self.phys.opts = dict(self.cfg.get("phys", DEFAULTS["phys"]))
        self.phys.exclude = (self.hwnd,)
        self.phys._rect_fn = WL.window_rects
        self.phys.place(self.x, self.y)
        self.phys.set_mode(self.cfg.get("activity", "stroll"))
        self.vx = self.vy = 0.0                     # 拖拽速度（像素/秒）

        self._start_poller()
        self._start_balance_poller()
        self._tick()
        # 写 pid 文件，供 tools/follow.ps1 判断桌面宠物是否在跑
        try:
            with open(os.path.join(ROOT, "pet.pid"), "w") as f:
                f.write(str(os.getpid()))
        except Exception:
            pass

    # ============================================================ 形态 / 缩放
    def _form_sprites(self):
        form = self.cfg.get("form", "portrait")
        names = [n for n, i in self.index.items() if i.get("form") == form]
        if not names:
            names = list(self.index)
        return names

    def _compute_head_target(self):
        """先按外框等比试算一遍，取「头宽」的中位数作为统一基准"""
        vals = []
        for n in self._form_sprites():
            info = self.index[n]
            w, h = info["size"]
            k = min(self.box_w / w, self.box_h / h)
            vals.append(max(1.0, info.get("head_w", w * 0.7) * k))
        vals.sort()
        self.head_target = vals[len(vals) // 2] if vals else self.box_w * 0.66

    def _layout(self):
        s = self.scale * float(self.cfg.get("sprite_scale", 1.0))
        self.S = s
        # 贴图适配区（BOX）+ 四周留白（PAD）= 窗口
        # 留白的作用：旋转(单摆)、呼吸放大都不会把贴图顶出画布被裁掉
        self.box_w = int(round(BOX_W * s))
        self.box_h = int(round(BOX_H * s))
        self.pad_x = max(2, int(round(PAD_X * s)))
        self.pad_top = max(2, int(round(PAD_TOP * s)))
        self.pad_bot = max(2, int(round(PAD_BOT * s)))
        self.area_w = self.box_w + self.pad_x * 2
        self.bubble = barlib.BubbleRenderer(s)
        # 气泡区吃掉原本的顶部留白，让圆点贴着角色头顶，不留空隙
        self.bubble_h = (max(0, self.bubble.height() - self.pad_top)
                         if self.cfg.get("bubble_on", True) else 0)
        self.area_h = self.pad_top + self.bubble_h + self.box_h + self.pad_bot
        self.area_bottom = self.area_h           # 贴图底边在窗口内的 y
        self.win_w = self.area_w
        self._compute_head_target()
        self.bar_img = None
        if self._bar_visible():
            self.bar_img = self.bar.render(self._ctx(), self._active_rows(),
                                           self.win_w - int(6 * s), self._accent())
        self.bar_h = self.bar_img.height if self.bar_img else 0
        self.win_h = self.area_h + (int(round(GAP * s)) + self.bar_h if self.bar_h else 0)
        self._bar_gen += 1
        self.cache.clear()

    # ============================================================ 窗口
    def _build_window(self):
        # Windows 默认计时器分辨率 15.6ms，after(16) 实际只能跑 ~35fps。
        # 把分辨率提到 1ms，60fps 才能跑满。
        try:
            import ctypes as _ct
            _ct.windll.winmm.timeBeginPeriod(1)
            self._hi_res_timer = True
        except Exception:
            self._hi_res_timer = False
        self.root = tk.Tk()
        self.root.title("DeepSeek 大肥鱼桌宠")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.config(bg="#0b1020")

        wl, wt, wr, wb = WL.work_area()
        self._layout()
        x, y = self.cfg.get("x"), self.cfg.get("y")
        if not isinstance(x, int) or not (wl - 100 <= x <= wr + 100):
            x = wr - self.win_w - int(24 * self.scale)
        if not isinstance(y, int) or not (wt - 100 <= y <= wb + 100):
            y = wb - self.win_h - int(16 * self.scale)
        self.x, self.y = int(x), int(y)
        self.base_y = self.y

        self.root.geometry("%dx%d+%d+%d" % (self.win_w, self.win_h, self.x, self.y))
        self.cv = tk.Canvas(self.root, width=self.win_w, height=self.win_h,
                            highlightthickness=0, bd=0, bg="#0b1020")
        self.cv.pack()
        self.root.update_idletasks()
        self.root.update()

        self.hwnd = WL.root_hwnd(self.root.winfo_id())
        self.surface = WL.LayeredSurface(self.hwnd, lru=40)
        WL.move_window(self.hwnd, self.x, self.y, self.win_w, self.win_h)

        self.cv.bind("<ButtonPress-1>", self._press)
        self.cv.bind("<B1-Motion>", self._drag)
        self.cv.bind("<ButtonRelease-1>", self._release)
        self.cv.bind("<Double-Button-1>", lambda e: self._toggle_card())
        self.cv.bind("<Button-3>", self._menu)
        self.cv.bind("<Enter>", self._enter)
        self.cv.bind("<Leave>", self._leave)
        self._build_menu()

    def _relayout(self):
        bottom = self.y + self.win_h
        self._layout()
        self.y = bottom - self.win_h
        self.base_y = self.y
        self.root.geometry("%dx%d+%d+%d" % (self.win_w, self.win_h, self.x, self.y))
        self.cv.config(width=self.win_w, height=self.win_h)
        if getattr(self, "phys", None) is not None:
            self.phys.place(self.x, self.y)
            self.phys.set_mode(self.cfg.get("activity", "stroll"))
            if self.cfg.get("activity") == "gravity":
                self.phys._snap_ground()
            self.x, self.y = int(self.phys.x), int(self.phys.y)
        WL.move_window(self.hwnd, self.x, self.y, self.win_w, self.win_h)
        self._redraw_card()
        self._save()

    # ============================================================ 菜单
    def _build_menu(self):
        m = tk.Menu(self.root, tearoff=0)
        self.menu = m
        m.add_command(label="详情卡（双击也行）", command=self._toggle_card)
        m.add_separator()

        self.form_var = tk.StringVar(value=self.cfg.get("form", "portrait"))
        sub = tk.Menu(m, tearoff=0)
        for v, label in (("portrait", "胸像 · 静态差分（精细）"),
                         ("full", "全身立绘 · 静态差分"),
                         ("anim", "逐帧动画 · 57 帧（推荐）")):
            sub.add_radiobutton(label=label, value=v, variable=self.form_var,
                                command=lambda k=v: self._set_form(k))
        m.add_cascade(label="形态", menu=sub)

        self.act_var = tk.StringVar(value=self.cfg.get("activity", "stroll"))
        sub = tk.Menu(m, tearoff=0)
        for v, label in (("still", "乖乖待着"), ("stroll", "沿屏幕底边散步"),
                         ("roam", "自由游荡（二维随机走）"),
                         ("gravity", "重力模式（拖起来能扔出去）")):
            sub.add_radiobutton(label=label, value=v, variable=self.act_var,
                                command=lambda k=v: self._set_activity(k))
        sub.add_separator()
        self.phys_vars = {}
        for key, label in (("throw", "拖拽抛出（抛物线）"),
                           ("bounce", "落地弹跳"),
                           ("collide", "撞到别的窗口反弹"),
                           ("swing", "拖拽时摇晃（单摆）"),
                           ("flip", "走动时朝左转身")):
            var = tk.BooleanVar(value=bool(self.cfg["phys"].get(key, True)))
            self.phys_vars[key] = var
            sub.add_checkbutton(label=label, variable=var,
                                command=lambda k=key: self._toggle_phys(k))
        m.add_cascade(label="物理效果", menu=sub)

        self.bar_var = tk.StringVar(value=self.cfg.get("bar_mode", "always"))
        sub = tk.Menu(m, tearoff=0)
        for mode, label in (("always", "一直显示"), ("auto", "悬停时显示"),
                            ("hidden", "完全隐藏")):
            sub.add_radiobutton(label=label, value=mode, variable=self.bar_var,
                                command=lambda v=mode: self._set_bar_mode(v))
        m.add_cascade(label="信息条", menu=sub)

        self.bubble_var = tk.BooleanVar(value=bool(self.cfg.get("bubble_on", True)))
        m.add_checkbutton(label="头顶思考泡", variable=self.bubble_var,
                          command=self._toggle_bubble)
        m.add_command(label="设置积分额度…", command=self._edit_quota)
        m.add_command(label="立即刷新积分", command=self._balance_wake.set)
        m.add_command(label="从剪贴板同步额度", command=self._sync_from_clipboard)

        self.mod_menu = tk.Menu(m, tearoff=0)
        self.mod_vars = {}
        for key, mod in barlib.MODULES.items():
            var = tk.BooleanVar(value=bool(self.cfg["module_on"].get(key, False)))
            self.mod_vars[key] = var
            self.mod_menu.add_checkbutton(label=mod.label, variable=var,
                                          command=lambda k=key: self._toggle_module(k))
        m.add_cascade(label="信息条模块", menu=self.mod_menu)

        self.trans_var = tk.StringVar(value=self.cfg.get("transition", "instant"))
        sub = tk.Menu(m, tearoff=0)
        for v, label in (("instant", "瞬间切换（推荐）"), ("quick", "快速淡入 0.14s"),
                         ("smooth", "柔和淡入 0.32s")):
            sub.add_radiobutton(label=label, value=v, variable=self.trans_var,
                                command=lambda k=v: self._set_transition(k))
        m.add_cascade(label="切换过渡", menu=sub)

        self.breath_var = tk.BooleanVar(value=bool(self.cfg.get("breathing", True)))
        m.add_checkbutton(label="呼吸感（很轻微）", variable=self.breath_var,
                          command=self._toggle_breath)
        self.hover_var = tk.BooleanVar(value=bool(self.cfg.get("hover_expr", False)))
        m.add_checkbutton(label="悬停时换表情", variable=self.hover_var,
                          command=self._toggle_hover_expr)
        m.add_separator()

        ssub = tk.Menu(m, tearoff=0)
        for v in (0.8, 1.0, 1.25, 1.5):
            ssub.add_command(label="%d%%" % int(v * 100),
                             command=lambda k=v: self._set_scale(k))
        m.add_cascade(label="大小", menu=ssub)

        self.quiet_var = tk.BooleanVar(value=bool(self.cfg.get("quiet", False)))
        m.add_checkbutton(label="安静模式", variable=self.quiet_var,
                          command=self._toggle_quiet)
        self.auto_var = tk.BooleanVar(value=bool(self.cfg.get("autostart", False)))
        m.add_checkbutton(label="开机自启", variable=self.auto_var,
                          command=self._toggle_autostart)
        m.add_separator()
        m.add_command(label="回到右下角", command=self._reset_pos)
        m.add_command(label="退出", command=self._quit)

    def _menu(self, e):
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()

    def _set_form(self, v):
        self.cfg["form"] = v
        self._sem_key = ""
        self._anim_state = ""
        self._anim_frame = 0
        self._anim_ms = 0.0
        self._relayout()

    def _set_activity(self, v):
        self.cfg["activity"] = v
        self.act_var.set(v)
        self.phys.place(self.x, self.y)
        self.phys.set_mode(v)
        if v == "gravity":
            self.phys._snap_ground()
        self.x, self.y = int(self.phys.x), int(self.phys.y)
        WL.move_window(self.hwnd, self.x, self.y, self.win_w, self.win_h)
        self._save()

    def _toggle_phys(self, key):
        on = bool(self.phys_vars[key].get())
        self.cfg["phys"][key] = on
        self.phys.opts[key] = on
        if key == "flip" and not on:
            self.phys.flip = False
        if key == "swing" and not on:
            self.phys.swing = self.phys.swing_v = 0.0
        self.cache.clear()
        self._save()

    def _set_bar_mode(self, v):
        self.cfg["bar_mode"] = v
        self._relayout()

    def _toggle_module(self, key):
        on = self.mod_vars[key].get()
        self.cfg["module_on"][key] = on
        rows = [[k for k in row] for row in self.cfg.get("bar_rows", [])] or [["period"]]
        keys = [k for row in rows for k in row]
        if on and key not in keys:
            rows[-1].append(key)
        if not on:
            rows = [[k for k in row if k != key] for row in rows]
        rows = [r for r in rows if r] or [["period"]]
        self.cfg["bar_rows"] = rows
        self._relayout()

    def _toggle_breath(self):
        self.cfg["breathing"] = bool(self.breath_var.get())
        self.cache.clear()
        self._save()

    # ------------------------------------------------------------ 思考泡 / 额度
    def _toggle_bubble(self):
        self.cfg["bubble_on"] = bool(self.bubble_var.get())
        self.cache.clear()
        self._relayout()
        self._save()

    def _edit_quota(self):
        FONT = ("Microsoft YaHei UI", 10)
        win = tk.Toplevel(self.root)
        win.title("设置积分额度")
        win.attributes("-topmost", True)
        win.resizable(False, False)
        win.configure(bg="#0e1424")
        frm = tk.Frame(win, bg="#0e1424")
        frm.pack(padx=16, pady=14)

        def lab(t, r):
            tk.Label(frm, text=t, fg="#cbd6ea", bg="#0e1424",
                     font=FONT).grid(row=r, column=0, sticky="w", pady=5)

        lab("剩余总积分", 0)
        e1 = tk.Entry(frm, width=18, bg="#1a2338", fg="#ffffff", relief="flat",
                      insertbackground="#ffffff", font=FONT)
        e1.grid(row=0, column=1, padx=(12, 0), pady=5, ipady=3)
        if self.quota_left is not None:
            e1.insert(0, ("%g" % float(self.quota_left)))

        lab("最近到期剩余", 1)
        e3 = tk.Entry(frm, width=18, bg="#1a2338", fg="#ffffff", relief="flat",
                      insertbackground="#ffffff", font=FONT)
        e3.grid(row=1, column=1, padx=(12, 0), pady=5, ipady=3)
        if self.quota_expire_left is not None:
            e3.insert(0, ("%g" % float(self.quota_expire_left)))

        lab("到期时间", 2)
        e2 = tk.Entry(frm, width=18, bg="#1a2338", fg="#ffffff", relief="flat",
                      insertbackground="#ffffff", font=FONT)
        e2.grid(row=2, column=1, padx=(12, 0), pady=5, ipady=3)
        e2.insert(0, self.quota_expire or "")

        tk.Label(frm, text="离线回退；自动同步恢复后以接口余额为准",
                 fg="#6c7a99", bg="#0e1424", font=("Microsoft YaHei UI", 8)
                 ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(2, 0))

        def _num(s):
            s = s.strip().replace(",", "").replace("，", "")
            if not s:
                return None
            try:
                return float(s)
            except ValueError:
                return None

        def ok(_e=None):
            self.quota_status = "手动额度"
            self.quota_error = ""
            self.quota_left = _num(e1.get())
            self.quota_expire_left = _num(e3.get())
            self.quota_expire = e2.get().strip()
            self.cfg["quota_left"] = self.quota_left
            self.cfg["quota_expire_left"] = self.quota_expire_left
            self.cfg["quota_expire"] = self.quota_expire
            self.cache.clear()
            self._save()
            win.destroy()

        btns = tk.Frame(frm, bg="#0e1424")
        btns.grid(row=4, column=0, columnspan=2, sticky="e", pady=(12, 0))
        tk.Button(btns, text="取消", command=win.destroy, relief="flat",
                  bg="#232c42", fg="#cbd6ea", width=7, font=FONT).pack(side="right", padx=(8, 0))
        tk.Button(btns, text="保存", command=ok, relief="flat",
                  bg="#2b6cf6", fg="#ffffff", width=7, font=FONT).pack(side="right")
        win.bind("<Return>", ok)
        win.bind("<Escape>", lambda _e: win.destroy())
        win.update_idletasks()
        wx = max(10, min(self.x - 30, win.winfo_screenwidth() - win.winfo_width() - 20))
        wy = max(10, min(self.y - 140, win.winfo_screenheight() - win.winfo_height() - 60))
        win.geometry("+%d+%d" % (wx, wy))
        e1.focus_set()

    # ------------------------------------------------------------ 额度自动同步
    def _apply_quota(self, total=None, exp_left=None, expire=None, groups=None):
        changed = False
        if total is not None and total != self.quota_left:
            self.quota_left = total
            changed = True
        if exp_left is not None and exp_left != self.quota_expire_left:
            self.quota_expire_left = exp_left
            changed = True
        if expire is not None and expire != self.quota_expire:
            self.quota_expire = expire
            changed = True
        if groups is not None and groups != self.quota_groups:
            self.quota_groups = groups
            changed = True
        if not changed:
            return False
        self.cfg["quota_left"] = self.quota_left
        self.cfg["quota_expire_left"] = self.quota_expire_left
        self.cfg["quota_expire"] = self.quota_expire
        self.cfg["quota_groups"] = self.quota_groups
        self.cache.clear()
        self._save()
        self._sem_key = ""
        self._forced = (self._pick_from("happy"), time.time() + 1.2)
        # 分组数量变了就重建卡片，否则只重绘内容
        if getattr(self, "card_win", None) is not None:
            new_h = 96 + 28 * min(6, max(1, len(self.quota_groups)))
            if new_h != getattr(self, "_card_h_logical", 0):
                was_open = self.cfg.get("card_open")
                self.card_win.destroy()
                self.card_win = None
                if was_open:
                    self._ensure_card()
                    self.card_win.deiconify()
            else:
                self._redraw_card()
        return True

    def _poll_clipboard(self):
        """剪贴板里出现 WorkBuddy 积分页的文字时，自动抽数字更新气泡"""
        if not self.cfg.get("quota_clipboard", True):
            return
        try:
            txt = self.root.clipboard_get()
        except Exception:
            return
        if not txt or txt == self._clip_last or len(txt) > 20000:
            return
        self._clip_last = txt
        if self.quota_status == "已同步":
            return  # 旧剪贴板不能覆盖刚取得的真实余额；手动菜单仍可用
        got = wb_quota.parse(txt)
        if got:
            self.quota_status = "手动额度"
            self.quota_error = ""
            self._apply_quota(got.get("total"), got.get("expire_left"),
                              got.get("expire"), got.get("groups"))

    def _sync_from_clipboard(self):
        self._clip_last = None
        try:
            txt = self.root.clipboard_get()
        except Exception:
            return
        got = wb_quota.parse(txt)
        if got:
            self.quota_status = "手动额度"
            self.quota_error = ""
            self._apply_quota(got.get("total"), got.get("expire_left"),
                              got.get("expire"), got.get("groups"))

    def _set_transition(self, v):
        self.cfg["transition"] = v
        self.cache.clear()
        self._save()

    def _toggle_hover_expr(self):
        self.cfg["hover_expr"] = bool(self.hover_var.get())
        self._sem_key = ""
        self._save()

    def _toggle_quiet(self):
        self.cfg["quiet"] = bool(self.quiet_var.get())
        self._save()

    def _set_scale(self, v):
        self.cfg["sprite_scale"] = v
        self._relayout()

    def _reset_pos(self):
        wl, wt, wr, wb = WL.work_area()
        self.x = wr - self.win_w - int(24 * self.scale)
        self.y = wb - self.win_h - int(16 * self.scale)
        self.base_y = self.y
        WL.move_window(self.hwnd, self.x, self.y, self.win_w, self.win_h)
        self._save()

    def _toggle_autostart(self):
        want = bool(self.auto_var.get())
        ok = install_autostart(want)
        self.cfg["autostart"] = bool(ok and want)
        if not ok:
            self.auto_var.set(self.cfg["autostart"])
        self._forced = (self._pick_from("happy" if (want and ok) else "peak"),
                        time.time() + 1.6)
        self._save()

    # ============================================================ 详情卡
    def _toggle_card(self):
        self.cfg["card_open"] = not self.cfg.get("card_open", False)
        if self.cfg["card_open"]:
            self._ensure_card()
            self.card_win.deiconify()
        elif getattr(self, "card_win", None) is not None:
            self.card_win.withdraw()
        self._save()

    def _ensure_card(self):
        if getattr(self, "card_win", None) is not None:
            return
        pad = int(10 * self.scale)
        self.card_pad = pad
        # 高度随分组数量走，2 组时不留一大片空白
        n = len(self.quota_groups) or 1
        self._card_h_logical = 96 + 28 * min(6, n)
        cw, ch = int(262 * self.scale), int(self._card_h_logical * self.scale)
        self.card_w, self.card_h = cw, ch
        self.card_win = tk.Toplevel(self.root)
        self.card_win.overrideredirect(True)
        self.card_win.attributes("-topmost", True)
        self.card_win.config(bg="#000000")
        wl, wt, wr, wb = WL.work_area()
        cx, cy = self.cfg.get("card_x"), self.cfg.get("card_y")
        if not isinstance(cx, int) or not (wl - 60 <= cx <= wr + 60):
            cx = self.x - cw - pad
            if cx < wl + pad:
                cx = min(wr - cw - pad, self.x + self.win_w + pad)
        if not isinstance(cy, int) or not (wt - 60 <= cy <= wb + 60):
            cy = max(wt + pad, min(wb - ch - pad, self.y + self.win_h - ch))
        self.card_x, self.card_y = int(cx), int(cy)
        self.card_win.geometry("%dx%d+%d+%d" % (cw, ch, self.card_x, self.card_y))
        self.card_cv = tk.Canvas(self.card_win, width=cw, height=ch, bg="#000000",
                                 highlightthickness=0, bd=0)
        self.card_cv.pack()
        self.card_photo = None
        self.card_cv.bind("<ButtonPress-1>", self._card_press)
        self.card_cv.bind("<B1-Motion>", self._card_drag)
        self.card_cv.bind("<ButtonRelease-1>", self._card_release)
        self.card_cv.bind("<Button-3>", lambda e: self._toggle_card())
        try:
            import ctypes
            g = ctypes.WinDLL("gdi32")
            u = ctypes.WinDLL("user32")
            r = int(14 * self.scale)
            hr = g.CreateRoundRectRgn(0, 0, cw + 1, ch + 1, r, r)
            u.SetWindowRgn(WL.root_hwnd(self.card_win.winfo_id()), hr, True)
        except Exception:
            pass

    def _card_press(self, e):
        self._cd = (e.x_root - self.card_x, e.y_root - self.card_y)
        self._card_dragging = True

    def _card_drag(self, e):
        self.card_x = e.x_root - self._cd[0]
        self.card_y = e.y_root - self._cd[1]
        self.card_win.geometry("+%d+%d" % (self.card_x, self.card_y))

    def _card_release(self, e):
        # 松手后交还给跟随逻辑，卡片会自己贴回桌宠身边
        self._card_dragging = False

    def _follow_card(self):
        """让详情卡跟着桌宠走：优先贴右侧，放不下就贴左侧"""
        if getattr(self, "card_win", None) is None:
            return
        if not self.cfg.get("card_open") or getattr(self, "_card_dragging", False):
            return
        try:
            if not self.card_win.winfo_viewable():
                return
        except Exception:
            return
        wl, wt, wr, wb = WL.work_area()
        cw, ch = getattr(self, "card_w", 0), getattr(self, "card_h", 0)
        if not cw or not ch:
            return
        gap = int(8 * self.scale)
        x = self.x + self.win_w + gap
        if x + cw > wr - gap:
            x = self.x - cw - gap
        if x < wl + gap:
            x = max(wl + gap, min(wr - cw - gap, self.x + (self.win_w - cw) // 2))
        y = self.y + self.win_h - ch
        y = max(wt + gap, min(y, wb - ch - gap))
        if (int(x), int(y)) != (self.card_x, self.card_y):
            self.card_x, self.card_y = int(x), int(y)
            self.card_win.geometry("+%d+%d" % (self.card_x, self.card_y))

    def _redraw_card(self):
        if not self.cfg.get("card_open") or getattr(self, "card_win", None) is None:
            return
        ctx = self._ctx()
        ctx["accent"] = self._accent()
        ctx["quota_left"] = self.quota_left
        img = self.card.render_quota(ctx, self.quota_groups, 262,
                                     getattr(self, "_card_h_logical", 212))
        self.card_photo = ImageTk.PhotoImage(img)
        self.card_cv.delete("all")
        self.card_cv.create_image(0, 0, anchor="nw", image=self.card_photo)

    # ============================================================ 数据
    def _start_balance_poller(self):
        def run():
            client = wb_balance.BalanceClient()
            last_sig, next_due, last_try, failures = None, 0.0, -100.0, 0
            while not self._stop:
                now = time.monotonic()
                st = self.stats
                sig = (st.session_id, st.last_call_ts, st.today_credit, st.session_cost)
                manual = self._balance_wake.is_set()
                changed = sig != last_sig and failures == 0
                if now - last_try >= 5 and (manual or now >= next_due or changed):
                    self._balance_wake.clear()
                    last_try, last_sig = now, sig
                    try:
                        data = client.fetch()
                        self._balance_result = (data, "", time.time())
                        failures = 0
                        next_due = time.monotonic() + (15 if st.active else 60)
                    except Exception as e:
                        failures += 1
                        error = str(e) if isinstance(e, wb_balance.BalanceError) else "积分查询失败"
                        self._balance_result = (None, error, time.time())
                        next_due = time.monotonic() + min(120, 15 * 2 ** min(failures - 1, 3))
                time.sleep(0.5)
        threading.Thread(target=run, daemon=True).start()

    def _consume_balance_result(self):
        result = self._balance_result
        if result is None or result is self._balance_seen:
            return
        self._balance_seen = result
        data, error, _ = result
        self.quota_error = error
        if data is not None:
            self.quota_status = "已同步"
            self.quota_synced_at = data["updated_at"]
            self.cfg["quota_synced_at"] = self.quota_synced_at
            changed = self._apply_quota(data["total"], data["expire_left"],
                                        data["expire"], data["groups"])
            if not changed:
                self._save()
        else:
            self.quota_status = "未同步"
        self._bar_gen += 1
        self.cache.clear()
        self._redraw_card()

    def _start_poller(self):
        self._stop = False

        def run():
            while not self._stop:
                try:
                    self.stats = self.tracker.poll()
                except Exception:
                    pass
                try:
                    self.cpu = self.sysmon.cpu_percent()
                    self.mem = self.sysmon.mem_percent()
                except Exception:
                    pass
                time.sleep(2.0)

        threading.Thread(target=run, daemon=True).start()

    def _ctx(self):
        st = self.stats
        return {
            "stats": st, "peak": False,
            "period": (st.display_model or "WorkBuddy") if st else "WorkBuddy",
            "period_sub": ("今日 %.2f 积分 · %d 轮" % (st.today_credit, st.turns_today))
                          if st and st.has_data else "等待 WorkBuddy 数据",
            "switch_hours": None, "cpu": self.cpu, "mem": self.mem,
            "clock": time.strftime("%H:%M"),
            "quota_status": self.quota_status,
            "quota_error": self.quota_error,
        }

    def _accent(self):
        st = self.stats
        if st and st.has_data:
            if st.context_ratio >= 0.85:
                return ACCENT_PEAK
            if st.active:
                return ACCENT_BUSY
        return ACCENT_VALLEY

    def _active_rows(self):
        on = self.cfg.get("module_on", {})
        rows = [[k for k in row if on.get(k, False)]
                for row in self.cfg.get("bar_rows", [])]
        return [r for r in rows if r] or [["period"]]

    def _bar_visible(self):
        mode = self.cfg.get("bar_mode", "auto")
        if mode == "always":
            return True
        if mode == "hidden":
            return False
        return self.hover

    # ============================================================ 状态机
    def _semantic(self):
        st = self.stats
        # 只有打开「悬停时换表情」才用 hover 影响立绘；否则悬停只暂停移动，绝不换图
        if self.hover and self.cfg.get("hover_expr", False):
            return "shy"
        if st and st.has_data and st.idle_seconds > SLEEP_AFTER:
            return "sleep"
        if st and st.has_data and st.active:
            if st.context_ratio >= 0.9:
                return "peak"
            if st.burn_rate > 1_200_000:
                return "happy"
            return "busy"
        return "idle"

    def _pick_from(self, sem):
        form = self.cfg.get("form", "portrait")
        pool = [p for p in SEMANTIC.get(form, SEMANTIC["portrait"]).get(sem, [])
                if p in self.index or self._anim_of(p)]
        if not pool:
            uni = UNIVERSAL.get(sem)
            if uni and uni in self.index:
                return uni
            pool = [p for p in SEMANTIC.get(form, SEMANTIC["portrait"])["idle"]
                    if p in self.index or self._anim_of(p)] or ["base"]
        return random.choice(pool)

    def _switch_to(self, sem, now, dwell):
        self._sem_key = sem
        self.sprite = self._pick_from(sem)
        self._sprite_until = now + dwell
        return self.sprite

    def _advance_idle(self, now):
        """空闲：必须待够 IDLE_DWELL 才换下一次；小概率插入「吃白饭」小剧场"""
        if self._sem_key == "idle" and now < self._sprite_until:
            return self.sprite
        if self._sem_key != "idle":
            return self._switch_to("idle", now, random.uniform(*IDLE_DWELL))
        if random.random() < CHAIN_P:
            self._chain = list(CHAIN_RICE)
            self._chain[0] = (self._chain[0][0], now + self._chain[0][1])
            return self._switch_to(self._chain[0][0], now, self._chain[0][1])
        form = self.cfg.get("form", "portrait")
        pool = [p for p in SEMANTIC.get(form, SEMANTIC["portrait"])["idle"]
                if (p in self.index or self._anim_of(p)) and p != self.sprite] or [self.sprite]
        self.sprite = random.choice(pool)
        self._sprite_until = now + random.uniform(*IDLE_DWELL)
        return self.sprite

    def _pick_state(self):
        """返回当前应显示的差分名（对外接口，自检脚本依赖）"""
        now = time.time()
        if self.dragging:
            return self._switch_to("drag", now, 2.0)
        if self._forced and now < self._forced[1]:
            return self._forced[0]
        self._forced = None

        if self._chain:
            sem, until = self._chain[0]
            if now < until:
                return self.sprite
            self._chain.pop(0)
            if self._chain:
                self._chain[0] = (self._chain[0][0], now + self._chain[0][1])
                return self._switch_to(self._chain[0][0], now, self._chain[0][1])
            self._sem_key = ""
            return self._advance_idle(now)

        sem = self._semantic()
        if sem == self._sem_key:
            if sem == "idle":
                return self._advance_idle(now)
            return self.sprite                      # 语义没变 —— 绝不换图
        if sem == "idle":
            return self._advance_idle(now)
        return self._switch_to(sem, now, SPECIAL_DWELL)

    # ============================================================ 贴图
    def _anim_of(self, name):
        """'a:idle' -> 'idle'；不是动画名返回 None"""
        if isinstance(name, str) and name.startswith("a:"):
            key = name[2:]
            if key in getattr(self, "anim", {}):
                return key
        return None

    def _anim_img(self, key):
        """取动画状态下当前帧的贴图（按 BOX 等比适配，缓存带帧号）"""
        state = self.anim.get(key)
        if not state:
            return None
        frames = state.get("frames") or []
        if not frames:
            return None
        idx = max(0, min(self._anim_frame, len(frames) - 1))
        flip = bool(self.phys.opts.get("flip") and self.phys.flip)
        ck = ("anim", key, idx, self.box_w, self.box_h, flip)
        im = self.cache.get(ck)
        if im is not None:
            return im
        try:
            src = Image.open(os.path.join(ANIM_DIR, key, frames[idx])).convert("RGBA")
        except Exception:
            return None
        k = min(self.box_w / float(src.width), self.box_h / float(src.height))
        im = src.resize((max(1, int(round(src.width * k))),
                         max(1, int(round(src.height * k)))), Image.LANCZOS)
        if flip:
            im = ImageOps.mirror(im)
        if len(self.cache) > 160:
            self.cache.clear()
        self.cache[ck] = im
        return im

    def _sprite_img(self, name):
        anim_key = self._anim_of(name)
        if anim_key:
            return self._anim_img(anim_key)
        info = self.index.get(name) or self.index.get("base")
        flip = bool(self.phys.opts.get("flip") and self.phys.flip)
        key = ("sp", info["file"], self.box_w, self.box_h, flip)
        im = self.cache.get(key)
        if im is not None:
            return im
        try:
            src = Image.open(os.path.join(SPRITE_DIR, info["file"])).convert("RGBA")
        except Exception:
            return None
        # ① 外框等比，保证不溢出
        k = min(self.box_w / src.width, self.box_h / src.height)
        # ② 再按头宽归一：让每张贴图里角色头部在屏幕上一样大
        hw = max(1.0, info.get("head_w", src.width * 0.7)) * k
        k *= max(0.62, min(1.35, self.head_target / hw))
        k = min(k, self.box_w / src.width, self.box_h / src.height)
        im = src.resize((max(1, int(round(src.width * k))),
                         max(1, int(round(src.height * k)))), Image.LANCZOS)
        if self.phys.opts.get("flip") and self.phys.flip:
            im = ImageOps.mirror(im)
        if len(self.cache) > 120:
            self.cache.clear()
        self.cache[key] = im
        return im

    def _breath(self, bucket):
        ph = 2 * math.pi * bucket / BREATH_STEPS
        s = math.sin(ph)
        return 1.0 + BREATH_SCALE * s, -BREATH_LIFT * s

    def _draw_on(self, canvas, img, dy=0.0, alpha=1.0):
        """把贴图画到画布上。核心保证：任何情况下都不裁切 ——
        旋转/呼吸放大后如果放不下，先等比缩小到放得下；坐标也钳在画布内。"""
        if img is None:
            return
        max_w = self.win_w - 2
        max_h = self.area_h
        if img.width > max_w or img.height > max_h:
            k = min(max_w / img.width, max_h / img.height)
            img = img.resize((max(1, int(img.width * k)),
                              max(1, int(img.height * k))), Image.LANCZOS)
        if alpha < 0.999:
            img = img.copy()
            img.putalpha(img.split()[3].point(lambda v: int(v * alpha)))
        x = max(0, min(self.win_w - img.width, (self.win_w - img.width) // 2))
        y = max(0, min(self.win_h - img.height,
                       int(round(self.area_bottom - img.height + dy))))
        canvas.alpha_composite(img, (x, y))

    def _swing_angle(self):
        if not self.phys.opts.get("swing"):
            return 0.0
        a = self.phys.swing
        return 0.0 if abs(a) < 0.6 else a

    @staticmethod
    def _apply_swing(img, ang):
        """单摆旋转。expand=True 让旋转后的图完整保留，
        之后 _draw_on 里再统一做「放不下就等比缩小」，绝不会裁掉一角。"""
        if img is None or not ang:
            return img
        return img.rotate(ang, resample=Image.BICUBIC, expand=True)

    def _compose(self, name):
        use_breath = bool(self.cfg.get("breathing", True))
        bucket = (self.bucket % BREATH_STEPS) if use_breath else 0
        ang = self._swing_angle()
        today = round(float(getattr(self.stats, "today_credit", 0.0) or 0.0), 2)
        bkey = ((self.bubble_h, self._dot_bucket, self.quota_left,
                 self.quota_expire_left, self.quota_expire, today,
                 self.quota_status, self.quota_synced_at) if self.bubble_h else None)
        ck = (name, bucket, self._bar_gen, self._prev, self._fade, round(ang),
              self._anim_frame if self._anim_of(name) else -1, bkey)
        img = self.cache.get(ck)
        if img is not None:
            return img
        canvas = Image.new("RGBA", (self.win_w, max(1, self.win_h)), (0, 0, 0, 0))
        scale, dy = self._breath(bucket)
        cur = self._sprite_img(name)
        if cur is not None and abs(scale - 1.0) > 1e-5:
            cur = cur.resize((max(1, int(cur.width * scale)),
                              max(1, int(cur.height * scale))), Image.LANCZOS)
        cur = self._apply_swing(cur, ang)

        fade_n = getattr(self, "_fade_total_n", 0) or 0
        if self._fade > 0 and self._prev and self._prev != name and fade_n > 0:
            prev = self._sprite_img(self._prev)
            if prev is not None and abs(scale - 1.0) > 1e-5:
                prev = prev.resize((max(1, int(prev.width * scale)),
                                    max(1, int(prev.height * scale))), Image.LANCZOS)
            prev = self._apply_swing(prev, ang)
            self._draw_on(canvas, prev, dy)
            # fade 从 fade_n 递减到 0：越接近 0 越接近「完全换成新图」
            a = smoothstep((fade_n - self._fade) / float(fade_n))
            self._draw_on(canvas, cur, dy, alpha=a)
        else:
            self._draw_on(canvas, cur, dy)
        if self.bubble_h:
            ctx = self._ctx()
            ctx["quota_left"] = self.quota_left
            ctx["quota_expire_left"] = self.quota_expire_left
            ctx["quota_expire"] = self.quota_expire
            ctx["quota_status"] = self.quota_status
            ctx["quota_synced_at"] = self.quota_synced_at
            body = self.bubble.render_body(ctx)
            # 靠左摆：窗口右缘贴着屏幕边时，居中的气泡会被切掉半个「积分」
            bx = max(0, min(self.pad_x, self.win_w - body.width))
            canvas.alpha_composite(body, (bx, 0))
            canvas.alpha_composite(
                self.bubble.render_dots(body.width, self._dot_phase), (bx, body.height))
        if self.bar_img is not None:
            canvas.alpha_composite(self.bar_img,
                                   ((self.win_w - self.bar_img.width) // 2,
                                    self.area_bottom + int(round(GAP * self.S))))
        if len(self.cache) > 150:
            self.cache.clear()
        self.cache[ck] = canvas
        return canvas

    def _refresh_bar(self):
        if not self._bar_visible():
            return
        new = self.bar.render(self._ctx(), self._active_rows(),
                              self.win_w - int(6 * self.S), self._accent())
        if new.height != (self.bar_img.height if self.bar_img else -1):
            self._relayout()
            return
        if self.bar_img is None or new.tobytes() != self.bar_img.tobytes():
            self.bar_img = new
            self._bar_gen += 1
            self.cache.clear()

    # ============================================================ 主循环
    def _fade_total(self):
        """当前切换过渡的总帧数（0 = 瞬间切换）"""
        secs = TRANSITIONS.get(self.cfg.get("transition", "instant"), 0.0)
        return int(round(secs * FPS))

    def _tick(self):
        if self._stop:
            return
        _t0 = time.perf_counter()
        now = time.time()
        self._consume_balance_result()
        dt = now - getattr(self, "_last_tick", now)
        self._last_tick = now
        if not (0.0 < dt < 0.5):
            dt = 1.0 / FPS
        new_sprite = self._pick_state()
        if new_sprite != self.sprite:
            self._prev = self.sprite
            self._fade_total_n = self._fade_total()
            self._fade = self._fade_total_n
            self.sprite = new_sprite
        elif self._fade > 0:
            self._fade -= 1
        self._advance_motion()
        self._advance_anim(dt)
        # 呼吸相位按【真实时间】推进（老实现按帧数推进，
        # 导致周期 = BREATH_STEPS/FPS = 0.64 秒，快得像在抖 —— 这就是"晃眼"的根因）
        self._phase_t = (getattr(self, "_phase_t", 0.0) + dt / BREATH_PERIOD) % 1.0
        self.bucket = int(self._phase_t * BREATH_STEPS) % BREATH_STEPS
        # 思考圆点的脉动相位（2.6 秒一轮，12 档；档位变化才重绘，省 CPU）
        self._dot_t = (getattr(self, "_dot_t", 0.0) + dt / 2.6) % 1.0
        self._dot_bucket = int(self._dot_t * 12) % 12
        self._dot_phase = self._dot_bucket / 12.0
        self._bar_tick += 1
        # 3 秒定时刷新之外，数据首次到达/积分变化时也立刻刷，避免空条停留
        sig = (bool(getattr(self.stats, "has_data", False)),
               round(float(getattr(self.stats, "today_credit", 0.0)), 2))
        if self._bar_tick % (FPS * 3) == 0 or sig != getattr(self, "_bar_sig", None):
            self._bar_sig = sig
            self._refresh_bar()
            self._redraw_card()
        # 每 1.5 秒看一眼剪贴板：从 WorkBuddy 积分页复制一段文字就能更新额度
        if self._bar_tick % 90 == 0:
            self._poll_clipboard()
        # 每 1 秒重申一次置顶，跟 WorkBuddy 主窗口抢层时不至于被压住
        if self._bar_tick % FPS == 0:
            WL.reassert_topmost(self.hwnd)
        # 详情卡跟着桌宠走
        if self._bar_tick % 10 == 0:
            self._follow_card()
        bucket_key = self.bucket if self.cfg.get("breathing") else 0
        img = self._compose(self.sprite)
        self.surface.paint(img, self.x, self.y,
                           key=(self.win_w, self.win_h, self.sprite, bucket_key,
                                self._bar_gen, self._fade,
                                self.quota_left, self.quota_expire_left,
                                self.quota_expire, self.quota_status,
                                self.quota_synced_at, sig,
                                self._anim_frame, self._dot_bucket,
                                round(self._swing_angle()),
                                bool(self.phys.flip and self.phys.opts.get("flip"))))
        if os.environ.get("PET_PROF"):
            self._prof_t[0] += time.perf_counter() - _t0
            self._prof_t[1] += 1
            if self._prof_t[1] % 60 == 0:
                print("[prof] 平均 %.2f ms/帧，%d 帧，约 %.0f fps"
                      % (self._prof_t[0] / self._prof_t[1] * 1000,
                         self._prof_t[1], self._prof_t[1] / self._prof_t[0]),
                      flush=True)
        self._trace_tick += 1
        if self.trace and self._trace_tick % 10 == 0:
            print("[move] x=%d y=%d vx=%.0f vy=%.0f mode=%s grounded=%s flip=%s "
                  "swing=%.1f" % (self.x, self.y, self.phys.vx, self.phys.vy,
                                  self.phys.mode, self.phys.grounded,
                                  self.phys.flip, self.phys.swing), flush=True)
        if self.dump_dir:
            try:
                os.makedirs(self.dump_dir, exist_ok=True)
                img.save(os.path.join(self.dump_dir, "%05d.png" % self._dump_i))
                self._dump_i += 1
            except Exception:
                pass
        self._redraw_card()
        self.root.after(max(1, int(1000 / FPS)), self._tick)

    def _advance_anim(self, dt):
        """按真实时间推进动画帧；切状态时回到第 0 帧"""
        anim_key = self._anim_of(self.sprite)
        if not anim_key:
            self._anim_state = ""
            return
        state = self.anim.get(anim_key) or {}
        frames = state.get("frames") or []
        durs = state.get("durations") or []
        if not frames:
            return
        if self._anim_state != anim_key:
            self._anim_state = anim_key
            self._anim_ms = 0.0
            self._anim_frame = 0
        durs = list(durs[:len(frames)])
        while len(durs) < len(frames):
            durs.append(durs[-1] if durs else 120)
        total = float(sum(durs) or 1)
        self._anim_ms = (self._anim_ms + dt * 1000.0) % total
        acc = 0.0
        idx = len(frames) - 1
        for i, d in enumerate(durs):
            acc += d
            if self._anim_ms < acc:
                idx = i
                break
        self._anim_frame = idx

    def _advance_motion(self):
        """把这一帧的时间交给物理引擎：散步 / 游荡 / 重力 / 抛出 / 弹跳 / 撞窗口"""
        now = time.time()
        dt = now - getattr(self, "_last_step", now)
        self._last_step = now
        if not (0.0 < dt < 0.5):
            dt = 1.0 / FPS
        if self.dragging:
            return
        paused = self.hover or bool(self.cfg.get("card_open"))
        moved = self.phys.step(dt, now, paused=paused)
        nx, ny = int(round(self.phys.x)), int(round(self.phys.y))
        # 窗口不能出屏：气泡比角色本身宽，超出右边会让「积分」二字被切掉；
        # 超出底边则信息条会被任务栏压住
        wl, wt, wr, wb = WL.work_area()
        cx = max(wl, min(nx, wr - self.win_w))
        cy = max(wt, min(ny, wb - self.win_h))
        if cx != nx or cy != ny:
            nx, ny = cx, cy
            self.phys.x = float(nx)
            self.phys.y = float(ny)
            try:
                self.phys.vx = 0.0
            except Exception:
                pass
            moved = True
        if moved and (nx, ny) != (self.x, self.y):
            self.x, self.y = nx, ny
            WL.move_window(self.hwnd, self.x, self.y, self.win_w, self.win_h)

    # ============================================================ 鼠标
    def _enter(self, e):
        if not self.hover:
            self.hover = True
            # 只有开启「悬停换表情」才打断当前语义，否则只是暂停移动
            if self.cfg.get("hover_expr", False):
                self._sem_key = ""
            if self.cfg.get("bar_mode") == "auto":
                self._relayout()

    def _leave(self, e):
        if self.hover:
            self.hover = False
            if self.cfg.get("hover_expr", False):
                self._sem_key = ""
            if self.cfg.get("bar_mode") == "auto":
                self._relayout()

    def _press(self, e):
        self._off = (e.x_root - self.x, e.y_root - self.y)
        self._moved = False
        self._t0 = time.time()
        self._drag_t = time.time()
        self.vx = self.vy = 0.0
        self._chain = []                    # 拖拽打断「吃白饭」小剧场
        self._sem_key = ""

    def _drag(self, e):
        self.dragging = True
        nx, ny = e.x_root - self._off[0], e.y_root - self._off[1]
        if abs(nx - self.x) + abs(ny - self.y) > 2:
            self._moved = True
        now = time.time()
        dt = max(1e-3, min(0.15, now - getattr(self, "_drag_t", now)))
        self._drag_t = now
        # 拖拽速度（像素/秒）—— 松手时用来抛出、也用来驱动单摆
        self.vx = (nx - self.x) / dt
        self.vy = (ny - self.y) / dt
        self.x, self.y = nx, ny
        self.phys.on_drag(nx, ny, self.vx, self.vy, dt)
        WL.move_window(self.hwnd, self.x, self.y, self.win_w, self.win_h)

    def _release(self, e):
        self.dragging = False
        self._sem_key = ""
        if self._moved:
            self.phys.on_release()             # 交给物理：抛出 / 落回底边
            self._save()
        elif time.time() - self._t0 < 0.7:
            self._poke()
        self._last_step = time.time()

    def _poke(self):
        if self.stats and self.stats.has_data and self.stats.context_ratio >= 0.9:
            sp = self._pick_from("peak")
        elif self.stats and self.stats.has_data and self.stats.active:
            sp = self._pick_from("happy")
        else:
            sp = self._pick_from(random.choice(["happy", "shy", "idle"]))
        self._forced = (sp, time.time() + 1.2)
        self._sem_key = ""

    # ============================================================ 收尾
    def _save(self):
        self.cfg.update({"x": self.x, "y": self.y,
                         "card_x": getattr(self, "card_x", None),
                         "card_y": getattr(self, "card_y", None)})
        try:
            with open(CONFIG, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _quit(self):
        self._save()
        self._stop = True
        try:
            os.remove(os.path.join(ROOT, "pet.pid"))
        except Exception:
            pass
        try:
            if getattr(self, "_hi_res_timer", False):
                import ctypes as _ct
                _ct.windll.winmm.timeEndPeriod(1)
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self):
        if self.cfg.get("card_open"):
            self._ensure_card()
        print("[pet] dpi=%.2f form=%s %.2fx box=%dx%d win=%dx%d pos=(%d,%d) "
              "head=%.0f fps=%d"
              % (self.scale, self.cfg.get("form"), self.cfg.get("sprite_scale", 1.0),
                 self.box_w, self.box_h, self.win_w, self.win_h, self.x, self.y,
                 self.head_target, FPS), flush=True)
        self.root.mainloop()
        self._stop = True


# ================================================================ 开机自启
def install_autostart(on: bool) -> bool:
    startup = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows",
                           "Start Menu", "Programs", "Startup")
    if not os.path.isdir(startup):
        return False
    bat = os.path.join(startup, "DeepSeekWhalePet.bat")
    try:
        if on:
            pyw = os.path.join(ROOT, "tools", "venv-tk", "Scripts", "pythonw.exe")
            if not os.path.exists(pyw):
                pyw = sys.executable.replace("python.exe", "pythonw.exe")
            with open(bat, "w", encoding="ascii", errors="ignore") as f:
                f.write('@echo off\r\nstart "" "%s" "%s"\r\n'
                        % (pyw, os.path.join(ROOT, "src", "pet.py")))
            return True
        if os.path.exists(bat):
            os.remove(bat)
        return True
    except Exception:
        return False


def _acquire_single_instance():
    """全局命名互斥：保证只有一个桌宠实例

    看门脚本万一跑了两份、或者用户手贱双击了两次 start-pet.bat，
    第二个实例会在这里直接退出，不会出现两只鲸鱼同屏。
    返回句柄；拿不到就返回 None。
    """
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        handle = k32.CreateMutexW(None, False, "Local\\WorkBuddyWhalePet")
        if k32.GetLastError() == 183:      # ERROR_ALREADY_EXISTS
            return None
        return handle
    except Exception:
        # 拿不到锁就当没有锁，宁可多开也不要起不来
        return True


def main():
    lock = _acquire_single_instance()
    if lock is None:
        print("[pet] 已经有一只鲸鱼娘在跑了，本次退出", flush=True)
        return
    app = PetApp()
    try:
        app.run()
    except KeyboardInterrupt:
        app._quit()


if __name__ == "__main__":
    main()
