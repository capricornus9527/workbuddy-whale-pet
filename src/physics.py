# -*- coding: utf-8 -*-
"""physics.py —— 桌宠的物理引擎（参考 Vast-Beyond-Space/pet-app 的 float.js）

四种活动模式：
    still    乖乖待着
    stroll   沿屏幕底边散步（撞边反弹）
    roam     自由游荡：随机挑一个目标点，匀速走过去，到了等 2~6 秒再来
    gravity  重力模式：贴地行走；被拎起来松手 → 抛物线飞出去，落地弹跳

可选物理开关（右键菜单里逐项开关）：
    throw    松手时带惯性抛出（抛物线）
    bounce   落地弹跳（速度衰减）
    collide  撞到其它应用窗口会反弹
    swing    被拖拽时单摆式摇晃
    flip     往左走时水平翻转贴图

这个模块**完全不依赖 tkinter**，纯数值 —— 所以自检可以直接喂它 dt 做断言。
"""

from __future__ import annotations

import math
import random
import time

# 单位：逻辑像素 / 秒
G = 1700.0                 # 重力加速度
MAX_THROW = 1900.0         # 抛出速度上限
EDGE_DAMP = 0.45           # 撞屏幕边缘的能量保留
GROUND_DAMP = 0.42         # 落地弹跳的能量保留
GROUND_FRICTION = 0.72     # 每次落地时的水平摩擦
ROLL_FRICTION = 0.985      # 贴地滚动时的每秒衰减
STOP_EPS = 26.0            # 速度低于此值就停住
ROAM_SPEED = 46.0          # 游荡速度（逻辑像素/秒）
STROLL_SPEED = 34.0        # 底边散步速度
ROAM_WAIT = (2.0, 6.0)     # 到点后的发呆时间
SWING_K = 90.0             # 单摆刚度
SWING_D = 7.0              # 单摆阻尼
SWING_MAX = 13.0           # 单摆最大角度（度）


class PetPhysics:
    def __init__(self, work_area_fn, size_fn, scale=1.0):
        self.work_area = work_area_fn          # () -> (left, top, right, bottom)
        self.size = size_fn                    # () -> (win_w, win_h)
        self.scale = scale
        self.mode = "still"
        self.x = self.y = 0.0
        self.vx = self.vy = 0.0
        self.grounded = True
        self.airborne = False
        self.flip = False
        self.swing = 0.0                       # 当前摆角（度）
        self.swing_v = 0.0
        self.target = None
        self.wait_until = 0.0
        self.opts = {"throw": True, "bounce": True, "collide": True,
                     "swing": True, "flip": True}
        self._rects = []
        self._rects_at = -99.0
        self._rect_fn = None
        self.exclude = ()                      # 需要排除的窗口句柄

    # ------------------------------------------------------------ 基本
    @property
    def s(self):
        return self.scale

    def place(self, x, y):
        self.x, self.y = float(x), float(y)
        self.vx = self.vy = 0.0
        self.grounded = True
        self.airborne = False

    def set_mode(self, mode, now=None):
        now = now if now is not None else time.time()
        self.mode = mode
        self.target = None
        self.wait_until = 0.0
        if mode in ("stroll", "roam", "gravity"):
            self._pick_target(now)
        if mode == "gravity":
            self._snap_ground()

    def ground_y(self, win_h):
        l, t, r, b = self.work_area()
        return b - win_h - 8 * self.s

    def _snap_ground(self):
        w, h = self.size()
        self.y = self.ground_y(h)
        self.grounded = True
        self.airborne = False
        self.vy = 0.0

    def _pick_target(self, now):
        l, t, r, b = self.work_area()
        w, h = self.size()
        m = 16 * self.s
        if self.mode == "gravity":
            self.target = (self.x, self.ground_y(h))
            return
        if self.mode == "roam":
            rx = min(r - l, b - t) / 3.0
        else:                                   # stroll
            rx = (r - l) / 4.0
        minx, maxx = l + m, max(l + m, r - w - m)
        miny, maxy = t + m, max(t + m, b - h - m)
        if self.mode == "stroll":
            tx = min(max(self.x + (random.random() - 0.5) * 2 * rx, minx), maxx)
            ty = self.ground_y(h)
        else:
            tx = min(max(self.x + (random.random() - 0.5) * 2 * rx, minx), maxx)
            ty = min(max(self.y + (random.random() - 0.5) * 2 * rx, miny), maxy)
        self.target = (tx, ty)

    # ------------------------------------------------------------ 拖拽
    def on_drag(self, x, y, vx, vy, dt):
        self.x, self.y = float(x), float(y)
        self.vx, self.vy = float(vx), float(vy)
        self.grounded = False
        if self.opts["swing"]:
            # 单摆：横向拖得越快，甩得越开
            self.swing_v += (-vx * 0.012) * 60.0 * dt
            self.swing_v = max(-260.0, min(260.0, self.swing_v))

    def on_release(self, now=None):
        now = now if now is not None else time.time()
        self.target = None
        self.wait_until = now + 0.6
        if self.mode == "gravity":
            if self.opts["throw"]:
                cap = MAX_THROW * self.s
                self.vx = max(-cap, min(cap, self.vx))
                self.vy = max(-cap, min(cap, self.vy))
                if abs(self.vx) < 20 and abs(self.vy) < 20:
                    self.vx = random.choice((-1, 1)) * 260 * self.s
                self.airborne = True
                self.grounded = False
            else:
                self._snap_ground()
        else:
            self.airborne = False
            self.grounded = True
        self.swing_v += max(-200.0, min(200.0, self.vx * 0.05))

    # ------------------------------------------------------------ 主循环
    def step(self, dt, now=None, paused=False):
        """推进物理；返回是否发生了位移（调用方据此决定要不要移动窗口）"""
        now = now if now is not None else time.time()
        dt = max(1e-4, min(0.1, dt))
        self._step_swing(dt)
        if self.mode == "still":
            return False

        w, h = self.size()
        l, t, r, b = self.work_area()
        ground = self.ground_y(h)
        moved = False

        if self.mode == "gravity":
            moved |= self._step_gravity(dt, ground, l, t, r, w, h)
        else:
            moved |= self._step_walk(dt, now, paused, ground, l, t, r, w, h)

        if moved:
            moved |= self._step_collide(l, t, r, b, w, h)
        return moved

    def _step_swing(self, dt):
        # 阻尼弹簧回到 0 度
        self.swing_v += (-SWING_K * self.swing) * dt
        self.swing_v -= SWING_D * self.swing_v * dt
        self.swing += self.swing_v * dt
        if self.swing > SWING_MAX:
            self.swing, self.swing_v = SWING_MAX, -abs(self.swing_v) * 0.4
        elif self.swing < -SWING_MAX:
            self.swing, self.swing_v = -SWING_MAX, abs(self.swing_v) * 0.4
        if abs(self.swing) < 0.05 and abs(self.swing_v) < 0.5:
            self.swing = self.swing_v = 0.0

    def _step_gravity(self, dt, ground, l, t, r, w, h):
        moved = False
        if self.airborne or not self.grounded:
            self.vy += G * self.s * dt
            self.x += self.vx * dt
            self.y += self.vy * dt
            moved = True
            # 屏幕左右 / 顶部
            if self.x < l:
                self.x, self.vx = float(l), abs(self.vx) * EDGE_DAMP
            elif self.x + w > r:
                self.x, self.vx = float(r - w), -abs(self.vx) * EDGE_DAMP
            if self.y < t:
                self.y, self.vy = float(t), abs(self.vy) * EDGE_DAMP
            if self.y >= ground:
                self.y = float(ground)
                if abs(self.vy) > 130 * self.s and self.opts["bounce"]:
                    self.vy = -abs(self.vy) * GROUND_DAMP
                    self.vx *= GROUND_FRICTION
                else:
                    self.vy = 0.0
                    self.airborne = False
                    self.grounded = True
                    self.vx *= GROUND_FRICTION
        elif self.grounded:
            # 贴地：横向滑行 + 走路
            self.vx *= ROLL_FRICTION ** (dt * 60)
            if abs(self.vx) > STOP_EPS * self.s:
                self.x += self.vx * dt
                moved = True
            else:
                self.vx = 0.0
            if self._walk_towards_target(dt, STROLL_SPEED, w, h):
                moved = True
            if self.x < l:
                self.x, self.vx = float(l), abs(self.vx) * EDGE_DAMP
            elif self.x + w > r:
                self.x, self.vx = float(r - w), -abs(self.vx) * EDGE_DAMP
            self.y = float(ground)
        return moved

    def _step_walk(self, dt, now, paused, ground, l, t, r, w, h):
        if paused:
            return False
        moved = False
        speed = ROAM_SPEED if self.mode == "roam" else STROLL_SPEED
        moved |= self._walk_towards_target(dt, speed, w, h)
        # 边缘反弹
        hit = False
        if self.x < l:
            self.x, hit = float(l), True
        elif self.x + w > r:
            self.x, hit = float(r - w), True
        if self.mode == "roam":
            if self.y < t:
                self.y, hit = float(t), True
            elif self.y + h > t + (self.work_area()[3] - t):
                self.y, hit = float(self.work_area()[3] - h), True
        else:
            self.y = float(ground)
        if hit:
            self.target = None
            self.wait_until = now + random.uniform(0.4, 1.2)
            moved = True
        return moved

    def _walk_towards_target(self, dt, speed, w, h):
        now = time.time()
        if self.target is None:
            if now >= self.wait_until:
                self._pick_target(now)
                if self.target is None:
                    return False
            else:
                return False
        tx, ty = self.target
        dx, dy = tx - self.x, ty - self.y
        dist = math.hypot(dx, dy)
        step = speed * self.s * dt
        if dist <= max(step, 1.5):
            self.x, self.y = tx, ty
            self.target = None
            self.wait_until = now + random.uniform(*ROAM_WAIT)
            return True
        self.x += dx / dist * step
        self.y += dy / dist * step
        if self.opts["flip"]:
            if dx > 1.5:
                self.flip = False
            elif dx < -1.5:
                self.flip = True
        return True

    # ------------------------------------------------------------ 撞窗口
    def _step_collide(self, l, t, r, b, w, h):
        if not self.opts["collide"] or self._rect_fn is None:
            return False
        now = time.time()
        if now - self._rects_at > 1.0:          # 每秒刷新一次窗口列表
            self._rects_at = now
            try:
                self._rects = self._rect_fn(self.exclude)
            except Exception:
                self._rects = []
        if not self._rects:
            return False
        pr = (self.x, self.y, self.x + w, self.y + h)
        area = float(max(1, w * h))
        for wr in self._rects:
            ox = min(pr[2], wr[2]) - max(pr[0], wr[0])
            oy = min(pr[3], wr[3]) - max(pr[1], wr[1])
            if ox <= 0 or oy <= 0:
                continue
            # 已经陷得很深（比如桌宠被拖进窗口里）就不硬推，免得乱弹
            if ox * oy > area * 0.45:
                continue
            # 贴地时只做水平推开 —— 否则会和「地面吸附」互相打架，来回抖
            horizontal = (ox < oy) or (self.mode == "gravity" and self.grounded
                                       and not self.airborne)
            if horizontal:
                if self.x + w / 2 < (wr[0] + wr[2]) / 2:
                    self.x = wr[0] - w - 1
                    self.vx = -abs(self.vx) * EDGE_DAMP
                else:
                    self.x = wr[2] + 1
                    self.vx = abs(self.vx) * EDGE_DAMP
            else:
                if self.y + h / 2 < (wr[1] + wr[3]) / 2:
                    self.y = wr[1] - h - 1
                    self.vy = -abs(self.vy) * EDGE_DAMP
                else:
                    self.y = wr[3] + 1
                    self.vy = abs(self.vy) * EDGE_DAMP
            self.target = None
            self.wait_until = time.time() + random.uniform(0.3, 1.0)
            return True
        return False
