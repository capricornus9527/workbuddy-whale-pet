"""离线回归：不联网、不读用户账号、不启动窗口。"""
import base64
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import wb_balance as balance
import wb_usage
import barlib
import pet


def account(left="10.25", **kwargs):
    return {"ResourceId": "fixture", "CycleCapacityRemainPrecise": left,
            "PackageName": "测试积分包", "DeductionEndTime": "2026-11-30 20:48:52", **kwargs}


class FakePipe:
    ticket = "local-fixture-only"
    corrupt_proof = False
    malformed_body = False
    fail_http = False
    empty = False
    paginated = False
    instances = []

    def __init__(self, endpoint):
        self.endpoint, self.closed, self.requests = endpoint, False, []
        self.instances.append(self)

    def close(self):
        self.closed = True

    def send(self, msg):
        self.message = msg
        self.requests.append(msg)

    def receive(self):
        msg = self.message
        if msg.get("type") == "session_hello":
            proof = balance._proof(self.ticket, "server", self.endpoint, msg["client_nonce"], "server-fixture")
            return {"type": "session_challenge", "protocol": 1, "server_nonce": "server-fixture",
                    "server_proof": "wrong" if self.corrupt_proof else proof}
        if msg.get("type") == "session_prove":
            return {"type": "session_hello_ack", "pipes": ["wb.request"]}
        if msg["method"] == "broker/GetPipe":
            data = {"channel": "c:wb.request", "methods": ["http.fetch"]}
        else:
            p = msg["params"]
            assert p["path"] in balance.BalanceClient.PATHS.values()
            payload = json.loads(base64.b64decode(p["body_b64"]))
            if p["path"].endswith("summary"):
                body = {"TotalCredits": "0" if self.empty else "10.25", "Packages": [] if self.empty else [{"PackageCode": "fixture-code"}]}
            elif p["path"].endswith("free-packages"):
                if self.paginated:
                    page = payload["PageNumber"]
                    rows = [account(ResourceId=str(i)) for i in (range(200) if page == 1 else range(200,201))]
                    body = {"Accounts": rows, "TotalCount": 201}
                else:
                    body = {"Accounts": [account()], "TotalCount": 1}
            else:
                body = {"Accounts": [], "TotalCount": 0}
            raw = b"bad JSON" if self.malformed_body else json.dumps({"code": 0, "data": body}).encode()
            data = {"status": 429 if self.fail_http else 200,
                    "body_b64": base64.b64encode(raw).decode()}
        return {"id": msg["id"], "result": data}


class BalanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.root = Path(self.temp.name)
        (self.root / "wbipc").mkdir()
        (self.root / "wbipc/endpoint.json").write_text(json.dumps({
            "endpoint": r"\\.\pipe\wbipc-fixture", "ticket": FakePipe.ticket}), encoding="utf-8")
        FakePipe.instances.clear()
        for attr in ("corrupt_proof", "malformed_body", "fail_http", "empty", "paginated"):
            setattr(FakePipe, attr, False)

    def tearDown(self):
        self.temp.cleanup()

    def fetch(self):
        return balance.BalanceClient(self.root, FakePipe).fetch()

    def test_real_query_shape_and_cleanup(self):
        result = self.fetch()
        self.assertEqual((result["total"], result["expire_left"]), (10.25, 10.25))
        self.assertTrue(FakePipe.instances[-1].closed)
        requests = [r for r in FakePipe.instances[-1].requests if "params" in r and "path" in r["params"]]
        self.assertEqual(len(requests), 3)
        self.assertTrue(all("authorization" not in r["params"]["headers"] for r in requests))

    def test_server_proof_required_and_handle_closed(self):
        FakePipe.corrupt_proof = True
        with self.assertRaisesRegex(balance.BalanceError, "握手"):
            self.fetch()
        self.assertTrue(FakePipe.instances[-1].closed)

    def test_missing_login(self):
        (self.root / "wbipc/endpoint.json").unlink()
        with self.assertRaises(balance.BalanceError):
            self.fetch()

    def test_bad_json_and_rate_limit(self):
        FakePipe.malformed_body = True
        with self.assertRaises(balance.BalanceError):
            self.fetch()
        FakePipe.fail_http = True
        with self.assertRaisesRegex(balance.BalanceError, "429"):
            self.fetch()

    def test_zero_balance_clears_expiry_and_groups(self):
        FakePipe.empty = True
        result = self.fetch()
        self.assertEqual((result["total"], result["groups"], result["expire"], result["expire_left"]), (0, [], "", 0))

    def test_pagination(self):
        FakePipe.paginated = True
        self.assertEqual(len(self.fetch()["groups"]), 201)

    def test_invalid_credit_numbers(self):
        for v in (None, "NaN", "Infinity", -1, "oops"):
            with self.subTest(v=v), self.assertRaises(balance.BalanceError):
                balance.normalize_accounts([account(v)])

    def test_precision_dedup_and_cycle_expiry(self):
        a = account("10.2599", CapacityType=4, CycleEndTime="2026-11-30 23:59:59",
                    AccountAttributes=[{"Key": "grantReason", "Value": "注册奖励"}])
        rows = balance.normalize_accounts([a, a, account("0", ResourceId="zero")])
        self.assertEqual(rows, [{"name": "注册奖励", "left": 10.26, "expire": "2026-11-30 23:59:59"}])

    def test_wal_commit_invalidates_usage_cache(self):
        db = sqlite3.connect(self.root / "workbuddy.db")
        try:
            db.execute("pragma journal_mode=wal")
            db.execute("create table session_usage(session_id text,used int,size int,credit_json text)")
            db.execute("insert into session_usage values ('s',1,100,?)", (json.dumps({"r":1}),))
            db.commit()
            tracker = wb_usage.UsageTracker(str(self.root))
            self.assertEqual(tracker._session_usage("s")[0], {"r":1})
            stamp = os.stat(tracker.db_path).st_mtime_ns
            db.execute("update session_usage set used=2,credit_json=?", (json.dumps({"r":2}),))
            db.commit()
            self.assertEqual(os.stat(tracker.db_path).st_mtime_ns, stamp)
            self.assertEqual(tracker._session_usage("s"), ({"r":2},2,100))
        finally:
            db.close()

    def test_today_uses_settled_credits_for_all_sessions(self):
        project = self.root / "projects/test"
        project.mkdir(parents=True)
        db = sqlite3.connect(self.root / "workbuddy.db")
        try:
            db.execute("create table session_usage(session_id text,used int,size int,credit_json text)")
            for sid, settled in (("first", 3), ("latest", 4)):
                line = {"timestamp":time.time(), "providerData": {
                    "conversationRequestId": "r", "rawUsage": {"credit":1}}}
                (project / (sid + ".jsonl")).write_text(json.dumps(line) + "\n", encoding="utf-8")
                db.execute("insert into session_usage values (?,1,100,?)", (sid,json.dumps({"r":settled})))
            db.commit()
            result = wb_usage.UsageTracker(str(self.root))._today_credits(time.time(),str(project / "latest.jsonl"))
            self.assertEqual(result,(7,2))
        finally:
            db.close()

    def app(self):
        app = pet.PetApp.__new__(pet.PetApp)
        app.cfg = {"breathing":False}
        app.stats = wb_usage.Stats()
        app.quota_left, app.quota_expire_left, app.quota_expire = 20,10,"2026-11-30"
        app.quota_groups, app.quota_status, app.quota_synced_at = [],"已同步",0
        app.cache, app._bar_gen, app._dot_bucket = {},0,0
        app._dot_phase, app._prev, app._fade, app._anim_frame = 0,None,0,0
        app.bucket, app.win_w, app.win_h, app.pad_x, app.bubble_h = 0,212,220,2,180
        app.bubble, app.bar_img = barlib.BubbleRenderer(1),None
        app._swing_angle = lambda: 0
        app._anim_of = lambda n: None
        app._breath = lambda b: (1,0)
        app._sprite_img = lambda n: None
        app._ctx = lambda: {"stats":app.stats}
        app._save, app._redraw_card, app._pick_from = Mock(),Mock(),Mock(return_value="base")
        return app

    def test_compose_updates_for_expiry_amount_only(self):
        app = self.app()
        first = app._compose("base").tobytes()
        app.quota_expire_left = 9.99
        self.assertNotEqual(app._compose("base").tobytes(), first)

    def test_failure_retains_balance_but_marks_stale(self):
        app = self.app()
        app._balance_seen = None
        app._balance_result = (None,"积分查询超时",100)
        app._consume_balance_result()
        self.assertEqual((app.quota_left,app.quota_status),(20,"未同步"))

    def test_success_replaces_empty_groups_and_persists(self):
        app = self.app()
        app.quota_groups = [{"left":10}]
        app._balance_seen = None
        app._balance_result = ({"total":0,"groups":[],"expire":"","expire_left":0,"updated_at":100},"",100)
        app._consume_balance_result()
        self.assertEqual((app.quota_left,app.quota_groups,app.quota_expire),(0,[],""))
        self.assertEqual(app.cfg["quota_synced_at"],100)
        app._save.assert_called_once()

    def test_dpi_and_extreme_values_fit_window(self):
        for scale in (1,1.25,1.5,2,2.5):
            for value in (None,0,999999999.99):
                renderer = barlib.BubbleRenderer(scale)
                image = renderer.render_body({"quota_left":value,"quota_status":"未同步"})
                self.assertLessEqual(image.width,round(212*scale))
                self.assertEqual(image.height,renderer.body_height())
                self.assertIs(image,renderer.render_body({"quota_left":value,"quota_status":"未同步"}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
