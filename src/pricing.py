# -*- coding: utf-8 -*-
"""pricing.py —— DeepSeek API 峰谷定价与「梁文峰 / 梁文谷」时段判定

时段规则（北京时间 UTC+8）：
    高峰时段 = 工作日（周一~周五，不含法定节假日）09:00-12:00 与 14:00-18:00
    空闲时段 = 其余全部（夜间、午休 12:00-14:00、周末、法定节假日）
    高峰价 = 空闲价 x 2

梗（用户指定）：
    梁文峰 = 高峰时段，价格翻倍，「梁文峰时间」钱包在滴血
    梁文谷 = 空闲时段，价格便宜，「梁文谷时间」可以放心造

============================================================================
价格是**会变的**，而且变过好几次，所以这里用「按生效时刻分段的表」而不是一张
静态价目表 —— 折算历史花费时必须用「那次调用当时的价格」，不能拿今天的价格
去乘历史用量。

已收录的变动（由官方价格页与官方公告核实）：
    2026-08-17  开始峰谷计费（此前是全天一口价），同时大幅涨价（最高 12 倍）
    2026-09-10 12:00  flash 降价：缓存命中 0.05→0.02、未命中 1.50→1.00、
                      输出 4.50→4.00（空闲价；高峰价同步减半关系不变）
    2026-09-14 12:00  deepseek-v4-pro 的请求全部路由到 V4.1 Flash，
                      并按 Flash 单价计费（即 Pro 不再贵 3 倍）

⚠️ 已知不足：2026-08-17 之前的价格未收录（涨价前与现在差异过大，且那段窗口
   在本机日志里只有 1~2 天）。落在该窗口的调用会用最早一档价格估算，偏保守。

来源：
    https://api-docs.deepseek.com/zh-cn/quick_start/pricing
    https://api-docs.deepseek.com/zh-cn/news/news260910
"""

from __future__ import annotations

import datetime as _dt
import json
import os

TZ = _dt.timezone(_dt.timedelta(hours=8), "Asia/Shanghai")


def _t(y, m, d, hh=0, mm=0):
    return _dt.datetime(y, m, d, hh, mm, tzinfo=TZ)


# 空闲时段单价（元 / 1M tokens）；高峰 = 空闲 x 2
#   cache_hit  = 输入 · 缓存命中
#   cache_miss = 输入 · 缓存未命中
#   output     = 输出
PRICE_EPOCHS = [
    (_t(2026, 8, 17),
     "峰谷计费开始 + 涨价后的价格",
     {
         "deepseek-flash":  {"cache_hit": 0.05, "cache_miss": 1.50, "output": 4.50},
         "deepseek-v4-pro": {"cache_hit": 0.15, "cache_miss": 4.50, "output": 13.50},
     }),
    (_t(2026, 9, 10, 12, 0),
     "flash 降价（缓存命中降 60%）",
     {
         "deepseek-flash":  {"cache_hit": 0.02, "cache_miss": 1.00, "output": 4.00},
         "deepseek-v4-pro": {"cache_hit": 0.15, "cache_miss": 4.50, "output": 13.50},
     }),
    (_t(2026, 9, 14, 12, 0),
     "deepseek-v4-pro 路由到 V4.1 Flash，按 Flash 单价计费",
     {
         "deepseek-flash":  {"cache_hit": 0.02, "cache_miss": 1.00, "output": 4.00},
         "deepseek-v4-pro": {"cache_hit": 0.02, "cache_miss": 1.00, "output": 4.00},
     }),
]

# 峰谷计费开始的时刻；此前是全天一口价，不加倍
PEAK_BILLING_FROM = _t(2026, 8, 17)

# 当前生效的价格表（= 最新一档），保留这个名字方便外部引用
PRICES = PRICE_EPOCHS[-1][2]

# 旧模型名仍可调用，按 Flash 计费
ALIASES = {
    "deepseek-v4-flash": "deepseek-flash",
    "deepseek-v4-flash-vision-exp": "deepseek-flash",
    "deepseek-chat": "deepseek-flash",
    "deepseek-reasoner": "deepseek-flash",
    "deepseek-v4.1-flash": "deepseek-flash",
}
DEFAULT_MODEL = "deepseek-flash"

# 高峰时段（本地时间小时，左闭右开）
PEAK_WINDOWS = ((9, 12), (14, 18))
PEAK_WEEKDAYS = (0, 1, 2, 3, 4)          # 周一=0 .. 周五=4

# 法定节假日（"YYYY-MM-DD"）。默认留空 —— 官方表述是「周一至周五（不含法定节假日）」，
# 周末自动按空闲处理；如需把调休/法定假日也算成空闲，把日期补进这里即可。
HOLIDAYS: set[str] = set()

_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pricing.json")


def _load_config():
    global PRICES, PEAK_WINDOWS, HOLIDAYS, DEFAULT_MODEL, ALIASES, PEAK_BILLING_FROM
    try:
        with open(_CONFIG, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return
    if isinstance(cfg.get("prices"), dict):
        # 覆盖「当前生效」那一档，同时写回 PRICE_EPOCHS，保持历史折算一致
        cur = PRICE_EPOCHS[-1][2]
        for k, v in cfg["prices"].items():
            if isinstance(v, dict):
                cur[k] = {**cur.get(k, {}), **v}
        PRICES = cur
    if isinstance(cfg.get("peak_windows"), list) and cfg["peak_windows"]:
        PEAK_WINDOWS = tuple((int(a), int(b)) for a, b in cfg["peak_windows"])
    if isinstance(cfg.get("holidays"), list):
        HOLIDAYS = set(str(d) for d in cfg["holidays"])
    if cfg.get("default_model"):
        DEFAULT_MODEL = str(cfg["default_model"])
    if cfg.get("peak_billing_from"):
        try:
            PEAK_BILLING_FROM = _dt.datetime.fromisoformat(str(cfg["peak_billing_from"]))
            if PEAK_BILLING_FROM.tzinfo is None:
                PEAK_BILLING_FROM = PEAK_BILLING_FROM.replace(tzinfo=TZ)
        except Exception:
            pass
    if isinstance(cfg.get("aliases"), dict):
        ALIASES = {**ALIASES, **{str(k): str(v) for k, v in cfg["aliases"].items()}}


_load_config()


def canon_model(model: str | None) -> str:
    m = (model or "").strip().lower()
    m = ALIASES.get(m, m)
    return m if m in PRICES else DEFAULT_MODEL


def epoch_for(dt: _dt.datetime | None = None):
    """返回 (生效时刻, 说明, 价格表)：dt 当时生效的那一档

    dt 早于最早一档时返回最早一档（并可由调用方知道它早于 PEAK_BILLING_FROM）。
    """
    dt = dt or now_local()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    chosen = PRICE_EPOCHS[0]
    for ep in PRICE_EPOCHS:
        if dt >= ep[0]:
            chosen = ep
        else:
            break
    return chosen


def is_price_uncertain(dt: _dt.datetime | None = None) -> bool:
    """该时刻的价格是否属于「已知不准」的窗口（峰谷计费开始之前）"""
    dt = dt or now_local()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt < PEAK_BILLING_FROM


# ------------------------------------------------ 时段判定
def now_local() -> _dt.datetime:
    return _dt.datetime.now(TZ)


def is_peak(dt: _dt.datetime | None = None) -> bool:
    """高峰（梁文峰）= 峰谷计费已生效 + 工作日 + 落在 PEAK_WINDOWS 内"""
    dt = dt or now_local()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    if dt < PEAK_BILLING_FROM:      # 峰谷计费之前是全天一口价
        return False
    if dt.weekday() not in PEAK_WEEKDAYS:
        return False
    if dt.strftime("%Y-%m-%d") in HOLIDAYS:
        return False
    h = dt.hour
    return any(a <= h < b for a, b in PEAK_WINDOWS)


def period_name(dt: _dt.datetime | None = None) -> str:
    return "梁文峰" if is_peak(dt) else "梁文谷"


def period_label(dt: _dt.datetime | None = None) -> str:
    """给桌宠用的一句话描述"""
    dt = dt or now_local()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    if dt < PEAK_BILLING_FROM:
        return "峰谷计费之前的时段（本表无价格记录）"
    if is_peak(dt):
        return "梁文峰时间 · 高峰双倍价，省着点用"
    if dt.weekday() >= 5:
        return "梁文谷时间 · 周末全场平峰价"
    h = dt.hour
    if 0 <= h < 9:
        return "梁文谷时间 · 夜间低谷，随便造"
    if 12 <= h < 14:
        return "梁文谷时间 · 午休平峰窗口"
    if 18 <= h < 24:
        return "梁文谷时间 · 晚间平峰，放心跑批"
    return "梁文谷时间 · 平峰价"


def next_switch(dt: _dt.datetime | None = None) -> tuple[str, float]:
    """返回 (下一个时段的名字, 距离切换的小时数)"""
    dt = dt or now_local()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    cur_peak = is_peak(dt)
    # 以 10 秒为步长向前找第一个状态翻转点（最多找 4 天）
    step = _dt.timedelta(seconds=10)
    t = dt
    limit = dt + _dt.timedelta(days=4)
    while t < limit:
        t = t + step
        if is_peak(t) != cur_peak:
            return ("梁文峰" if not cur_peak else "梁文谷"), (t - dt).total_seconds() / 3600.0
    return ("梁文谷", 0.0)


def times_until_offpeak(dt: _dt.datetime | None = None) -> tuple[str, float] | None:
    """如果现在是高峰，返回 (平峰名, 剩余小时)；否则 None"""
    if not is_peak(dt):
        return None
    name, hrs = next_switch(dt)
    return (name, hrs)


# ------------------------------------------------ 费用折算
def unit_price(model: str, key: str, dt: _dt.datetime | None = None) -> float:
    """单次调用当时的单价（元/1M tokens，已含高峰倍率）"""
    dt = dt or now_local()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    table = epoch_for(dt)[2]
    p = table.get(canon_model(model)) or table.get(DEFAULT_MODEL) or {}
    base = p.get(key, 0.0)
    return base * (2.0 if is_peak(dt) else 1.0)


def cost_parts(uncached_in: int, cached_in: int, out_tokens: int,
               model: str = DEFAULT_MODEL, dt: _dt.datetime | None = None) -> float:
    """按「输入未命中 / 输入命中缓存 / 输出」三档计价（元）

    uncached_in 与 cached_in 是**分开**的两部分，不要传「含缓存的输入总量」——
    DeepSeek 的缓存命中价与未命中价差 50 倍，混在一起会算错一个数量级。
    """
    p_hit = unit_price(model, "cache_hit", dt)
    p_mis = unit_price(model, "cache_miss", dt)
    p_out = unit_price(model, "output", dt)
    return (int(uncached_in) * p_mis + int(cached_in) * p_hit + int(out_tokens) * p_out) / 1_000_000.0


def cost_cny(in_tokens: int, out_tokens: int, cached_tokens: int = 0,
             model: str = DEFAULT_MODEL, dt: _dt.datetime | None = None) -> float:
    """兼容旧签名：in_tokens 视为**输入总量**（含缓存命中部分）

    行为与旧版一致：未命中 = max(0, in_tokens - cached_tokens)，cached 按传入值计。
    新代码请直接用 cost_parts()（未命中与命中分开传，语义更清楚）。
    """
    total_in = max(0, int(in_tokens))
    cached = max(0, int(cached_tokens))
    return cost_parts(max(0, total_in - cached), cached, out_tokens, model, dt)


def price_table(dt: _dt.datetime | None = None, model: str = DEFAULT_MODEL) -> dict:
    m = canon_model(model)
    ep = epoch_for(dt)
    return {
        "model": m,
        "peak": is_peak(dt),
        "cache_hit": unit_price(m, "cache_hit", dt),
        "cache_miss": unit_price(m, "cache_miss", dt),
        "output": unit_price(m, "output", dt),
        "epoch": ep[0].strftime("%Y-%m-%d %H:%M"),
        "epoch_note": ep[1],
        "uncertain": is_price_uncertain(dt),
    }


def model_label(model: str | None) -> str:
    m = canon_model(model)
    return {"deepseek-flash": "V4.1-Flash", "deepseek-v4-pro": "V4-Pro"}.get(m, m)


if __name__ == "__main__":
    n = now_local()
    print("现在:", n.strftime("%Y-%m-%d %H:%M:%S"), "周", "一二三四五六日"[n.weekday()])
    print("时段:", period_label(n), "|", period_name(n))
    nm, hrs = next_switch(n)
    print("下一个时段:", nm, "还有 %.2f 小时" % hrs)
    t = price_table(n)
    print("当前价格: 命中 %.2f / 未命中 %.2f / 输出 %.2f 元每百万"
          % (t["cache_hit"], t["cache_miss"], t["output"]))
    print("价格档:", t["epoch"], "|", t["epoch_note"])

    # 自检
    t1 = _t(2026, 9, 15, 10, 0)
    t2 = _t(2026, 9, 19, 10, 0)
    assert is_peak(t1) is True, "工作日 10:00 应为高峰"
    assert is_peak(t2) is False, "周六 10:00 应为平峰"
    assert is_peak(_t(2026, 9, 15, 13, 0)) is False
    assert is_peak(_t(2026, 9, 15, 19, 0)) is False
    # 峰谷计费之前不加倍
    assert is_peak(_t(2026, 8, 10, 10, 0)) is False, "峰谷计费之前不应有高峰"
    # 9/10 前后 flash 缓存命中：0.05 -> 0.02（用 20:00 避开高峰倍率，纯验分档）
    assert unit_price("deepseek-flash", "cache_hit", _t(2026, 9, 9, 20, 0)) == 0.05
    assert unit_price("deepseek-flash", "cache_hit", _t(2026, 9, 11, 20, 0)) == 0.02
    # 9/14 前后 pro 的未命中输入：4.50 -> 1.00（路由到 flash 计费）
    assert unit_price("deepseek-v4-pro", "cache_miss", _t(2026, 9, 13, 20, 0)) == 4.50
    assert unit_price("deepseek-v4-pro", "cache_miss", _t(2026, 9, 15, 20, 0)) == 1.00
    # 高峰翻倍（9/10 后的高峰 flash 命中 = 0.02 x 2 = 0.04）
    assert abs(unit_price("deepseek-flash", "cache_hit", t1) - 0.04) < 1e-9
    # 三档计价：1M 未命中输入，高峰前的平峰 = 1.00 元
    assert abs(cost_cny(1_000_000, 0, 0, "deepseek-v4-flash", t2) - 1.0) < 1e-9
    print("pricing self-test OK")
