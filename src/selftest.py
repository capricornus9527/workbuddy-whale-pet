# -*- coding: utf-8 -*-
"""selftest.py —— 分层自检（不依赖真实桌面，可反复跑）

    L1  纯逻辑：定价规则 / 用量解析与去重 / 状态机 / 排版 —— 无副作用
    L2  资源完整性：18 张贴图是否都在、是否带 alpha、尺寸是否合理
    L3  渲染冒烟：信息条、详情卡真的能画出来，且尺寸符合预期
    L4  端到端：真的把桌宠跑起来 → 截屏 → 检查屏幕像素 → 关掉

用法： python src\\selftest.py [--e2e]
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import math
import random
import sys
import time
import types

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

FAIL = []
OK = []


def kill_tree(p):
    """venv 的 pythonw.exe 只是启动器，terminate() 只杀父进程，真正的
    pythonw3.13.exe 会变孤儿（而且它还攥着 stdout 管道，让 communicate 挂起）。
    所以必须杀整棵进程树。"""
    try:
        import subprocess as _sp
        _sp.run(["taskkill", "/T", "/F", "/PID", str(p.pid)],
                capture_output=True, timeout=10)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass
    try:
        return p.communicate(timeout=4)[0]
    except Exception:
        return ""


def check(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    print("%s %-52s %s" % ("✅" if cond else "❌", name, extra))


# ================================================================ L1 定价
def t_pricing():
    import pricing as P
    TZ = P.TZ
    D = lambda *a: _dt.datetime(*a, tzinfo=TZ)
    wd = D(2026, 9, 15)          # 周二
    we = D(2026, 9, 19)          # 周六
    check("高峰: 周二 09:30", P.is_peak(wd.replace(hour=9, minute=30)))
    check("高峰: 周二 11:59", P.is_peak(wd.replace(hour=11, minute=59)))
    check("平峰: 周二 12:00（午休）", not P.is_peak(wd.replace(hour=12)))
    check("高峰: 周二 14:00", P.is_peak(wd.replace(hour=14)))
    check("平峰: 周二 18:00", not P.is_peak(wd.replace(hour=18)))
    check("平峰: 周二 03:00（夜低谷）", not P.is_peak(wd.replace(hour=3)))
    check("平峰: 周六 10:00（周末）", not P.is_peak(we.replace(hour=10)))
    check("平峰: 周日 15:00", not P.is_peak(D(2026, 9, 20, 15)))
    check("高峰价=2x 平峰价",
          abs(P.unit_price("deepseek-flash", "output", wd.replace(hour=10))
              - 2 * P.unit_price("deepseek-flash", "output", we.replace(hour=10))) < 1e-9)
    check("flash 平峰输出 4元/M",
          abs(P.cost_cny(0, 1_000_000, 0, "deepseek-flash", we.replace(hour=10)) - 4.0) < 1e-9)
    check("flash 高峰输出 8元/M",
          abs(P.cost_cny(0, 1_000_000, 0, "deepseek-flash", wd.replace(hour=10)) - 8.0) < 1e-9)
    check("缓存命中低价 0.02元/M",
          abs(P.cost_cny(0, 0, 1_000_000, "deepseek-flash", we.replace(hour=10)) - 0.02) < 1e-9)
    check("未命中 = 输入 - 命中",
          abs(P.cost_cny(1_000_000, 0, 400_000, "deepseek-flash", we.replace(hour=10))
              - (0.6 * 1.0 + 0.4 * 0.02)) < 1e-9)
    check("旧模型名映射 flash", P.canon_model("deepseek-v4-flash") == "deepseek-flash")
    check("未知模型名回落到默认", P.canon_model("gpt-9") == P.DEFAULT_MODEL)
    nm, hrs = P.next_switch(wd.replace(hour=10))
    check("下一个时段判定正确", nm == "梁文谷" and 1.9 < hrs < 2.1, "%.2fh" % hrs)

    # ---- 价格是分档的：必须按「调用当时」的价格算，不能拿今天的价乘历史用量
    # 用 20:00 避开高峰倍率，纯验分档逻辑
    # 9/10 12:00 flash 降价：缓存命中 0.05 -> 0.02（空闲价）
    check("9/10 前 flash 命中 0.05元/M",
          abs(P.unit_price("deepseek-flash", "cache_hit", D(2026, 9, 9, 20)) - 0.05) < 1e-9)
    check("9/10 后 flash 命中 0.02元/M",
          abs(P.unit_price("deepseek-flash", "cache_hit", D(2026, 9, 11, 20)) - 0.02) < 1e-9)
    check("9/10 前 flash 未命中 1.50元/M",
          abs(P.unit_price("deepseek-flash", "cache_miss", D(2026, 9, 9, 20)) - 1.50) < 1e-9)
    # 9/14 12:00 起 pro 请求路由到 V4.1 Flash，按 flash 价计费
    check("9/14 前 pro 未命中 4.50元/M",
          abs(P.unit_price("deepseek-v4-pro", "cache_miss", D(2026, 9, 13, 20)) - 4.50) < 1e-9)
    check("9/14 后 pro 未命中 1.00元/M",
          abs(P.unit_price("deepseek-v4-pro", "cache_miss", D(2026, 9, 15, 20)) - 1.00) < 1e-9)
    check("高峰时 pro 也翻倍（9/15 周二 10:00 = 2.00）",
          abs(P.unit_price("deepseek-v4-pro", "cache_miss", D(2026, 9, 15, 10)) - 2.00) < 1e-9)
    # 峰谷计费 2026-08-17 才开始，此前是全天一口价
    check("峰谷计费前不加倍（8/10 周一 10:00）",
          not P.is_peak(D(2026, 8, 10, 10))
          and abs(P.unit_price("deepseek-flash", "output", D(2026, 8, 10, 10)) - 4.50) < 1e-9)
    check("价格不确定窗口被标记", P.is_price_uncertain(D(2026, 8, 10, 10)) is True
          and P.is_price_uncertain(D(2026, 9, 15, 10)) is False)
    # 三档计价的语义：命中与未命中必须分开传入（差 50 倍）
    check("cost_parts 区分命中/未命中",
          abs(P.cost_parts(1_000_000, 0, 0, "deepseek-flash", D(2026, 9, 19, 10)) - 1.0) < 1e-9
          and abs(P.cost_parts(0, 1_000_000, 0, "deepseek-flash", D(2026, 9, 19, 10)) - 0.02) < 1e-9)


# ================================================================ L1 用量解析
def _dsh_line(seq, ts_ms, uncached, cached, out, model="deepseek-flash",
              turn=1, step=1, reasoning=0):
    """造一行 DSH 会话日志（schema 见 usage_reader 文档头）

    注意 DSH 的语义：inputTokens 是**未命中**输入，命中部分在 cacheReadTokens。
    """
    return json.dumps({
        "type": "assistant/message", "seq": seq, "time": ts_ms,
        "data": {"turn": turn, "step": step,
                 "message": {"role": "assistant",
                             "source": {"kind": "model", "provider": "deepseek-official",
                                        "model": model}},
                 "usage": {"inputTokens": uncached, "outputTokens": out,
                           "cacheReadTokens": cached, "reasoningTokens": reasoning}},
    }, ensure_ascii=False)


SYN_LINES = [
    # 一次正常调用：未命中 2000 / 命中 98000 / 输出 2000
    _dsh_line(1, 1789262000000, 2000, 98000, 2000, reasoning=1500),
    # 同 seq 的重复行 → 必须去重
    _dsh_line(1, 1789262000000, 2000, 98000, 2000, reasoning=1500),
    # 第二条：未命中 10000 / 命中 40000 / 输出 3000
    _dsh_line(2, 1789262060000, 10000, 40000, 3000, step=2),
    "{ 半行坏 JSON",
    json.dumps({"type": "user/message", "seq": 9, "time": 1789262061000, "data": {}}),
    # assistant/message 但没有 usage（插件消息）→ 跳过
    json.dumps({"type": "assistant/message", "seq": 10, "time": 1789262062000,
                "data": {"usage": None}}),
]


def t_usage():
    import usage_reader as U
    if not U.HAVE_ZSTD:
        check("zstandard 可用（DSH 日志解析的前提）", False, "未安装 zstandard")
        return
    import zstandard as zstd

    calls = [c for c in (U.UsageTracker._extract(s) for s in SYN_LINES) if c]
    check("坏行/无 usage 行被跳过", len(calls) == 3, "解析出 %d 条" % len(calls))
    check("字段抽取正确（未命中/命中/输出分开）",
          calls[0].uncached_in == 2000 and calls[0].cached == 98000
          and calls[0].out_tokens == 2000 and calls[0].reasoning == 1500)
    check("in_tokens = 输入总量（未命中 + 命中）", calls[0].in_tokens == 100000,
          str(calls[0].in_tokens))
    check("model 字段被读取", calls[0].model == "deepseek-flash")
    check("seq 作为去重键", calls[0].key == "1" and calls[1].key == "1")

    # 用临时目录写一个真的 .jsonl.zstd，跑一次完整 poll
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        sess = os.path.join(td, "proj", "session-abc")
        os.makedirs(sess)
        p = os.path.join(sess, "session.v3.jsonl.zstd")
        with open(p, "wb") as f:
            f.write(zstd.ZstdCompressor().compress(
                ("\n".join(SYN_LINES) + "\n").encode("utf-8")))
        tr = U.UsageTracker(root=td)
        st = tr.poll()
        check("能定位到 .jsonl.zstd 会话日志",
              os.path.normcase(os.path.abspath(st.session_file or ""))
              == os.path.normcase(os.path.abspath(p)))
        check("去重后调用次数=2", st.session_calls == 2, "实际 %d" % st.session_calls)
        check("未命中累计 = 12000", st.session_uncached == 12000, str(st.session_uncached))
        check("命中累计 = 138000", st.session_cached == 138000, str(st.session_cached))
        check("输入累计（含命中）= 150000", st.session_in == 150000, str(st.session_in))
        check("输出累计 = 5000", st.session_out == 5000, str(st.session_out))
        check("缓存命中率 = 138000/150000",
              abs(st.cache_hit_ratio - 138000 / 150000) < 1e-6,
              "%.4f" % st.cache_hit_ratio)
        check("折算花费 > 0", st.session_cost > 0, "¥%.4f" % st.session_cost)

        # 追加一帧后，文件大小变了 → 应重扫并新增 1 条
        with open(p, "ab") as f:
            f.write(zstd.ZstdCompressor().compress(
                (_dsh_line(3, 1789262100000, 500, 20000, 700, step=3) + "\n").encode("utf-8")))
        st2 = tr.poll()
        check("追加后重扫，新增 1 条", st2.session_calls == 3, "实际 %d" % st2.session_calls)
        st3 = tr.poll()
        check("文件没变时不重复计数", st3.session_calls == 3)

        # 缺 zstandard 时要给出可读提示而不是抛异常
        saved = U.HAVE_ZSTD
        U.HAVE_ZSTD = False
        try:
            stx = U.UsageTracker(root=td).poll()
            check("缺 zstandard 时给出可读提示", "zstandard" in stx.error, stx.error[:40])
        finally:
            U.HAVE_ZSTD = saved


# ================================================================ L1 物理
def t_physics():
    import physics as PH

    WA = lambda: (0.0, 0.0, 2000.0, 1200.0)      # 工作区
    SZ = lambda: (200, 300)                      # 桌宠窗口尺寸

    def mk(mode="gravity", opts=None):
        p = PH.PetPhysics(WA, SZ, scale=1.0)
        p.opts = {**PH.PetPhysics(WA, SZ).opts, **(opts or {})}
        p._rect_fn = None
        p.set_mode(mode)
        return p

    def run(p, seconds, dt=1 / 60, paused=False):
        n = int(seconds / dt)
        for _ in range(n):
            p.step(dt, paused=paused)
        return p

    # ---- 重力：自由落体并停在地面
    p = mk("gravity")
    p.place(500, 100)
    p.airborne, p.grounded, p.vy = True, False, 0.0
    run(p, 3.0)
    ground = p.ground_y(300)
    check("重力：自由落体后停在地面", p.grounded and abs(p.y - ground) < 2,
          "y=%.1f ground=%.1f" % (p.y, ground))

    # ---- 抛出：会飞出去再落地
    p = mk("gravity")
    p.place(900, ground)
    p.vx, p.vy = 700.0, -900.0
    p.on_release()
    check("抛出：松手后处于空中", p.airborne and not p.grounded)
    peak = p.y
    for _ in range(900):
        p.step(1 / 60)
        peak = min(peak, p.y)
    check("抛出：真的飞起来过", peak < ground - 120, "最高 y=%.0f" % peak)
    check("抛出：最终落回地面并停下", p.grounded and abs(p.y - ground) < 2
          and abs(p.vx) < PH.STOP_EPS, "vx=%.1f" % p.vx)
    check("抛出：横向确实位移了", abs(p.x - 900) > 100, "x=%.0f" % p.x)

    # ---- 弹跳：落地瞬间速度反向
    p = mk("gravity", {"bounce": True})
    p.place(500, ground - 400)
    p.airborne, p.grounded, p.vy = True, False, 900.0
    bounced = False
    for _ in range(300):
        prev = p.vy
        p.step(1 / 60)
        if prev > 200 and p.vy < -50:
            bounced = True
            break
    check("弹跳：落地后速度反向", bounced, "vy=%.1f" % p.vy)

    # ---- 关掉弹跳就不该弹
    p = mk("gravity", {"bounce": False})
    p.place(500, ground - 200)
    p.airborne, p.grounded, p.vy = True, False, 900.0
    for _ in range(300):
        p.step(1 / 60)
        if p.grounded:
            break
    check("关闭弹跳后落地即停", p.grounded and p.vy == 0.0)

    # ---- 屏幕边缘反弹
    p = mk("gravity")
    p.place(1800, ground)
    p.vx = 1500.0
    p.airborne, p.grounded = True, False
    p.vy = 0.0
    for _ in range(240):
        p.step(1 / 60)
    check("右边框：被挡住并反弹", p.x <= 2000 - 200 + 1 and p.vx <= 0,
          "x=%.0f vx=%.0f" % (p.x, p.vx))

    # ---- 游荡/散步：会朝目标移动
    for mode in ("stroll", "roam"):
        p = mk(mode)
        p.place(300, 300)
        p.target = (900.0, 300.0 if mode == "roam" else p.ground_y(300))
        x0 = p.x
        run(p, 2.0)
        check("[%s] 会朝目标点移动" % mode, abs(p.x - x0) > 30,
              "x %.0f -> %.0f" % (x0, p.x))

    # ---- 悬停/面板打开时暂停游荡
    p = mk("roam")
    p.place(300, 300)
    p.target = (1500.0, 300.0)
    x0 = p.x
    run(p, 2.0, paused=True)
    check("悬停时游荡被暂停", abs(p.x - x0) < 1)

    # ---- 撞其他窗口：从左边撞上去会被推开并反弹
    WIN = [(1000.0, 600.0, 1400.0, 1100.0)]
    p = mk("gravity")
    p._rect_fn = lambda ex=(): WIN
    p.place(700, ground)                     # 700~900，向右走
    p.vx = 900.0
    p.vy = 0.0
    pushed = False
    for _ in range(300):
        p.step(1 / 60)
        if p.vx < 0 and p.x + 200 <= 1001:
            pushed = True
            break
    check("撞到窗口会被推开并反弹", pushed, "x=%.0f vx=%.0f" % (p.x, p.vx))
    p2 = mk("gravity", {"collide": False})
    p2._rect_fn = lambda ex=(): WIN
    p2.place(700, ground)
    p2.vx = 900.0
    p2.vy = 0.0
    for _ in range(300):
        p2.step(1 / 60)
    check("关闭碰撞后直接穿过去", p2.x > 1000, "x=%.0f" % p2.x)

    # ---- 单摆：拖拽产生摆角，松手后衰减回 0
    p = mk("gravity")
    p.place(500, ground)
    for _ in range(10):
        p.on_drag(500, ground, 900.0, 0.0, 1 / 60)
        p.step(1 / 60)
    check("单摆：快速拖拽甩出摆角", abs(p.swing) > 1.0, "swing=%.1f°" % p.swing)
    run(p, 4.0)
    check("单摆：会自己衰减回正", abs(p.swing) < 0.6, "swing=%.2f°" % p.swing)
    check("单摆：角度不会超过上限", abs(p.swing) <= PH.SWING_MAX + 0.01)

    # ---- 朝左走会转身
    p = mk("roam")
    p.place(900, 300)
    p.target = (200.0, 300.0)
    run(p, 1.0)
    check("朝左走会转身", p.flip is True)
    p.target = (1800.0, 300.0)
    run(p, 1.0)
    check("朝右走会转回来", p.flip is False)

    # ---- 待着模式不该乱动
    p = mk("still")
    p.place(600, 400)
    x0, y0 = p.x, p.y
    run(p, 3.0)
    check("「乖乖待着」完全不动", (p.x, p.y) == (x0, y0))

    # ---- 数值不能炸
    p = mk("gravity")
    p.place(1000, ground)
    p.vx, p.vy = 1e6, -1e6
    for _ in range(600):
        p.step(1 / 60)
    ok = all(map(math.isfinite, (p.x, p.y, p.vx, p.vy, p.swing)))
    check("极端速度下数值不炸", ok, "(%.0f, %.0f)" % (p.x, p.y))


# ================================================================ L2 素材
def t_assets():
    from PIL import Image
    import numpy as np
    d = os.path.join(ROOT, "assets", "pet")
    idxp = os.path.join(d, "index.json")
    check("素材索引存在", os.path.exists(idxp))
    idx = json.load(open(idxp, encoding="utf-8"))["sprites"]
    check("贴图数量 = 18", len(idx) == 18, str(len(idx)))
    need = ["base", "excited", "curious", "shy", "surprised", "speechless", "angry",
            "sad", "encouraging", "dazed", "eating", "cookie", "craving", "working",
            "angry_scene", "sleeping", "dragged", "coquettish"]
    missing = [n for n in need if n not in idx]
    check("状态名齐全", not missing, str(missing))
    bad_alpha, bad_size, worst = [], [], []
    for n, info in idx.items():
        p = os.path.join(d, info["file"])
        if not os.path.exists(p):
            bad_size.append(n); continue
        im = Image.open(p)
        a = np.asarray(im.convert("RGBA").split()[3])
        if a.min() != 0 or a.max() != 255:
            bad_alpha.append(n)
        if max(im.size) > 520 or min(im.size) < 60:
            bad_size.append(n)
        worst.append((float((a > 8).mean()), n))
    check("全部贴图带完整 alpha(含全透明)", not bad_alpha, str(bad_alpha))
    check("贴图尺寸合理(60~520)", not bad_size, str(bad_size))
    worst.sort()
    check("贴图内容占比正常(>=8%)", worst[0][0] >= 0.08,
          "最小 %s %.1f%%" % (worst[0][1], worst[0][0] * 100))


# ================================================================ L3 渲染
def t_render():
    import barlib
    import usage_reader as U
    tr0 = U.UsageTracker()          # 默认读 ~/.dsh/sessions；本机没有也能跑（stats 为空）
    st0 = tr0.poll()
    ctx = {"period": "梁文峰", "switch_hours": 1.6, "cpu": 22.0, "mem": 63.0,
           "clock": "10:24", "stats": st0}
    br = barlib.BarRenderer(1.75)
    rows = [["period"], ["speed", "cache", "cost"]]
    img = br.render(ctx, rows, 300, (255, 122, 122))
    check("信息条能渲染", img.mode == "RGBA" and img.width == 300 and img.height > 20,
          "%dx%d" % img.size)
    # 无用量数据时各模块返回空 → 只剩时段行，高度更小
    img1 = br.render({**ctx, "stats": None}, rows, 300, (255, 122, 122))
    check("无用量数据时自动只剩时段行", img1.height < img.height,
          "%d < %d" % (img1.height, img.height))
    # 三行
    img3 = br.render(ctx, [["period"], ["speed", "cache"], ["cost", "sys"]], 300, (96, 220, 176))
    check("模块可自由增行", img3.height > img.height, "%d > %d" % (img3.height, img.height))

    # 带真实缓存数据的曲线
    tr = U.UsageTracker()
    st = tr.poll()
    cr = barlib.CardRenderer(1.75)
    card = cr.render({"period": "梁文谷", "accent": (96, 220, 176), "switch_hours": 3,
                      "card_rows": [("输出速率", "%.1f tok/s" % st.out_rate),
                                    ("折算花费", "¥0.00")],
                      "history": st.history or [(0, 10), (1, 40), (2, 20)]}, 252, 152)
    check("详情卡能渲染", card.size == (int(252 * 1.75), int(152 * 1.75)),
          "%dx%d" % card.size)
    check("详情卡有实际像素", card.split()[3].getextrema()[1] > 200)


# ================================================================ L1 状态机
def _fake(stats, hovering=False, dragging=False, chain=None, forced=None,
          until=0, form="portrait"):
    import pet as P
    import usage_reader as U
    o = types.SimpleNamespace()
    o.sprite = "base"
    o.stats = stats
    o.hover = hovering
    o.dragging = dragging
    o._chain = list(chain or [])
    o._forced = forced
    o._sem_key = ""
    o._sprite_until = until
    o.cfg = {"form": form}
    idx = json.load(open(os.path.join(ROOT, "assets", "pet", "index.json"),
                         encoding="utf-8"))["sprites"]
    o.index = idx
    o._pick_from = lambda sem, _o=o: P.PetApp._pick_from(_o, sem)
    o._switch_to = lambda sem, now, d, _o=o: P.PetApp._switch_to(_o, sem, now, d)
    o._advance_idle = lambda now, _o=o: P.PetApp._advance_idle(_o, now)
    o._semantic = lambda _o=o: P.PetApp._semantic(_o)
    return o


def t_state():
    import pet as P
    import usage_reader as U
    random.seed(7)

    busy = U.Stats(); busy.has_data = True; busy.active = True
    busy.idle_seconds = 1; busy.burn_rate = 1000
    idle = U.Stats(); idle.has_data = True; idle.active = False; idle.idle_seconds = 5
    deep = U.Stats(); deep.has_data = True; deep.active = False; deep.idle_seconds = 9999

    for form in ("portrait", "full"):
        allow = P.SEMANTIC[form]
        ok = set(sum(allow.values(), [])) | set(P.UNIVERSAL.values())
        check("[%s] 差分池都在素材里" % form,
              all(x in json.load(open(os.path.join(ROOT, "assets", "pet", "index.json"),
                                      encoding="utf-8"))["sprites"] for x in ok))

    check("拖拽 → dragged", P.PetApp._pick_state(_fake(busy, dragging=True)) == "dragged")
    f = _fake(busy); f._forced = ("curious", time.time() + 5)
    check("强制状态优先", P.PetApp._pick_state(f) == "curious")
    check("长时间空闲 → 睡觉", P.PetApp._pick_state(_fake(deep)) == "sleeping")

    import pricing
    peak = pricing.is_peak()
    got = P.PetApp._pick_state(_fake(busy))
    check("干活中 → 对应差分", got in (["working"] if not peak else ["angry_scene"]),
          "%s (现在%s)" % (got, "高峰" if peak else "平峰"))
    busy.burn_rate = 2_000_000
    check("吞吐爆表 → 开心差分",
          P.PetApp._pick_state(_fake(busy)) in set(P.SEMANTIC["portrait"]["happy"]))
    # 悬停开关关闭时：干活中悬停也绝不能换图（这正是之前的 bug 根因）
    off = _fake(busy, hovering=True)
    off.cfg["hover_expr"] = False
    check("悬停(开关关闭)：干活中不换图",
          P.PetApp._pick_state(off) in (["working", "angry_scene", "cookie", "eating",
                                         "excited"]),
          "%s" % P.PetApp._pick_state(off))
    # 悬停开关打开时：才换害羞差分
    on = _fake(idle, hovering=True)
    on.cfg["hover_expr"] = True
    check("悬停(开关打开) → 害羞差分",
          P.PetApp._pick_state(on) in set(P.SEMANTIC["portrait"]["shy"])
          | set(P.UNIVERSAL.values()))

    # 最小停留：连续调用必须返回同一个差分（不再一秒一变）
    o = _fake(idle)
    seq = [P.PetApp._pick_state(o) for _ in range(300)]
    check("空闲差分不会频繁切换", len(set(seq)) <= 2,
          "300 次调用出现 %d 种：%s" % (len(set(seq)), sorted(set(seq))))

    o = _fake(idle)
    o._chain = [("want", time.time() + 5)]
    o.sprite = P.PetApp._pick_from(o, "want")     # 真实流程里链启动时会先切到该语义
    check("小剧场：链中保持", P.PetApp._pick_state(o) in set(P.SEMANTIC["portrait"]["want"]))
    o._chain = [("want", time.time() - 1), ("happy", time.time() + 5)]
    check("小剧场：切下一节", P.PetApp._pick_state(o) in set(P.SEMANTIC["portrait"]["happy"]))

    # 抽样：任何状态下都必须返回合法差分名
    names = set(o.index)
    seen = set()
    for _ in range(300):
        o2 = _fake(random.choice([busy, idle, deep]),
                   hovering=random.random() < .3,
                   dragging=random.random() < .1)
        seen.add(P.PetApp._pick_state(o2))
    check("随机抽样只产出合法差分", seen <= names, str(sorted(seen - names)))


# ================================================================ L2 比例一致性
def t_scale():
    """验证「按头宽归一」真的消掉了小人/大人跳变"""
    from PIL import Image
    import numpy as np
    import pet as P
    idx = json.load(open(os.path.join(ROOT, "assets", "pet", "index.json"),
                         encoding="utf-8"))["sprites"]

    def head_px(im):
        a = np.asarray(im.split()[3]) > 16
        H, W = a.shape
        seg = a[int(H * 0.05):int(H * 0.40)]
        return int(seg.sum(axis=1).max()) if seg.size else W

    for form in ("portrait", "full"):
        names = [n for n, i in idx.items() if i.get("form") == form]
        vals = [idx[n]["head_w"] * min(P.BOX_W / idx[n]["size"][0],
                                       P.BOX_H / idx[n]["size"][1]) for n in names]
        vals.sort()
        target = vals[len(vals) // 2]
        before, after = [], []
        for n in names:
            info = idx[n]; w, h = info["size"]
            src = Image.open(os.path.join(ROOT, "assets", "pet", info["file"])).convert("RGBA")
            k = min(P.BOX_W / w, P.BOX_H / h)
            before.append(info["head_w"] * k)
            k *= max(0.62, min(1.35, target / (info["head_w"] * k)))
            k = min(k, P.BOX_W / w, P.BOX_H / h)
            after.append(head_px(src.resize((max(1, round(w * k)), max(1, round(h * k))),
                                            Image.LANCZOS)))
        sp_b = (max(before) - min(before)) / (sum(before) / len(before))
        sp_a = (max(after) - min(after)) / (sum(after) / len(after))
        check("[%s] 头宽离散度 < 15%%" % form, sp_a < 0.15,
              "旧 %.1f%% -> 新 %.1f%%" % (sp_b * 100, sp_a * 100))
        check("[%s] 归一后确实更一致" % form, sp_a < sp_b)


# ================================================================ L3.5 交互
def t_ui():
    """真开一个窗口，把所有菜单/交互路径走一遍，确认不会炸"""
    import pet as P
    app = None
    try:
        app = P.PetApp()
        app.root.update()
        ops = [("切到悬停显示", lambda: app._set_bar_mode("auto")),
               ("加一个模块", lambda: app._toggle_module("sys")),
               ("改成 125% 大小", lambda: app._set_scale(1.25)),
               ("开详情卡", lambda: app._toggle_card()),
               ("关呼吸感", lambda: app._toggle_breath()),
               ("戳一下", lambda: app._poke()),
               ("改成完全隐藏", lambda: app._set_bar_mode("hidden")),
               ("去掉一个模块", lambda: app._toggle_module("sys")),
               ("关详情卡", lambda: app._toggle_card()),
               ("恢复常显", lambda: app._set_bar_mode("always")),
               ("恢复 100%", lambda: app._set_scale(1.0)),
               ("切自由游荡", lambda: app._set_activity("roam")),
               ("切重力模式", lambda: app._set_activity("gravity")),
               ("关掉落地弹跳", lambda: app._toggle_phys("bounce")),
               ("关掉撞窗口", lambda: app._toggle_phys("collide")),
               ("关掉摇晃", lambda: app._toggle_phys("swing")),
               ("关掉转身", lambda: app._toggle_phys("flip")),
               ("开回来", lambda: [app._toggle_phys(k) for k in
                                ("bounce", "collide", "swing", "flip")]),
               ("回底边散步", lambda: app._set_activity("stroll"))]
        for label, op in ops:
            op()
            app.root.update()
        check("%d 条交互路径无异常" % len(ops), True)
        app._relayout()
        app.root.update()
        bad = []
        for name in app.index:
            img = app._compose(name)
            if img is None or img.size != (app.win_w, app.win_h):
                bad.append(name)
        check("18 张差分都能合成到窗口尺寸", not bad, str(bad))
        exp_h = app.area_h + ((int(round(P.GAP * app.S)) + app.bar_h) if app.bar_h else 0)
        check("窗口尺寸随内容自适应", app.win_h == exp_h,
              "win %dx%d (area %d + bar %d)" % (app.win_w, app.win_h, app.area_h, app.bar_h))
        check("窗口四周有防裁切留白",
              app.win_w >= app.box_w + 2 * app.pad_x and app.area_h >= app.box_h,
              "box %dx%d pad %d/%d/%d" % (app.box_w, app.box_h, app.pad_x,
                                          app.pad_top, app.pad_bot))

        # ---- 物理真的接上了：游荡模式跑一会儿位置必须变
        app._set_activity("roam")
        app.phys.place(app.x, app.y)
        app.phys.target = (float(app.x + 260), float(app.y))
        x0, y0 = app.x, app.y
        # 物理是按真实时间推进的，测试里得真的等一会儿
        for _ in range(40):
            app._advance_motion()
            app.root.update()
            time.sleep(0.016)
        moved = abs(app.x - x0) + abs(app.y - y0)
        check("物理接入：游荡模式窗口真的会动", moved > 10, "位移 %d px" % moved)
        app._set_activity("gravity")
        check("物理接入：重力模式贴地", app.phys.grounded, "y=%d" % app.y)
        app._set_activity("stroll")

        # ---- 淡入是否「均匀」（不再突然跳变）
        import numpy as np
        raw = lambda im: np.asarray(im.convert("RGBA"), dtype=np.int16)
        app._prev = "base"
        app.sprite = "dazed"
        app.cfg["transition"] = "quick"
        n = app._fade_total()
        frames = []
        for f in range(0, n + 1):                # fade 从 0 递增到最大
            app._fade = f
            app._fade_total_n = n
            app.cache.clear()
            frames.append(raw(app._compose("dazed")))
        diffs = [float(np.abs(frames[i + 1] - frames[i]).mean())
                 for i in range(len(frames) - 1)]
        big = max(diffs) / max(1e-6, (sum(diffs) / len(diffs)))
        check("差分淡入逐帧差异均匀（无突跳）", big < 3.2,
              "%d 帧，最大/均值 = %.2f" % (len(frames), big))
        # 默认 instant：切换应当是零帧过渡
        app.cfg["transition"] = "instant"
        check("默认瞬间切换（零过渡帧）", app._fade_total() == 0)
        app.cfg["transition"] = "quick"

        # ---- 极端姿态（最大单摆 + 呼吸最大）下：不报错、贴图不被顶出画布
        import physics as _PH
        app.phys.swing = _PH.SWING_MAX          # 最大摆角
        bad2 = []
        for name in app.index:
            for b in (0, P.BREATH_STEPS // 2):   # 呼吸的两个极端相位
                app.bucket = b
                app.cache.clear()
                img = app._compose(name)
                a = np.asarray(img.split()[3])
                if (a > 16).mean() < 0.02:
                    bad2.append((name, b))
        check("极端姿态下所有差分都完整渲染（无裁切/无空图）", not bad2, str(bad2))
        app.phys.swing = app.phys.swing_v = 0.0
        app.bucket = 0

        # ---- _draw_on 直接断言：超大图也不会越过画布边缘
        big = Image.new("RGBA", (app.win_w * 2, app.win_h * 2), (200, 80, 80, 200))
        c = Image.new("RGBA", (app.win_w, app.win_h), (0, 0, 0, 0))
        app._draw_on(c, big)
        check("_draw_on 对超大贴图不会越界/报错", True)
        m = np.asarray(c.split()[3]) > 8
        check("超大贴图被缩到画布内且居中", m.any() and
              m.sum() <= (app.win_w - 2) * (app.area_h) + 8)

        # ---- 极端姿态（最大单摆 + 呼吸最大）下：不报错、贴图不被顶出画布
        import physics as _PH
        app.phys.swing = _PH.SWING_MAX          # 最大摆角
        bad2 = []
        for name in app.index:
            for b in (0, P.BREATH_STEPS // 2):   # 呼吸的两个极端相位
                app.bucket = b
                app.cache.clear()
                img = app._compose(name)
                a = np.asarray(img.split()[3])
                if (a > 16).mean() < 0.02:
                    bad2.append((name, b))
        check("极端姿态下所有差分都完整渲染（无裁切/无空图）", not bad2, str(bad2))
        app.phys.swing = app.phys.swing_v = 0.0
        app.bucket = 0

        # ---- _draw_on 直接断言：超大图也不会越过画布边缘
        big = Image.new("RGBA", (app.win_w * 2, app.win_h * 2), (200, 80, 80, 200))
        c = Image.new("RGBA", (app.win_w, app.win_h), (0, 0, 0, 0))
        app._draw_on(c, big)
        check("_draw_on 对超大贴图不会越界/报错", True)
        m = np.asarray(c.split()[3]) > 8
        check("超大贴图被缩到画布内且居中", m.any() and
              m.sum() <= (app.win_w - 2) * (app.area_h) + 8)

        # ---- 呼吸幅度是否够小
        app.cache.clear()
        a0 = raw(app._compose("base"))
        app.bucket = P.BREATH_STEPS // 4          # 相位 1/8 周期处
        app.cache.clear()
        a1 = raw(app._compose("base"))
        # 位移量：取 alpha 通道的非零包围盒差
        def bbox(arr):
            m = arr[:, :, 3] > 16
            ys, xs = np.where(m)
            return (xs.min(), xs.max(), ys.min(), ys.max())
        b0, b1 = bbox(a0), bbox(a1)
        dh = abs((b1[3] - b1[2]) - (b0[3] - b0[2]))
        dw = abs((b1[1] - b1[0]) - (b0[1] - b0[0]))
        amp = max(dh, dw) / max(1, (b0[3] - b0[2]))
        check("呼吸幅度足够轻微（<1.5% 尺寸）", amp < 0.015,
              "高度变化 %dpx / 宽度变化 %dpx（%.2f%%）" % (dh, dw, amp * 100))
        app.bucket = 0
        app._fade = 0
        app._prev = None

        # ---- 呼吸周期按真实时间推进（修复前 = BREATH_STEPS/FPS ≈ 0.64s，像在抖）
        period = P.BREATH_PERIOD
        check("呼吸周期是设计值而非帧数换算",
              abs(period - P.BREATH_STEPS / P.FPS) > 0.5,
              "周期 %.1fs vs 帧数换算 %.2fs" % (period, P.BREATH_STEPS / P.FPS))
        check("呼吸采样足够细（每步 < 半像素）",
              P.BREATH_STEPS >= 24,
              "%d 步 / 周期，每步 %.3f%%" % (P.BREATH_STEPS,
                                            P.BREATH_SCALE * 2 * 3.1416 / P.BREATH_STEPS * 100))
        check("渲染帧率达到 60", P.FPS >= 60, "%d fps" % P.FPS)
    except Exception as e:
        import traceback
        check("交互路径无异常", False,
              repr(e) + " | " + traceback.format_exc().replace("\n", " ")[:300])
    finally:
        if app is not None:
            try:
                app._quit()
            except Exception:
                pass


# ================================================================ L4 端到端
def t_e2e():
    """端到端：真起进程 → 让它把自己的每一帧导出 → 检查帧内容

    注意：不能依赖全屏截屏 —— 玩家一开全屏游戏，所有置顶窗口都会被盖住，
    截屏里根本看不到桌宠。所以以「导出帧」为准，截屏只作参考。
    """
    import glob
    import subprocess
    import numpy as np
    from PIL import Image

    py = sys.executable
    dump = os.path.join(ROOT, "ref", "dump")
    os.makedirs(dump, exist_ok=True)
    t0 = time.time()          # 用 mtime 判定「本次新增」：桌宠每次启动都从 00000.png
                              # 重新编号并覆盖旧帧，靠文件总数增长会算错（连跑两次必失败）
    env = dict(os.environ, PET_DUMP_DIR=dump, PET_TRACE="1")
    p = subprocess.Popen([py, os.path.join(HERE, "pet.py")], cwd=ROOT,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, env=env)
    try:
        time.sleep(11)
        alive = p.poll() is None
        check("桌宠进程存活", alive)
    finally:
        out = kill_tree(p)
    check("子进程无异常输出", "Traceback" not in (out or ""), (out or "").strip()[:160])

    files = sorted(f for f in glob.glob(os.path.join(dump, "*.png"))
                   if os.path.getmtime(f) >= t0 - 1)
    check("桌宠确实在渲染（本次导出帧数 > 60）", len(files) > 60, "新增 %d 帧" % len(files))
    if not files:
        return
    # 检查帧里真的有信息条（描边 / 时段文字用的就是强调色）
    hit = 0
    for f in files[::6]:
        try:
            a = np.asarray(Image.open(f).convert("RGB")).astype(int)
        except Exception:
            continue      # 进程被 taskkill 时最后一帧可能只写了一半，跳过即可
        ok = False
        for c in ((96, 220, 176), (255, 122, 122), (96, 158, 245)):
            if (np.abs(a - np.array(c)).sum(axis=2) < 60).sum() > 60:
                ok = True
                break
        hit += ok
    check("帧里画出了信息条（强调色像素）", hit >= 3, "%d 帧命中" % hit)

    # 物理接入：轨迹里应当出现位移
    moves = [ln for ln in (out or "").splitlines() if ln.startswith("[move]")]
    check("物理在跑（有运动轨迹输出）", len(moves) >= 3, "%d 条轨迹" % len(moves))

    # 屏幕检查（仅参考：全屏游戏会盖住置顶窗口）
    try:
        import screencap
        shot = np.asarray(screencap.grab().convert("RGB")).astype(int)
        cnt = sum(int((np.abs(shot - np.array(c)).sum(axis=2) < 60).sum())
                  for c in ((96, 220, 176), (255, 122, 122), (96, 158, 245)))
        print("   （参考）屏幕上强调色像素 %d —— 全屏游戏会把它挡掉，不算失败" % cnt)
    except Exception:
        pass
    print("   导出帧:", dump)


# ================================================================ main
def main():
    print("=" * 68)
    print("L1 定价规则");       t_pricing()
    print("L1 用量解析");       t_usage()
    print("L1 状态机");         t_state()
    print("L1 物理引擎");       t_physics()
    print("L2 素材完整性");     t_assets()
    print("L2 比例一致性");     t_scale()
    print("L3 渲染冒烟");       t_render()
    print("L3.5 交互路径");     t_ui()
    if "--e2e" in sys.argv:
        print("L4 端到端");     t_e2e()
    print("=" * 68)
    print("通过 %d 项，失败 %d 项" % (len(OK), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  ❌", f)
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
