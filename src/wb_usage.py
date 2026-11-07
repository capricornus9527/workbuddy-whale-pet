# -*- coding: utf-8 -*-
"""wb_usage.py —— WorkBuddy 用量数据源

替换上游的 DSH(zstd) 读取器。数据来自两处：

1. ``<WORKBUDDY_HOME>/projects/<项目>/<会话id>.jsonl``
   每行一条会话记录，模型调用的用量在 ``providerData.usage`` /
   ``providerData.rawUsage``，一轮对话用 ``providerData.conversationRequestId``
   归组。字段口径（与 DSH 相反，务必注意）：
       prompt_tokens          输入总量，**已包含**缓存命中部分
       prompt_cache_hit_tokens  其中命中的部分
       prompt_cache_miss_tokens 未命中的部分
       completion_tokens      输出总量
       completion_tokens_details.reasoning_tokens 思维链
       credit                 该步消耗的积分

2. ``<WORKBUDDY_HOME>/workbuddy.db`` 的 ``session_usage`` 表
   宿主结算好的每轮积分 ``credit_json``（{requestId: credit}）以及
   上下文占用 ``used`` / 窗口 ``size``。这一份比逐行累加更可信。

对外接口与上游 ``usage_reader.UsageTracker`` 保持一致（``poll()`` 返回
``Stats``），因此 pet.py / barlib.py 不需要知道数据从哪来。
"""

from __future__ import annotations

import glob
import json
import os
import sqlite3
import threading
import time

WORKBUDDY_HOME = (os.environ.get("WORKBUDDY_HOME")
                  or os.path.join(os.path.expanduser("~"), ".workbuddy"))

# 判定「模型正在干活」的静默阈值（秒）
ACTIVE_WINDOW = 30.0
RECENT_WINDOW = 60.0
BURN_WINDOW = 600.0


class Call:
    """一次模型调用（一个 step）的用量记录"""

    __slots__ = ("ts", "uncached_in", "in_total", "out_tokens", "cached",
                 "reasoning", "model", "req_id", "credit")

    def __init__(self, ts, uncached_in, in_total, out_tokens, cached,
                 reasoning, model, req_id="", credit=0.0):
        self.ts = ts
        self.uncached_in = uncached_in
        self.in_total = in_total
        self.out_tokens = out_tokens
        self.cached = cached
        self.reasoning = reasoning
        self.model = model
        self.req_id = req_id
        self.credit = credit

    @property
    def in_tokens(self):
        """输入总量（未命中 + 命中）——与上游语义一致，便于复用 barlib"""
        return self.in_total

    @property
    def total(self):
        return self.in_tokens + self.out_tokens


class Stats:
    """与上游 usage_reader.Stats 属性名兼容，额外补充 WorkBuddy 口径字段"""

    def __init__(self):
        self.has_data = False
        self.session_file = ""
        self.session_id = ""
        self.project = ""
        self.model = "deepseek"
        self.calls_total = 0
        self.session_calls = 0
        self.session_in = 0
        self.session_uncached = 0
        self.session_out = 0
        self.session_cached = 0
        self.session_reasoning = 0
        self.session_cost = 0.0        # 当前会话累计积分
        self.today_credit = 0.0        # 今日累计积分
        self.turn_credit = 0.0         # 最近一轮积分
        self.turns_today = 0
        self.last_call_ts = 0.0
        self.last_out = 0
        self.last_reasoning = 0
        self.out_rate = 0.0            # tok/s，近 60 秒
        self.burn_rate = 0.0           # tok/min，近 60 秒
        self.cost_per_hour = 0.0       # 积分/小时，近 10 分钟外推
        self.cache_hit_ratio = 0.0
        self.active = False
        self.idle_seconds = 9999.0
        self.history = []              # [(ts, out_tokens)]
        self.session_started = 0.0
        self.context_used = 0          # 上下文已用 token
        self.context_size = 0          # 上下文窗口
        self.display_model = ""        # 人类可读模型名
        self.error = ""

    @property
    def context_left(self):
        if not self.context_size:
            return 0
        return max(0, self.context_size - self.context_used)

    @property
    def context_ratio(self):
        if not self.context_size:
            return 0.0
        return max(0.0, min(1.0, self.context_used / float(self.context_size)))


def _as_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _as_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


class UsageTracker:
    """WorkBuddy 用量追踪器：文件/数据库有变化才重新解析"""

    def __init__(self, root=None, poll_interval=2.0):
        self.root = root or WORKBUDDY_HOME
        self.projects_dir = os.path.join(self.root, "projects")
        self.db_path = os.path.join(self.root, "workbuddy.db")
        self.poll_interval = poll_interval
        self._lock = threading.Lock()
        self._stats = Stats()
        self._cache = {}          # path -> (size, mtime, calls)
        self._credit_cache = {}   # session_id -> (mtime, {req_id: credit}, used, size)

    # ------------------------------------------------------------ 文件定位
    def find_session_file(self):
        """返回 mtime 最新的会话 jsonl（= 当前正在写入的那个）"""
        best, best_m = None, -1.0
        for f in glob.glob(os.path.join(self.projects_dir, "*", "*.jsonl")):
            try:
                m = os.path.getmtime(f)
            except OSError:
                continue
            if m > best_m:
                best, best_m = f, m
        return best

    # ------------------------------------------------------------ 解析单行
    @staticmethod
    def _parse_line(line):
        """从一行里抽出一次模型调用；抽不到返回 None"""
        if '"rawUsage"' not in line:
            return None
        try:
            d = json.loads(line)
        except Exception:
            return None
        if not isinstance(d, dict):
            return None
        pd = d.get("providerData")
        if not isinstance(pd, dict):
            return None
        u = pd.get("usage")
        raw = pd.get("rawUsage")
        if not isinstance(u, dict) and not isinstance(raw, dict):
            return None

        ts = d.get("timestamp") or d.get("time")
        if isinstance(ts, (int, float)):
            ts = ts / 1000.0 if ts > 1e11 else float(ts)
        else:
            return None

        u = u if isinstance(u, dict) else {}
        raw = raw if isinstance(raw, dict) else {}

        in_total = _as_int(u.get("inputTokens"), _as_int(raw.get("prompt_tokens")))
        out = _as_int(u.get("outputTokens"), _as_int(raw.get("completion_tokens")))

        cached = _as_int(raw.get("prompt_cache_hit_tokens"))
        if not cached:
            cached = _as_int(raw.get("cache_read_input_tokens"))
        if not cached:
            det = raw.get("prompt_tokens_details")
            if isinstance(det, dict):
                cached = _as_int(det.get("cached_tokens"))
        if not cached:
            det = u.get("inputTokensDetails")
            if isinstance(det, list) and det and isinstance(det[0], dict):
                cached = _as_int(det[0].get("cached_tokens"))

        miss = _as_int(raw.get("prompt_cache_miss_tokens"))
        if not miss and in_total:
            miss = max(0, in_total - cached)

        reasoning = _as_int(raw.get("completion_thinking_tokens"))
        if not reasoning:
            odet = raw.get("completion_tokens_details")
            if isinstance(odet, dict):
                reasoning = _as_int(odet.get("reasoning_tokens"))
        if not reasoning:
            odet = u.get("outputTokensDetails")
            if isinstance(odet, list) and odet and isinstance(odet[0], dict):
                reasoning = _as_int(odet[0].get("reasoning_tokens"))

        model = pd.get("requestModelName") or pd.get("model") or ""
        credit = _as_float(raw.get("credit"))
        req_id = pd.get("conversationRequestId") or ""

        return Call(ts=ts, uncached_in=miss, in_total=in_total, out_tokens=out,
                    cached=cached, reasoning=reasoning, model=model,
                    req_id=req_id, credit=credit)

    # ------------------------------------------------------------ 读一个会话文件
    def _calls_of(self, path):
        try:
            st = os.stat(path)
        except OSError:
            return []
        hit = self._cache.get(path)
        if hit and hit[0] == st.st_size and hit[1] == st.st_mtime:
            return hit[2]
        calls = []
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    c = self._parse_line(line)
                    if c is not None:
                        calls.append(c)
        except OSError:
            return []
        self._cache[path] = (st.st_size, st.st_mtime, calls)
        if len(self._cache) > 40:
            self._cache.clear()
        return calls

    # ------------------------------------------------------------ 读宿主结算
    def _session_usage(self, session_id):
        """从 workbuddy.db 读该会话的上下文占用与宿主结算的每轮积分"""
        if not session_id:
            return {}, 0, 0
        try:
            db_stat = os.stat(self.db_path)
            try:
                wal_stat = os.stat(self.db_path + "-wal")
                wal_stamp = (wal_stat.st_mtime_ns, wal_stat.st_size)
            except FileNotFoundError:
                wal_stamp = None
            mtime = (db_stat.st_mtime_ns, db_stat.st_size, wal_stamp)
        except OSError:
            return {}, 0, 0
        hit = self._credit_cache.get(session_id)
        if hit and hit[0] == mtime:
            return hit[1], hit[2], hit[3]
        credits, used, size = {}, 0, 0
        try:
            uri = "file:%s?mode=ro" % self.db_path.replace("\\", "/")
            con = sqlite3.connect(uri, uri=True, timeout=2.0)
            try:
                cur = con.cursor()
                cur.execute("SELECT used, size, credit_json FROM session_usage "
                            "WHERE session_id = ?", (session_id,))
                row = cur.fetchone()
                if row:
                    used, size = _as_int(row[0]), _as_int(row[1])
                    if row[2]:
                        try:
                            raw = json.loads(row[2])
                            if isinstance(raw, dict):
                                credits = {k: _as_float(v) for k, v in raw.items()}
                        except Exception:
                            credits = {}
            finally:
                con.close()
        except Exception:
            return {}, 0, 0
        self._credit_cache[session_id] = (mtime, credits, used, size)
        return credits, used, size

    # ------------------------------------------------------------ 今日汇总
    def _today_credits(self, now, latest_path):
        """扫 24 小时内改动过的会话文件，按轮次时间汇总今日积分"""
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        total, turns = 0.0, 0
        cutoff = now - 86400
        for f in glob.glob(os.path.join(self.projects_dir, "*", "*.jsonl")):
            try:
                if os.path.getmtime(f) < cutoff:
                    continue
            except OSError:
                continue
            calls = self._calls_of(f)
            if not calls:
                continue
            sid = os.path.splitext(os.path.basename(f))[0]
            credits, _, _ = self._session_usage(sid)
            # 按轮次归组：轮次时间取该组最早的时间戳
            turns_map = {}
            for c in calls:
                key = c.req_id or ("%s-%.0f" % (sid, c.ts))
                slot = turns_map.get(key)
                if slot is None:
                    turns_map[key] = [c.ts, c.ts, 0.0]
                else:
                    slot[0] = min(slot[0], c.ts)
                    slot[1] = max(slot[1], c.ts)
                turns_map[key][2] += c.credit
            for key, (first, _last, sum_credit) in turns_map.items():
                if time.strftime("%Y-%m-%d", time.localtime(first)) != today:
                    continue
                total += credits.get(key, sum_credit)
                turns += 1
        return total, turns

    @staticmethod
    def _sid(path):
        return os.path.splitext(os.path.basename(path))[0] if path else ""

    # ------------------------------------------------------------ 主入口
    def poll(self):
        stats = Stats()
        try:
            self._poll_into(stats)
        except Exception as e:               # 任何异常都不该让桌宠挂掉
            stats.error = "%s: %s" % (type(e).__name__, e)
        self._stats = stats
        return stats

    def _poll_into(self, st):
        path = self.find_session_file()
        if not path:
            st.error = "没找到 WorkBuddy 会话记录"
            return
        now = time.time()
        calls = self._calls_of(path)
        if not calls:
            st.error = "会话记录里没有用量数据"
            return

        sid = self._sid(path)
        st.session_id = sid
        st.session_file = path
        st.project = os.path.basename(os.path.dirname(path))
        st.has_data = True
        st.calls_total = len(calls)
        st.session_calls = len(calls)
        st.session_started = calls[0].ts
        st.last_call_ts = calls[-1].ts
        st.last_out = calls[-1].out_tokens
        st.last_reasoning = calls[-1].reasoning
        st.model = calls[-1].model or "deepseek"
        st.display_model = calls[-1].model or ""

        for c in calls:
            st.session_in += c.in_total
            st.session_uncached += c.uncached_in
            st.session_cached += c.cached
            st.session_out += c.out_tokens
            st.session_reasoning += c.reasoning

        if st.session_in:
            st.cache_hit_ratio = st.session_cached / float(st.session_in)

        # 积分：宿主结算优先，逐行累加兜底
        credits, used, size = self._session_usage(sid)
        if credits:
            st.session_cost = sum(credits.values())
            last_key = calls[-1].req_id
            st.turn_credit = credits.get(last_key, 0.0)
        else:
            by_turn = {}
            for c in calls:
                by_turn[c.req_id] = by_turn.get(c.req_id, 0.0) + c.credit
            st.session_cost = sum(by_turn.values())
            st.turn_credit = by_turn.get(calls[-1].req_id, 0.0)
        st.context_used, st.context_size = used, size

        # 今日
        st.today_credit, st.turns_today = self._today_credits(now, path)

        # 速率
        recent = [c for c in calls if now - c.ts <= RECENT_WINDOW]
        if recent:
            span = max(1.0, min(RECENT_WINDOW, now - recent[0].ts))
            st.out_rate = sum(c.out_tokens for c in recent) / span
            st.burn_rate = sum(c.total for c in recent) / span * 60.0
        burn = [c for c in calls if now - c.ts <= BURN_WINDOW]
        if burn:
            span_h = max(1.0 / 60.0, min(BURN_WINDOW, now - burn[0].ts) / 3600.0)
            st.cost_per_hour = sum(c.credit for c in burn) / span_h

        st.history = [(c.ts, c.out_tokens) for c in calls[-60:]]
        st.idle_seconds = max(0.0, now - st.last_call_ts)
        st.active = st.idle_seconds <= ACTIVE_WINDOW


if __name__ == "__main__":
    t = UsageTracker()
    s = t.poll()
    print(json.dumps({
        "error": s.error, "has_data": s.has_data, "model": s.display_model,
        "session_in": s.session_in, "session_out": s.session_out,
        "cache_hit": round(s.cache_hit_ratio, 3),
        "today_credit": round(s.today_credit, 3), "turn_credit": round(s.turn_credit, 3),
        "session_credit": round(s.session_cost, 3),
        "ctx": "%d/%d" % (s.context_used, s.context_size),
        "out_rate": round(s.out_rate, 1), "active": s.active,
        "turns_today": s.turns_today,
    }, ensure_ascii=False, indent=1))
