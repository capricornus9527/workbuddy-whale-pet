# -*- coding: utf-8 -*-
"""barlib.py —— 桌宠的「模块化信息条」与「迷你详情卡」绘制

设计目标：小巧、模块化。
    * 信息条由若干「模块」组成，模块可单独开关、可重排
    * 每个模块返回 (文本, 颜色)，由排版器塞进 N 行
    * 尺寸全部按 DPI 缩放，字号用像素（避免点值在高 DPI 下暴涨）
"""

from __future__ import annotations

import math
import os

from PIL import Image, ImageDraw, ImageFont, ImageFilter

FONT_PATHS = [r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\msyh.ttc",
              r"C:\Windows\Fonts\simhei.ttf"]

_FONT_CACHE = {}


def font(size, bold=False):
    key = (int(size), bold)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    order = FONT_PATHS if bold else [p for p in FONT_PATHS if "bd" not in p] + FONT_PATHS
    f = None
    for p in order:
        try:
            f = ImageFont.truetype(p, int(size))
            break
        except Exception:
            continue
    if f is None:
        try:
            f = ImageFont.load_default(size=int(size))
        except Exception:
            pass
    _FONT_CACHE[key] = f
    return f


def text_size(d, s, f):
    if f is None:
        return len(s) * 6, 12
    b = d.textbbox((0, 0), s, font=f)
    return b[2] - b[0], b[3] - b[1]


# ================================================================ 模块
class Module:
    """信息条模块基类：返回 [(文本, 颜色), ...]"""

    key = "?"
    label = "?"

    def __init__(self, scale=1.0):
        self.scale = scale

    def render(self, ctx):
        return []


def _fmt_tokens(n):
    n = float(n)
    if n >= 1e9:
        return "%.1fG" % (n / 1e9)
    if n >= 1e6:
        return "%.2fM" % (n / 1e6)
    if n >= 1e3:
        return "%.1fK" % (n / 1e3)
    return "%d" % int(n)


def _fmt_money(v):
    if v >= 100:
        return "%.0f" % v
    if v >= 10:
        return "%.1f" % v
    if v >= 1:
        return "%.2f" % v
    if v >= 0.01:
        return "%.3f" % v
    return "%.4f" % v


class PeriodModule(Module):
    key = "period"
    label = "今日积分"

    def render(self, ctx):
        st = ctx.get("stats")
        if not st or not st.has_data:
            return []
        col = (255, 214, 140)
        return [("今日 %s 积分" % _fmt_money(st.today_credit), col),
                ("%d 轮" % st.turns_today, (150, 165, 200))]


class SpeedModule(Module):
    key = "speed"
    label = "输出速率"

    def render(self, ctx):
        st = ctx.get("stats")
        if not st or not st.has_data:
            return []
        return [("%.0f tok/s" % st.out_rate, (130, 200, 255))]


class ThroughputModule(Module):
    key = "burn"
    label = "消耗速率"

    def render(self, ctx):
        st = ctx.get("stats")
        if not st or not st.has_data:
            return []
        return [("%s tok/min" % _fmt_tokens(st.burn_rate), (170, 190, 235))]


class CacheModule(Module):
    key = "cache"
    label = "缓存命中"

    def render(self, ctx):
        st = ctx.get("stats")
        if not st or not st.has_data:
            return []
        r = st.cache_hit_ratio
        col = (122, 226, 186) if r >= 0.9 else ((240, 200, 120) if r >= 0.6 else (255, 150, 150))
        return [("命中%.0f%%" % (r * 100), col)]


class CostModule(Module):
    key = "cost"
    label = "本轮积分"

    def render(self, ctx):
        st = ctx.get("stats")
        if not st or not st.has_data:
            return []
        return [("本轮 %s" % _fmt_money(st.turn_credit), (255, 214, 140))]


class CostRateModule(Module):
    key = "cost_rate"
    label = "积分速度"

    def render(self, ctx):
        st = ctx.get("stats")
        if not st or not st.has_data:
            return []
        return [("%s 积分/h" % _fmt_money(st.cost_per_hour), (235, 190, 130))]


class SessionModule(Module):
    key = "session"
    label = "上下文余量"

    def render(self, ctx):
        st = ctx.get("stats")
        if not st or not st.has_data or not st.context_size:
            return []
        pct = st.context_ratio * 100
        col = ((122, 226, 186) if pct < 70
               else ((240, 200, 120) if pct < 88 else (255, 150, 150)))
        return [("上下文 %.0f%%" % pct, col),
                ("剩 %s" % _fmt_tokens(st.context_left), (160, 180, 215))]


class SysModule(Module):
    key = "sys"
    label = "CPU/内存"

    def render(self, ctx):
        c = ctx.get("cpu", -1)
        m = ctx.get("mem", -1)
        out = []
        if c >= 0:
            out.append(("CPU%.0f%%" % c, (200, 180, 235)))
        if m >= 0:
            out.append(("内存%.0f%%" % m, (180, 205, 235)))
        return out


class CallsModule(Module):
    key = "calls"
    label = "调用次数"

    def render(self, ctx):
        st = ctx.get("stats")
        if not st or not st.has_data:
            return []
        return [("%d 次调用" % st.session_calls, (160, 178, 210))]


class ClockModule(Module):
    key = "clock"
    label = "当前时间"

    def render(self, ctx):
        return [(ctx.get("clock", ""), (150, 165, 200))]


MODULES = {m.key: m for m in (
    PeriodModule, SpeedModule, ThroughputModule, CacheModule, CostModule,
    CostRateModule, SessionModule, SysModule, CallsModule, ClockModule)}


# ================================================================ 排版
class BarRenderer:
    """把模块排版成 N 行小卡片，渲染成 RGBA 图"""

    def __init__(self, scale=1.0):
        self.s = scale
        self.cache = {}

    def _px(self, v):
        return max(8, int(round(v * self.s)))

    def layout(self, ctx, rows, width, radius_pad=10):
        """rows: [["period"], ["speed","cache","cost"]]"""
        d = Image.new("RGBA", (8, 8))
        dd = ImageDraw.Draw(d)
        f = font(self._px(10.5))
        sep = " · "
        lines = []
        for row in rows:
            segs = []
            for k in row:
                mod = MODULES.get(k)
                if not mod:
                    continue
                segs += mod(self.s).render(ctx)
            if not segs:
                continue
            # 逐段测宽，超宽就折行
            cur, curw = [], 0
            avail = width - radius_pad * 2
            for text, col in segs:
                w, _ = text_size(dd, text, f)
                sw, _ = text_size(dd, sep, f) if cur else (0, 0)
                if cur and curw + sw + w > avail:
                    lines.append(cur)
                    cur, curw = [(text, col)], w
                else:
                    cur.append((text, col))
                    curw += sw + w
            if cur:
                lines.append(cur)
        return lines

    def render(self, ctx, rows, width, accent, alpha=218):
        key = (tuple(tuple(x) for r in rows for x in [tuple(map(str, r))]),
               tuple(sorted((k, str(v)[:24]) for k, v in ctx.items()
                            if k in ("period", "switch_hours", "clock", "cpu", "mem"))),
               width, accent)
        lines = self.layout(ctx, rows, width)
        sig = "|".join(" ".join(t for t, _ in ln) for ln in lines)
        key = (sig, width, alpha, tuple(accent))
        if key in self.cache:
            return self.cache[key]

        s = self.s
        f = font(self._px(10.5))
        lh = self._px(15)
        pad = self._px(10)
        h = pad * 2 + lh * len(lines)
        img = Image.new("RGBA", (width, max(1, h)), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([0, 0, width - 1, img.height - 1],
                            radius=self._px(9), fill=(18, 23, 42, alpha),
                            outline=tuple(accent) + (150,), width=max(1, self._px(1)))
        y = pad
        for ln in lines:
            x = pad
            for i, (text, col) in enumerate(ln):
                if i:
                    d.text((x, y), " · ", font=f, fill=(96, 110, 145, 255))
                    x += text_size(d, " · ", f)[0]
                d.text((x, y), text, font=f, fill=col + (255,))
                x += text_size(d, text, f)[0]
            y += lh
        if len(self.cache) > 60:
            self.cache.clear()
        self.cache[key] = img
        return img


# ================================================================ 迷你详情卡
class CardRenderer:
    def __init__(self, scale=1.0):
        self.s = scale
        self._cache = {}

    def _px(self, v):
        return max(8, int(round(v * self.s)))

    def render_quota(self, ctx, groups, width=262, height=212):
        """积分包列表：每个包一行名称+剩余，下面一行是它的到期时间

        按到期时间升序，最多列 6 组（再多也看不完）。
        """
        W, H = self._px(width), self._px(height)
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        accent = ctx.get("accent", (90, 150, 240))
        d.rounded_rectangle([0, 0, W - 1, H - 1], radius=self._px(14),
                            fill=(16, 21, 38, 238),
                            outline=tuple(accent) + (170,), width=max(1, self._px(1.2)))

        pad = self._px(14)
        f_t = font(self._px(9.5), bold=False)
        f_v = font(self._px(13), bold=True)
        f_h = font(self._px(19), bold=True)
        f_s = font(self._px(8.5), bold=False)

        heading = ("未同步 · " + ctx.get("quota_error", "请稍后重试")) if ctx.get("quota_status") == "未同步" else "积分明细 · 按到期排序"
        d.text((pad, pad - self._px(1)), heading[:26],
               font=f_t, fill=(136, 152, 190, 255))

        y = pad + self._px(15)
        total = ctx.get("quota_left")
        total_txt = "—" if total is None else "{:,.2f}".format(total)
        d.text((pad, y), total_txt, font=f_h, fill=(234, 242, 255, 255))
        d.text((pad + text_size(d, total_txt, f_h)[0] + self._px(7), y + self._px(6)),
               "总剩余积分", font=f_t, fill=(118, 132, 168, 255))

        y += self._px(27)
        d.line([pad, y, W - pad, y], fill=(40, 50, 84, 255))
        y += self._px(7)

        if not groups:
            d.text((pad, y), "暂无可用积分包，可右键刷新积分",
                   font=f_t, fill=(120, 134, 168, 255))
        else:
            for g in groups[:6]:
                name = g.get("name") or "积分"
                # 明细里名称形如「平台奖励积分 · 注册积分奖励」，前缀都一样，
                # 只留来源部分才看得清是哪个包
                if " · " in name:
                    name = name.split(" · ", 1)[1]
                name = name[:14]
                left = g.get("left")
                exp = g.get("expire") or "无到期时间"
                d.text((pad, y), name, font=f_t, fill=(168, 182, 214, 255))
                val = "—" if left is None else "{:,.2f}".format(left)
                d.text((W - pad - text_size(d, val, f_v)[0], y - self._px(3)), val,
                       font=f_v, fill=(255, 214, 140, 255))
                y += self._px(13)
                d.text((pad, y), exp, font=f_s, fill=(110, 126, 162, 255))
                y += self._px(15)

        st = ctx.get("stats")
        if st and st.has_data:
            foot = "今日 %s · 本轮 %s" % (_fmt_money(st.today_credit), _fmt_money(st.turn_credit))
            d.text((pad, H - pad - self._px(11)), foot,
                   font=f_s, fill=(118, 132, 168, 255))
        return img

    def render(self, ctx, width=252, height=146):
        s = self.s
        W, H = self._px(width), self._px(height)
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        accent = ctx.get("accent", (90, 150, 240))
        d.rounded_rectangle([0, 0, W - 1, H - 1], radius=self._px(14),
                            fill=(16, 21, 38, 236),
                            outline=tuple(accent) + (170,), width=max(1, self._px(1.2)))
        pad = self._px(13)
        f_t = font(self._px(10), bold=False)
        f_v = font(self._px(12.5), bold=True)
        f_h = font(self._px(17), bold=True)

        d.text((pad, pad - self._px(2)), "DeepSeek 鲸鱼娘 · WorkBuddy",
               font=f_t, fill=(140, 158, 196, 255))
        y = pad + self._px(15)
        period = ctx.get("period", "--")
        col = (150, 200, 255)
        d.text((pad, y), period, font=f_h, fill=col + (255,))
        sub = ctx.get("period_sub", "")
        if sub:
            tw = text_size(d, period, f_h)[0]
            d.text((pad + tw + self._px(8), y + self._px(5)), sub,
                   font=f_t, fill=(122, 136, 172, 255))
        y += self._px(24)

        rows = ctx.get("card_rows") or []
        for label, value in rows:
            d.text((pad, y), label, font=f_t, fill=(138, 155, 192, 255))
            d.text((W - pad - text_size(d, value, f_v)[0], y - self._px(2)), value,
                   font=f_v, fill=(232, 240, 255, 255))
            y += self._px(17)

        # 速度曲线
        hist = ctx.get("history") or []
        if hist:
            cy0 = y + self._px(3)
            cy1 = H - self._px(12)
            x0, x1 = pad, W - pad
            d.line([x0, cy1, x1, cy1], fill=(40, 50, 84, 255))
            vals = [v for _, v in hist][-60:]
            mx = max(vals) or 1
            n = len(vals)
            if n > 1:
                stp = (x1 - x0) / (n - 1)
                pts = []
                for i, v in enumerate(vals):
                    pts.append((x0 + i * stp,
                                cy1 - (v / mx) * max(4, cy1 - cy0) * 0.94))
                d.line(pts, fill=tuple(accent) + (255,), width=max(1, self._px(1.4)),
                       joint="curve")
            d.text((x1 - text_size(d, "峰值%s" % _fmt_tokens(mx), f_t)[0],
                    cy0 - self._px(1)), "峰值%s" % _fmt_tokens(mx),
                   font=f_t, fill=(110, 126, 162, 255))
        return img


class BubbleRenderer:
    """椭圆思考泡：参考小鲸娘的留白、蓝色描边与椭圆尾泡。

    使用原创连续贝塞尔轮廓，三倍采样后缩小，文字保留两位小数。
    """
    PAD = 4
    BODY_H = 146
    DOTS_H = 26

    def __init__(self, scale=1.0):
        self.s = scale
        self._cache = {}

    def _px(self, v):
        return max(1, int(round(v * self.s)))

    def body_height(self):
        return self._px(self.PAD * 2 + self.BODY_H)

    def dots_height(self):
        return self._px(self.DOTS_H)

    def height(self):
        return self.body_height() + self.dots_height()

    def render_body(self, ctx):
        import time
        left = ctx.get("quota_left")
        exp_left = ctx.get("quota_expire_left")
        expire = ctx.get("quota_expire") or ""
        today = float(getattr(ctx.get("stats"), "today_credit", 0.0) or 0.0)
        status = ctx.get("quota_status", "未同步")
        synced = ctx.get("quota_synced_at", 0)
        sig = (left, exp_left, expire, round(today, 2), status, synced, self.s)
        hit = self._cache.get("body")
        if hit and hit[0] == sig:
            return hit[1]
        aa = 3
        k = self.s * aa
        W, H = self._px(208) * aa, self.body_height() * aa
        img = Image.new("RGBA", (W, H))
        mask = Image.new("L", (W, H))
        # 一个圆润的大泡加左下方的小鼓包；弧线相切，避免拼圆造成尖角。
        curves = [((201, 68), (201, 31), (160, 8), (108, 8)),
                  ((108, 8), (54, 8), (10, 29), (10, 68)),
                  ((10, 68), (10, 99), (31, 117), (62, 125)),
                  ((62, 125), (59, 139), (80, 144), (91, 131)),
                  ((91, 131), (148, 139), (202, 113), (201, 68))]
        points = []
        for curve in curves:
            for i in range(49):
                t = i / 48
                coeff = ((1-t)**3, 3*(1-t)**2*t, 3*(1-t)*t*t, t**3)
                points.append(tuple(sum(c*p[j] for c,p in zip(coeff, curve))*k for j in (0,1)))
        ImageDraw.Draw(mask).polygon(points, fill=255)
        shadow = Image.new("RGBA", (W,H), (0,0,0,0))
        shadow.paste((48,65,115,26), (0,0,W,H), mask.filter(ImageFilter.GaussianBlur(2*k)))
        img.alpha_composite(shadow, (0, round(2*k)))
        gradient = Image.new("RGBA", (1,H))
        gradient.putdata([(round(255-10*y/H),round(255-8*y/H),255,252) for y in range(H)])
        img.paste(gradient.resize((W,H)), (0,0), mask)
        d = ImageDraw.Draw(img)
        d.line(points + points[:1], fill=(63,82,142,255), width=max(1,round(1.6*k)), joint="curve")
        def text(y, value, size, color, bold=False):
            f = font(max(1,round(size*k)), bold)
            while text_size(d,value,f)[0] > 154*k and size > 6:
                size -= .5
                f = font(max(1,round(size*k)), bold)
            d.text((106*k,y*k),value,font=f,fill=color,anchor="mm")
        blue, muted = (48,69,128,255), (137,151,181,255)
        text(28,"剩余积分",9,muted)
        text(47,"—" if left is None else "{:,.2f}".format(left),23,blue,True)
        text(71,"近期剩余  " + ("—" if exp_left is None else "{:,.2f}".format(exp_left)),10,(88,110,157,255))
        text(87,expire or "暂无到期信息",8,(153,164,189,255))
        d.rounded_rectangle((58*k,98*k,154*k,115*k),radius=8*k,fill=(239,244,254,255))
        text(106,"今日消耗  %.2f" % today,9,(105,125,164,255))
        stamp = time.strftime("%H:%M:%S",time.localtime(synced)) if synced else ""
        text(122,("已同步 " + stamp) if status == "已同步" else status,7,
             (109,159,158,255) if status == "已同步" else (185,143,107,255))
        img = img.resize((W//aa,H//aa),Image.Resampling.LANCZOS)
        self._cache["body"] = (sig,img)
        return img

    def render_dots(self, width, phase=0.0):
        key = (width, round(phase,4))
        if key in self._cache:
            return self._cache[key]
        aa, k = 3, self.s * 3
        img = Image.new("RGBA", (width*aa,self.dots_height()*aa))
        d = ImageDraw.Draw(img)
        for i,(x,y,rx,ry) in enumerate(((87,4,6,4.2),(97,16,3.6,2.6))):
            pulse = 1 + .06*math.sin(phase*math.pi*2-i*.8)
            x,y,rx,ry = x*k,y*k,rx*k*pulse,ry*k*pulse
            d.ellipse((x-rx,y-ry,x+rx,y+ry),fill=(252,253,255,255),
                      outline=(63,82,142,255),width=max(1,round(1.1*k)))
        img = img.resize((width,self.dots_height()),Image.Resampling.LANCZOS)
        if len(self._cache)>32:
            self._cache.clear()
        self._cache[key] = img
        return img


def _fmt_int(n):
    """千分位整数；小于 1 万直接显示，便于一眼读数"""
    try:
        return "{:,}".format(int(round(float(n))))
    except (TypeError, ValueError):
        return "--"


if __name__ == "__main__":
    import usage_reader
    t = usage_reader.UsageTracker()
    st = t.poll()
    ctx = {"stats": st, "period": "梁文谷", "switch_hours": 2.3,
           "cpu": 13.0, "mem": 61.0, "clock": "09:52"}
    br = BarRenderer(1.4)
    bar = br.render(ctx, [["period"], ["speed", "cache", "cost"]], 246, (90, 150, 240))
    cr = CardRenderer(1.4)
    card = cr.render({**ctx, "period_sub": "周末全场平峰价",
                      "accent": (90, 150, 240),
                      "card_rows": [("输出速率", "%.1f tok/s" % st.out_rate),
                                    ("会话累计", "%s in" % _fmt_tokens(st.session_in)),
                                    ("折算花费", "¥%.4f" % st.session_cost)],
                      "history": st.history})
    out = Image.new("RGBA", (max(bar.width, card.width) + 20,
                             bar.height + card.height + 30), (100, 115, 140, 255))
    out.alpha_composite(bar, (10, 10))
    out.alpha_composite(card, (10, bar.height + 20))
    dst = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "ref", "bar_preview.png")
    os.makedirs(os.path.dirname(dst), exist_ok=True)   # ref/ 不入库，首次运行要自建
    out.convert("RGB").save(dst)
    print("ok", bar.size, card.size, "->", dst)
