"""通过 WorkBuddy 自带 WBIPC 代理读取真实积分；不提取云端登录令牌。

协议依据本机 WorkBuddy 的 WBIPC v1。仅发送三个固定的计费查询，
不提供任意请求入口，不保存或输出 endpoint.json 中的本机握手票据。
"""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import secrets
import struct
import time
from datetime import datetime


class BalanceError(Exception):
    pass


def _proof(ticket, role, endpoint, client_nonce, server_nonce):
    fields = ("wbipc-s" if role == "server" else "wbipc-c", "1",
              endpoint, client_nonce, server_nonce)
    data = b"".join(struct.pack(">I", len(v.encode())) + v.encode() for v in fields)
    return base64.urlsafe_b64encode(
        hmac.new(ticket.encode(), data, hashlib.sha256).digest()).decode().rstrip("=")


class _Pipe:
    """非阻塞 Win32 字节管道，每次通信有时限，关闭句柄即可取消。"""
    def __init__(self, endpoint, timeout=15):
        if os.name != "nt" or not endpoint.startswith("\\\\.\\pipe\\wbipc-"):
            raise BalanceError("不支持的 WorkBuddy 本机端点")
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                     wintypes.DWORD, ctypes.c_void_p,
                                     wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        self.k.CreateFileW.restype = wintypes.HANDLE
        self.k.SetNamedPipeHandleState.argtypes = [wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p, ctypes.c_void_p]
        for name in ("ReadFile", "WriteFile"):
            fn = getattr(self.k, name)
            fn.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                           ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        self.k.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.k.CreateFileW(endpoint, 0xC0000000, 0, None, 3, 0, None)
        if self.handle == wintypes.HANDLE(-1).value:
            self.handle = None
            raise BalanceError("WorkBuddy 本机服务不可用")
        mode = wintypes.DWORD(1)  # PIPE_NOWAIT；沿用 byte read mode
        if not self.k.SetNamedPipeHandleState(self.handle, ctypes.byref(mode), None, None):
            self.close()
            raise BalanceError("无法设置本机通信超时")
        self.timeout = timeout
        self.buffer = b""

    def close(self):
        if self.handle:
            self.k.CloseHandle(self.handle)
            self.handle = None

    def send(self, message):
        raw = (json.dumps(message, separators=(",", ":")) + "\n").encode()
        count = wintypes.DWORD()
        if not self.k.WriteFile(self.handle, raw, len(raw), ctypes.byref(count), None) or count.value != len(raw):
            raise BalanceError("本机通信已断开")

    def receive(self):
        deadline = time.monotonic() + self.timeout
        buf = ctypes.create_string_buffer(65536)
        while b"\n" not in self.buffer:
            count = wintypes.DWORD()
            ok = self.k.ReadFile(self.handle, buf, len(buf), ctypes.byref(count), None)
            if count.value:
                self.buffer += buf.raw[:count.value]
                if len(self.buffer) > 2_000_000:
                    raise BalanceError("本机响应过大")
            elif not ok and ctypes.get_last_error() != 232:  # ERROR_NO_DATA
                raise BalanceError("WorkBuddy 本机连接已关闭")
            if time.monotonic() >= deadline:
                raise BalanceError("积分查询超时")
            if not count.value:
                time.sleep(0.01)
        line, self.buffer = self.buffer.split(b"\n", 1)
        try:
            result = json.loads(line)
        except (ValueError, UnicodeError):
            raise BalanceError("本机响应格式异常") from None
        if not isinstance(result, dict):
            raise BalanceError("本机响应格式异常")
        return result


class BalanceClient:
    PATHS = {"summary": "/billing/meter/get-user-resource-summary",
             "free": "/billing/meter/get-user-resource-free-packages",
             "paid": "/billing/meter/get-user-resource-paid-packages"}

    def __init__(self, root=None, transport=_Pipe):
        self.root = Path(root or os.environ.get("WORKBUDDY_HOME") or Path.home() / ".workbuddy")
        self.transport = transport
        self.pipe = None
        self.sequence = 0

    def _connect(self):
        try:
            d = json.loads((self.root / "wbipc" / "endpoint.json").read_text(encoding="utf-8"))
            endpoint, ticket = d["endpoint"], d["ticket"]
            if not isinstance(endpoint, str) or not isinstance(ticket, str) or not ticket:
                raise ValueError()
        except (OSError, ValueError, KeyError, TypeError):
            raise BalanceError("请启动并登录 WorkBuddy") from None
        self.pipe = self.transport(endpoint)
        nonce = secrets.token_urlsafe(16)
        self.pipe.send({"type": "session_hello", "protocol_min": 1, "protocol_max": 1,
                        "client_nonce": nonce, "ticket_id": hashlib.sha256(ticket.encode()).hexdigest()[:16],
                        "client": {"kind": "desktop-pet", "id": "pet-whale", "version": "1"}})
        reply = self.pipe.receive()
        server_nonce = reply.get("server_nonce", "")
        if (reply.get("type") != "session_challenge" or reply.get("protocol") != 1
                or not isinstance(server_nonce, str) or not server_nonce
                or not hmac.compare_digest(str(reply.get("server_proof", "")),
                    _proof(ticket, "server", endpoint, nonce, server_nonce))):
            raise BalanceError("WorkBuddy 本机握手验证失败")
        self.pipe.send({"type": "session_prove", "client_proof":
                        _proof(ticket, "client", endpoint, nonce, server_nonce)})
        reply = self.pipe.receive()
        if reply.get("type") != "session_hello_ack" or "wb.request" not in reply.get("pipes", []):
            raise BalanceError("请在 WorkBuddy 中重新登录")
        channel = self._rpc("broker/GetPipe", {"pipe": "wb.request"})
        if "http.fetch" not in channel.get("methods", []):
            raise BalanceError("当前 WorkBuddy 不支持积分查询")
        self.channel = channel["channel"]

    def _rpc(self, method, params, mode=None):
        self.sequence += 1
        message = {"jsonrpc": "2.0", "id": self.sequence, "method": method, "params": params}
        if mode:
            message["mode"] = mode
        self.pipe.send(message)
        reply = self.pipe.receive()
        if reply.get("id") != self.sequence or "error" in reply or "result" not in reply:
            # 不回显上游错误正文，避免其中带有账号标识。
            raise BalanceError("WorkBuddy 积分服务不可用，请稍后重试")
        return reply["result"]

    def _query(self, kind, payload):
        reply = self._rpc(self.channel + "/http.fetch", {
            "method": "POST", "path": self.PATHS[kind],
            "headers": {"content-type": "application/json"},
            "body_b64": base64.b64encode(json.dumps(payload).encode()).decode()}, "call")
        if reply.get("status") != 200:
            raise BalanceError("积分接口 HTTP %s" % reply.get("status", "异常"))
        try:
            body = json.loads(base64.b64decode(reply["body_b64"], validate=True))
            if body.get("code") != 0 or not isinstance(body.get("data"), dict):
                raise ValueError()
            return body["data"]
        except (KeyError, TypeError, ValueError):
            raise BalanceError("积分接口返回异常，请确认登录状态") from None

    def fetch(self):
        try:
            self._connect()
            summary = self._query("summary", {})
            total = _number(summary.get("TotalCredits"))
            codes = [p["PackageCode"] for p in summary.get("Packages", []) if p.get("PackageCode")]
            accounts = []
            if codes:
                for kind in ("free", "paid"):
                    received = 0
                    for page in range(1, 11):
                        data = self._query(kind, {"PackageCodes": codes, "PageNumber": page,
                            "PageSize": 200, "Status": [0, 3], "OnlyValidPeriod": True,
                            "IsDisplayTotalInfo": True})
                        rows = data.get("Accounts")
                        if not isinstance(rows, list):
                            raise BalanceError("积分包数据格式异常")
                        accounts.extend(rows)
                        received += len(rows)
                        count = int(data.get("TotalCount", 0))
                        if count > received and not rows:
                            raise BalanceError("积分包分页数据不完整")
                        if (count > 0 and received >= count) or (count == 0 and len(rows) < 200):
                            break
                    else:
                        raise BalanceError("积分包分页超出限制")
            groups = normalize_accounts(accounts)
            nearest = next((g for g in groups if g["expire"]), None)
            return {"total": total, "groups": groups,
                    "expire_left": nearest["left"] if nearest else 0.0,
                    "expire": nearest["expire"] if nearest else "",
                    "updated_at": time.time()}
        finally:
            if self.pipe:
                self.pipe.close()
                self.pipe = None


def _number(value):
    try:
        n = float(value)
        if not math.isfinite(n) or n < 0:
            raise ValueError()
        return round(n, 2)
    except (TypeError, ValueError, OverflowError):
        raise BalanceError("积分数值异常") from None


def _date(value):
    if value in (None, ""):
        return ""
    try:
        if isinstance(value, (float, int)):
            return datetime.fromtimestamp(value / 1000 if value > 1e11 else value).strftime("%Y-%m-%d %H:%M:%S")
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError, OSError, OverflowError):
        raise BalanceError("积分包到期时间异常") from None


def normalize_accounts(accounts):
    groups, seen = [], set()
    for a in accounts:
        if not isinstance(a, dict):
            raise BalanceError("积分包数据格式异常")
        rid = a.get("ResourceId")
        if rid and rid in seen:
            continue
        if rid:
            seen.add(rid)
        value = next((a[k] for k in ("CycleCapacityRemainPrecise", "CycleCapacityRemain",
                                    "CapacityRemainPrecise", "CapacityRemain") if a.get(k) is not None), None)
        left = _number(value)
        if left <= 0:
            continue
        attrs = {v.get("Key"): v.get("Value") for v in a.get("AccountAttributes", []) if isinstance(v, dict)}
        # 周期套餐优先本周期截止时间，普通赠送包优先扣减截止时间。
        dates = (("CycleEndTime", "DeductionEndTime", "ExpiredTime") if a.get("CapacityType") == 4
                 else ("DeductionEndTime", "ExpiredTime", "CycleEndTime"))
        expire = _date(next((a[k] for k in dates if a.get(k)), None))
        groups.append({"name": attrs.get("grantReason") or a.get("PackageName") or "积分",
                       "left": left, "expire": expire})
    groups.sort(key=lambda g: g["expire"] or "9999")
    return groups
