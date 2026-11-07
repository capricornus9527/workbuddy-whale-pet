# -*- coding: utf-8 -*-
"""usage_reader.py —— 从 DeepSeek Harness (DSH) 会话日志里读真实 token 用量

数据来源（本机实测确认）：
    ~/.dsh/sessions/<项目目录>/<sessionId>/session.jsonl.zstd
    ~/.dsh/sessions/<项目目录>/<sessionId>/session.v3.jsonl.zstd

    这两个文件是 **zstd 压缩**的 JSONL（每行一个 JSON），两个版本的 schema
    对本模块关心的部分**完全一致**：
        {"type":"assistant/message", "seq":N, "time":<epoch 毫秒>,
         "data":{"turn":T, "step":S,
                 "message":{"source":{"model":"deepseek-flash"}},
                 "usage":{"inputTokens":U, "outputTokens":O,
                          "cacheReadTokens":C, "reasoningTokens":R}}}
    新格式（v3）不再输出 assistant/chunk / reasoning-chunks / text-chunks，
    只留 assistant/message，所以「一次模型响应 = 一行」可以直接计数。

⚠️ 语义陷阱（和 WorkBuddy 的日志不一样，很容易搞错）：
    DSH 的 usage.inputTokens 是**未命中缓存**的那部分输入，
    命中缓存的输入单独放在 usage.cacheReadTokens。
    而 WorkBuddy 的 inputTokens 是**含缓存**的输入总量。
    DeepSeek 缓存命中价与未命中价差 50 倍，把两者混用会让花费算错一个数量级。

本模块做三件事：
    1. 找到「当前正在写入」的会话日志（mtime 最新的 session*.jsonl.zstd）
    2. 全量解压解析出每一次模型调用的用量与当时用的模型
    3. 算出桌宠要展示的指标：输出速率、消耗速率、缓存命中率、折算花费……

关于「增量」：一开始想按 byte offset 增量解压，但 DSH 写的是多个 zstd 帧拼在
一起的流，Python 的 decompressobj 跨帧会报 "cannot use a decompressobj
multiple times"，而保持 stream_reader 打开又依赖写入时机。
实测全量解压 + 解析 2.8 MB 只要 **0.04 秒**，所以改成「文件大小变了才重扫」：
空闲时只做一次 os.path.getsize（零开销），有新增才重扫一遍，简单且不会漏。

刻意做了防错：日志可能被重写/截断、可能写到一半（半行 JSON）、文件可能正在被
别的进程占用 —— 全部静默降级，不抛异常。
"""

from __future__ import annotations

import datetime as _dt
import glob
import json
import os
import threading
import time
from collections import deque

import pricing

try:
    import zstandard as _zstd
    HAVE_ZSTD = True
except Exception:                                    # pragma: no cover
    _zstd = None
    HAVE_ZSTD = False

DEFAULT_ROOT = os.path.join(os.path.expanduser("~"), ".dsh", "sessions")

# 判定「模型正在干活」的静默阈值（秒）
ACTIVE_WINDOW = 30.0
RECENT_WINDOW = 60.0
BURN_WINDOW = 600.0

_SCAN_LIMIT_BYTES = 200 * 1024 * 1024        # 解压上限，防御性：坏文件不至于吃爆内存


class Call:
    """一次模型调用的用量记录"""

    __slots__ = ("ts", "uncached_in", "out_tokens", "cached", "reasoning",
                 "model", "key", "turn", "step")

    def __init__(self, ts, uncached_in, out_tokens, cached, reasoning,
                 model, key="", turn=0, step=0):
        self.ts = ts
        self.uncached_in = uncached_in
        self.out_tokens = out_tokens
        self.cached = cached
        self.reasoning = reasoning
        self.model = model
        self.key = key
        self.turn = turn
        self.step = step

    @property
    def in_tokens(self):
        """输入总量（未命中 + 命中）"""
        return self.uncached_in + self.cached

    @property
    def total(self):
        return self.in_tokens + self.out_tokens


class Stats:
    def __init__(self):
        self.has_data = False
        self.session_file = ""
        self.session_title = ""
        self.model = pricing.DEFAULT_MODEL
        self.calls_total = 0
        self.session_calls = 0
        self.session_in = 0           # 输入总量（含缓存命中）
        self.session_uncached = 0     # 输入 · 未命中
        self.session_out = 0
        self.session_cached = 0
        self.session_reasoning = 0
        self.session_cost = 0.0
        self.last_call_ts = 0.0
        self.last_out = 0
        self.last_reasoning = 0
        self.out_rate = 0.0          # tok/s，近 60 秒
        self.burn_rate = 0.0         # tok/min，近 60 秒（输入+输出）
        self.cost_per_hour = 0.0     # ¥/小时，近 10 分钟外推
        self.cache_hit_ratio = 0.0
        self.active = False
        self.idle_seconds = 9999.0
        self.history = []            # [(ts, out_tokens)] 最近若干次调用，供画速度曲线
        self.session_started = 0.0
        self.price_uncertain = False  # 是否含「价格未收录」时段的调用
        self.error = ""


class UsageTracker:
    """DSH 会话用量追踪器（每次 poll 做一次 stat，有变化才重新解压解析）"""

    def __init__(self, root=None, poll_interval=2.0):
        self.root = root or os.environ.get("DSH_SESSIONS_ROOT") or DEFAULT_ROOT
        self.poll_interval = poll_interval
        self._path = None
        self._size = -1
        self._mtime = -1.0
        self._calls = []
        self._lock = threading.Lock()
        self._stats = Stats()

    # ---------------------------------------------------------------- 文件定位
    def find_session_file(self):
        """返回 mtime 最新的会话日志（= 当前正在写入的那个）"""
        pats = [os.path.join(self.root, "*", "*", "session*.jsonl.zstd"),
                os.path.join(self.root, "*.jsonl.zstd")]
        best, best_m = None, -1.0
        for p in pats:
            for f in glob.glob(p):
                try:
                    m = os.path.getmtime(f)
                except OSError:
                    continue
                if m > best_m:
                    best, best_m = f, m
        return best

    # ---------------------------------------------------------------- 解析
    @staticmethod
    def _extract(raw):
        """从一行里抽出一次模型调用；抽不到返回 None

        raw 是 bytes（也接受 str，便于测试直接喂字符串）。
        先用子串预筛，避免对每一行都 json.loads。
        """
        if isinstance(raw, str):
            raw = raw.encode("utf-8", "ignore")
        if b'"assistant/message"' not in raw:
            return None
        try:
            d = json.loads(raw.decode("utf-8", "ignore"))
        except Exception:
            return None
        if not isinstance(d, dict) or d.get("type") != "assistant/message":
            return None
        data = d.get("data")
        if not isinstance(data, dict):
            return None
        u = data.get("usage")
        if not isinstance(u, dict):
            return None

        ts = d.get("time")
        if not isinstance(ts, (int, float)):
            return None

        msg = data.get("message") if isinstance(data.get("message"), dict) else {}
        src = msg.get("source") if isinstance(msg.get("source"), dict) else {}
        model = src.get("model")
        if not isinstance(model, str):
            model = None

        uncached = u.get("inputTokens", 0) or 0
        cached = u.get("cacheReadTokens", 0) or 0
        out = u.get("outputTokens", 0) or 0
        reasoning = u.get("reasoningTokens", 0) or 0
        if not uncached and not cached and not out:
            return None

        key = d.get("seq")
        if key is None:
            key = msg.get("id")
        return Call(ts / 1000.0, int(uncached), int(out), int(cached), int(reasoning),
                    model, str(key) if key is not None else "",
                    int(data.get("turn") or 0), int(data.get("step") or 0))

    def _scan(self, path):
        """全量解压 + 解析，返回 (calls, title, truncated)"""
        calls = []
        title = ""
        truncated = False
        d = _zstd.ZstdDecompressor()
        total = 0
        with open(path, "rb") as fh, d.stream_reader(fh) as r:
            buf = b""
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                total += len(chunk)
                if total > _SCAN_LIMIT_BYTES:
                    truncated = True
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    if b'"assistant/message"' in line:
                        c = self._extract(line)
                        if c:
                            calls.append(c)
                    elif b'"session/title"' in line and not title:
                        try:
                            o = json.loads(line.decode("utf-8", "ignore"))
                            v = (o.get("data") or {}).get("title") \
                                or (o.get("data") or {}).get("val")
                            if isinstance(v, str):
                                title = v
                        except Exception:
                            pass
            if buf.strip():
                c = self._extract(buf)
                if c:
                    calls.append(c)
        # 按 seq 去重（同一次响应理论上只出现一次，防御性处理）
        seen = set()
        uniq = []
        for c in calls:
            if c.key:
                if c.key in seen:
                    continue
                seen.add(c.key)
            uniq.append(c)
        return uniq, title, truncated

    # ---------------------------------------------------------------- 采样
    def poll(self):
        path = self.find_session_file()
        if path != self._path:
            self._path = path
            self._size, self._mtime = -1, -1.0
            self._calls = []

        if path:
            try:
                size = os.path.getsize(path)
                mtime = os.path.getmtime(path)
            except OSError:
                size, mtime = -1, -1.0
            # 只有文件长个了 / 被重写了才重新解压（空闲时只花一次 stat）
            if size != self._size or mtime != self._mtime:
                if HAVE_ZSTD:
                    try:
                        calls, title, truncated = self._scan(path)
                        self._calls = calls
                        self._title = title
                        self._truncated = truncated
                        self._size, self._mtime = size, mtime
                    except Exception as e:
                        self._size, self._mtime = size, mtime
                        self._scan_error = "读取会话日志失败: %s" % e
                else:
                    self._size, self._mtime = size, mtime

        with self._lock:
            self._stats = self._build()
        return self._stats

    # 供 _build 读取的可选属性
    _title = ""
    _truncated = False
    _scan_error = ""

    def _build(self):
        st = Stats()
        st.session_file = self._path or ""
        st.session_title = getattr(self, "_title", "")
        if not HAVE_ZSTD:
            st.error = ("缺少 zstandard 库，读不了 .jsonl.zstd 会话日志。"
                        "请运行 setup.bat，或 pip install zstandard")
            return st
        if getattr(self, "_scan_error", ""):
            st.error = self._scan_error
        calls = list(self._calls)
        if not calls:
            return st
        st.has_data = True
        now = time.time()
        calls.sort(key=lambda c: c.ts)
        st.calls_total = len(calls)

        st.session_calls = len(calls)
        st.session_uncached = sum(c.uncached_in for c in calls)
        st.session_cached = sum(c.cached for c in calls)
        st.session_in = st.session_uncached + st.session_cached
        st.session_out = sum(c.out_tokens for c in calls)
        st.session_reasoning = sum(c.reasoning for c in calls)
        st.last_call_ts = calls[-1].ts
        st.last_out = calls[-1].out_tokens
        st.last_reasoning = calls[-1].reasoning
        st.model = calls[-1].model or pricing.DEFAULT_MODEL
        st.session_started = calls[0].ts

        # 花费按「每次调用发生当时生效的价格 + 当时的峰谷时段」分别计费
        # （历史用量不会因为后来涨价/降价而变贵或变便宜）
        cost = 0.0
        uncertain = False
        for c in calls:
            dt = _dt.datetime.fromtimestamp(c.ts, pricing.TZ)
            if pricing.is_price_uncertain(dt):
                uncertain = True
            cost += pricing.cost_parts(c.uncached_in, c.cached, c.out_tokens, c.model, dt)
        st.session_cost = cost
        st.price_uncertain = uncertain

        st.cache_hit_ratio = (st.session_cached / st.session_in) if st.session_in else 0.0

        win60 = [c for c in calls if now - c.ts <= RECENT_WINDOW]
        st.out_rate = sum(c.out_tokens for c in win60) / RECENT_WINDOW
        st.burn_rate = sum(c.total for c in win60) * 60.0 / RECENT_WINDOW

        win_burn = [c for c in calls if now - c.ts <= BURN_WINDOW]
        if win_burn:
            el = max(60.0, now - win_burn[0].ts)
            cst = sum(pricing.cost_parts(
                c.uncached_in, c.cached, c.out_tokens, c.model,
                _dt.datetime.fromtimestamp(c.ts, pricing.TZ)) for c in win_burn)
            st.cost_per_hour = cst * 3600.0 / el

        st.idle_seconds = max(0.0, now - st.last_call_ts)
        st.active = st.idle_seconds <= ACTIVE_WINDOW
        st.history = [(c.ts, c.out_tokens) for c in calls[-60:]]
        return st


def fmt_tokens(n) -> str:
    n = float(n)
    if n >= 1_000_000:
        return "%.2fM" % (n / 1_000_000)
    if n >= 1_000:
        return "%.1fK" % (n / 1_000)
    return "%d" % int(n)


def fmt_money(v) -> str:
    if v >= 100:
        return "%.1f" % v
    if v >= 1:
        return "%.2f" % v
    if v >= 0.01:
        return "%.3f" % v
    return "%.4f" % v


def fmt_hm(ts) -> str:
    if not ts:
        return "--:--"
    return _dt.datetime.fromtimestamp(ts).strftime("%H:%M:%S")


if __name__ == "__main__":
    t = UsageTracker()
    s = t.poll()
    print("数据根目录:", t.root)
    print("会话文件:", s.session_file)
    if s.session_title:
        print("会话标题:", s.session_title)
    if s.error:
        print("错误:", s.error)
    print("是否有数据:", s.has_data, "| 调用次数:", s.session_calls, "| 模型:", s.model)
    print("输入:", fmt_tokens(s.session_in),
          "（其中未命中 %s / 命中 %s）" % (fmt_tokens(s.session_uncached),
                                          fmt_tokens(s.session_cached)))
    print("输出:", fmt_tokens(s.session_out),
          "| 缓存命中: %.1f%%" % (s.cache_hit_ratio * 100))
    print("折算花费: ¥%s%s" % (fmt_money(s.session_cost),
                              "（含价格未收录时段，为估算）" if s.price_uncertain else ""))
    print("输出速率: %.1f tok/s | 消耗速率: %.0f tok/min | 折算 ¥%s/h"
          % (s.out_rate, s.burn_rate, fmt_money(s.cost_per_hour)))
    print("最后调用:", fmt_hm(s.last_call_ts), "| 空闲 %.0fs" % s.idle_seconds)
    print("速度曲线样本数:", len(s.history))
