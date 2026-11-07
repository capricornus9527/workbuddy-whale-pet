# -*- coding: utf-8 -*-
"""wb_quota.py —— 从 WorkBuddy 积分页复制的文本里抽出额度

作为 wb_balance.py 自动同步不可用时的手动回退：用户复制积分页文本，
本模块解析总额、各积分包剩余和到期时间。自动同步不依赖剪贴板。

能识别的文本形态（截取自设置页「积分概览」）：

    4158.06 总剩余积分
    套餐基础积分
    500
    平台奖励积分
    3,658.06
    ...
    平台奖励积分
    最近到期时间 2026/11/30 08:59:33
    已使用 55.94/3714 剩余98%

规则：
    总额   = 「N 总剩余积分」里的 N
    到期   = 「最近到期时间」后面的日期
    剩余   = 最后一组「已使用 A/B」的 B - A（即最近到期那组的余额）
"""

from __future__ import annotations

import re

__all__ = ["parse", "apply_text"]

_RE_TOTAL = re.compile(r"([\d][\d,]*(?:\.\d+)?)\s*总剩余积分")
_RE_TOTAL_LOOSE = re.compile(r"总剩余积分\s*([\d][\d,]*(?:\.\d+)?)")
_RE_EXPIRE = re.compile(r"最近到期时间\s*(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})")
_RE_EXPIRE_LOOSE = re.compile(r"到期时间\s*(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})")
_RE_USED = re.compile(r"已使用\s*([\d][\d,]*(?:\.\d+)?)\s*/\s*([\d][\d,]*(?:\.\d+)?)")
_RE_REWARD = re.compile(r"平台奖励积分[^\d]{0,40}?([\d][\d,]*(?:\.\d+)?)")

# 单组：剩余 N / 已使用 A/B / 到期时间（可带时分秒）
_RE_LEFT = re.compile(r"剩余\s*([\d][\d,]*(?:\.\d+)?)")
_RE_LEFT_PCT = re.compile(r"剩余\s*\d+\s*%")
_RE_USED_SLASH = re.compile(r"已使用\s*([\d][\d,]*(?:\.\d+)?)\s*/\s*([\d][\d,]*(?:\.\d+)?)")
_RE_EXPIRE_FULL = re.compile(
    r"(?:到期时间|下次刷新时间)\s*(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})"
    r"(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?")


def _norm_expire(s):
    """"2026/11/30 08:59:33" -> "2026-11-30 08:59:33"；只到日期就只留日期"""
    m = _RE_EXPIRE_FULL.search(s)
    if not m:
        return ""
    y, mo, d, hh, mm, ss = m.groups()
    out = "%s-%02d-%02d" % (y, int(mo), int(d))
    if hh is not None and mm is not None:
        out += " %02d:%02d" % (int(hh), int(mm))
        if ss is not None:
            out += ":%02d" % int(ss)
    return out


def _is_group_line(ln):
    """这一行是否标志着一个新的积分包（已使用 XX 的收尾行也算）"""
    if _RE_USED_SLASH.search(ln):
        return True
    if "已使用" in ln:
        return True
    m = _RE_LEFT.search(ln)
    return bool(m and not _RE_LEFT_PCT.search(ln))


def parse_groups(text):
    """把「积分概览 / 消耗明细」里的每个积分包抽成一组

    一组的典型形态（来自设置页）：

        平台奖励积分        剩余 10.86
        已使用89.14 · 到期时间 2026/11/30 08:59:33
        Buddy 加油站签到

    或：

        套餐基础积分
        下次刷新时间 2026/11/30 23:59:59
        已使用 0/500 剩余100%

    识别规则：
      * 「剩余 N」或「已使用 A/B」（剩余 = B - A）出现，就算一个包；
      * 名称优先取「剩余」之前的文字，否则往上找最近的短文本行；
      * 到期时间先看同行，再往前找（「下次刷新时间」常在这一组的上方），
        最后才往后找；一旦穿过另一组的起始行就停，避免串组。
    """
    if not text:
        return []
    lines = [ln.strip() for ln in text.replace("\u00a0", " ").splitlines() if ln.strip()]
    groups, seen = [], set()

    for i, ln in enumerate(lines):
        left = None
        m = _RE_LEFT.search(ln)
        if m and not _RE_LEFT_PCT.search(ln):
            left = _num(m.group(1))
        if left is None:
            m2 = _RE_USED_SLASH.search(ln)
            if m2:
                a, b = _num(m2.group(1)), _num(m2.group(2))
                if a is not None and b is not None and b >= a:
                    left = round(b - a, 2)
        if left is None:
            continue

        summary_style = ("已使用" in ln and "剩余" in ln)
        expire = _norm_expire(ln)
        if not expire:
            # 概览页的组行形如「已使用 0/500 剩余100%」，到期时间（下次刷新时间）
            # 在这一组的上方；明细页的组行是「标题 + 剩余 N」，到期时间在下一行。
            if summary_style:
                for j in range(i - 1, max(-1, i - 4), -1):
                    expire = _norm_expire(lines[j])
                    if expire:
                        break
                    if _is_group_line(lines[j]):
                        break
            else:
                for j in range(i + 1, min(i + 3, len(lines))):
                    if "剩余" in lines[j]:
                        break
                    expire = _norm_expire(lines[j])
                    if expire:
                        break

        name = ""
        head = ln.split("剩余")[0] if "剩余" in ln else ""
        head = re.sub(r"[\d,.:%\s]+$", "", head).strip()
        if head and len(head) <= 24 and not re.search(r"已使用", head):
            name = head
        if not name:
            for j in range(i - 1, max(-1, i - 5), -1):
                cand = lines[j]
                if len(cand) > 24 or re.search(r"\d", cand):
                    continue
                if any(k in cand for k in ("已使用", "剩余", "到期", "积分明细", "查看明细")):
                    continue
                name = cand
                break

        # 明细页里同名的「平台奖励积分」有很多条，靠下面那行来源区分：
        #   平台奖励积分            剩余 1496.99
        #   已使用3.01 · 到期时间 2026/11/30 20:48:52
        #   注册积分奖励          <- 这一行
        source = ""
        if not summary_style:
            for j in range(i + 1, min(i + 4, len(lines))):
                cand = lines[j]
                if "剩余" in cand:
                    break                      # 已经到下一组了
                if re.search(r"\d", cand) or "已使用" in cand or "到期" in cand:
                    continue
                if len(cand) <= 20:
                    source = cand
                    break
        if source and source != name:
            name = (name + " · " + source) if name else source

        key = (name, round(left, 2), expire)
        if key in seen:
            continue
        seen.add(key)
        groups.append({"name": name or "积分", "left": round(left, 2), "expire": expire})

    # 已经用光的组（剩余 0）不占位置，也不该当「最近到期」
    groups = [g for g in groups if g["left"] > 0]
    # 按到期时间升序；没有到期时间的排最后
    groups.sort(key=lambda g: (g["expire"] or "9999-99-99"))
    return groups


def _num(s):
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _date(m):
    y, mo, d = m.group(1), m.group(2), m.group(3)
    return "%s-%02d-%02d" % (y, int(mo), int(d))


def parse(text):
    """返回 dict，可能含 total / expire / expire_left / groups；识别不到就给空 dict"""
    out = {}
    if not text or len(text) > 20000:
        return out
    t = text.replace("\u00a0", " ").replace("\u3000", " ")

    m = _RE_TOTAL.search(t) or _RE_TOTAL_LOOSE.search(t)
    if m:
        v = _num(m.group(1))
        if v is not None:
            out["total"] = v

    groups = parse_groups(t)
    if groups:
        out["groups"] = groups
        nearest = groups[0]
        out["expire_left"] = nearest["left"]
        if nearest.get("expire"):
            # 气泡第三行直接放完整时间（精确到秒），省掉「到期时间」四个字
            out["expire"] = nearest["expire"]
        if "total" not in out:
            out["total"] = round(sum(g["left"] for g in groups), 2)

    if "expire" not in out:
        m = _RE_EXPIRE.search(t) or _RE_EXPIRE_LOOSE.search(t)
        if m:
            out["expire"] = _date(m)

    if "expire_left" not in out:
        used = _RE_USED.findall(t)
        if used:
            a, b = _num(used[-1][0]), _num(used[-1][1])
            if a is not None and b is not None and b >= a:
                out["expire_left"] = round(b - a, 2)

    # 兜底：没有「已使用」时，抓「平台奖励积分」后紧跟的数字
    if "expire_left" not in out:
        m = _RE_REWARD.search(t)
        if m:
            v = _num(m.group(1))
            if v is not None:
                out["expire_left"] = v

    return out


def apply_text(text):
    """便捷入口：有识别结果才返回，否则 None"""
    got = parse(text)
    return got or None


if __name__ == "__main__":
    sample = (
        "体验版\n轻量开启 AI 效率体验\n"
        "4158.06 总剩余积分\n"
        "套餐基础积分\n500\n平台奖励积分\n3,658.06\n"
        "已加入 Buddy One，可共享 WorkBuddy 积分，部分产品待关联\n"
        "积分明细\n套餐基础积分\n下次刷新时间 2026/11/30 23:59:59\n"
        "已使用 0/500 剩余100%\n"
        "平台奖励积分\n最近到期时间 2026/11/30 08:59:33\n"
        "已使用 55.94/3714 剩余98%\n"
    )
    print(parse(sample))
