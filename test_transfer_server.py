import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path

from transfer_server import (
    AuthError,
    Config,
    HttpError,
    RateLimiter,
    TransferHttpServer,
    TransferService,
    TransferStore,
    UserInfo,
    resolve_recipient,
    validate_amount,
)


def config(**overrides):
    values = {
        "base_url": "http://127.0.0.1:18080",
        "admin_api_key": "test-admin-key",
        "listen_host": "127.0.0.1",
        "listen_port": 18083,
        "min_amount": Decimal("1"),
        "db_path": Path("transfer.sqlite3"),
        "rate_limit_per_minute": 5,
        "request_timeout_seconds": 15,
    }
    values.update(overrides)
    return Config(**values)


class FakeClient:
    def __init__(self, profiles=None, users=None):
        self.profiles = profiles or {}
        self.users = users or []
        self.adjust_calls = []
        self.failures = {}

    def user_profile(self, token):
        result = self.profiles.get(token)
        if result is None:
            raise AuthError()
        if isinstance(result, Exception):
            raise result
        return result

    def search_users(self, email):
        return list(self.users)

    def fail_next(self, user_id, operation, error=None):
        self.failures[(user_id, operation)] = error or RuntimeError("simulated failure")

    def adjust_balance(self, user_id, amount, operation, notes):
        self.adjust_calls.append(
            {"user_id": user_id, "amount": amount, "operation": operation, "notes": notes}
        )
        error = self.failures.pop((user_id, operation), None)
        if error is not None:
            raise error


class ServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = TransferStore(Path(self.directory.name) / "transfer.sqlite3")
        self.sender = UserInfo(1, "sender@example.com", "sender", 100.0)
        self.receiver_row = {"id": 2, "email": "receiver@example.com"}
        self.client = FakeClient(
            profiles={"sender-token": self.sender}, users=[self.receiver_row]
        )
        self.service = TransferService(
            config(), self.client, self.store, RateLimiter(5)
        )

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def transfer(self, amount=10, email="receiver@example.com", key="key-1"):
        return self.service.transfer(
            "sender-token", {"recipient_email": email, "amount": amount}, key
        )

    def record_status(self):
        return self.store.db.execute("SELECT status FROM transfers").fetchone()[0]


class AmountValidationTests(ServiceTestCase):
    def assert_rejected(self, amount, message):
        with self.assertRaisesRegex(HttpError, message) as caught:
            self.transfer(amount=amount)
        self.assertEqual(400, caught.exception.status)

    def test_negative_amount_rejected(self):
        self.assert_rejected(-5, "金额必须大于 0")

    def test_zero_amount_rejected(self):
        self.assert_rejected(0, "金额必须大于 0")

    def test_more_than_two_decimals_rejected(self):
        self.assert_rejected(1.005, "最多保留两位小数")

    def test_below_min_amount_rejected(self):
        self.assert_rejected(0.5, "不能低于 1")

    def test_non_number_amount_rejected(self):
        self.assert_rejected("10", "金额必须是数字")
        self.assert_rejected(True, "金额必须是数字")

    def test_validation_failure_leaves_no_record(self):
        with self.assertRaises(HttpError):
            self.transfer(amount=0)
        count = self.store.db.execute("SELECT count(*) FROM transfers").fetchone()[0]
        self.assertEqual(0, count)

    def test_validate_amount_accepts_two_decimals(self):
        self.assertEqual(Decimal("9.99"), validate_amount(9.99, Decimal("1")))


class RecipientResolutionTests(ServiceTestCase):
    def test_no_exact_match_returns_404(self):
        self.client.users = []
        with self.assertRaisesRegex(HttpError, "未找到该邮箱对应的用户") as caught:
            self.transfer()
        self.assertEqual(404, caught.exception.status)
        self.assertEqual("failed", self.record_status())

    def test_username_near_match_is_not_accepted(self):
        # search 同时匹配用户名，邮箱不同的结果必须被排除。
        self.client.users = [
            {"id": 3, "email": "other@example.com", "username": "receiver@example.com"}
        ]
        with self.assertRaisesRegex(HttpError, "未找到该邮箱对应的用户"):
            resolve_recipient(self.client, "receiver@example.com")

    def test_exact_match_case_insensitive(self):
        self.client.users = [{"id": 2, "email": "Receiver@Example.com"}]
        receiver = resolve_recipient(self.client, "receiver@example.com")
        self.assertEqual(2, receiver["id"])

    def test_multiple_exact_matches_refused(self):
        self.client.users = [
            {"id": 2, "email": "receiver@example.com"},
            {"id": 4, "email": "RECEIVER@example.com"},
        ]
        with self.assertRaisesRegex(HttpError, "匹配到多个用户") as caught:
            self.transfer()
        self.assertEqual(409, caught.exception.status)
        self.assertEqual([], self.client.adjust_calls)

    def test_near_matches_filtered_to_single_exact_match(self):
        self.client.users = [
            {"id": 9, "email": "receiver@example.com.evil.test"},
            self.receiver_row,
        ]
        status, payload = self.transfer()
        self.assertEqual(200, status)
        self.assertEqual("receiver@example.com", payload["recipient_email"])


class TransferExecutionTests(ServiceTestCase):
    def test_successful_transfer_subtracts_then_adds(self):
        status, payload = self.transfer(amount=9.5)
        self.assertEqual(200, status)
        self.assertEqual("succeeded", payload["status"])
        self.assertEqual("succeeded", self.record_status())
        self.assertEqual(2, len(self.client.adjust_calls))
        subtract, add = self.client.adjust_calls
        self.assertEqual((1, "subtract", Decimal("9.5")), (subtract["user_id"], subtract["operation"], subtract["amount"]))
        self.assertEqual((2, "add"), (add["user_id"], add["operation"]))
        self.assertIn("transfer to receiver@example.com via quota-sync", subtract["notes"])
        self.assertIn("transfer from sender@example.com via quota-sync", add["notes"])

    def test_insufficient_balance_precheck_aborts(self):
        self.client.profiles["sender-token"] = UserInfo(1, "sender@example.com", "sender", 5.0)
        with self.assertRaisesRegex(HttpError, "余额不足") as caught:
            self.transfer(amount=10)
        self.assertEqual(400, caught.exception.status)
        self.assertEqual([], self.client.adjust_calls)
        self.assertEqual("failed", self.record_status())

    def test_subtract_failure_aborts_without_crediting(self):
        self.client.fail_next(1, "subtract")
        with self.assertRaisesRegex(HttpError, "扣减余额失败") as caught:
            self.transfer()
        self.assertEqual(502, caught.exception.status)
        self.assertEqual(1, len(self.client.adjust_calls))
        self.assertEqual("subtract", self.client.adjust_calls[0]["operation"])
        self.assertEqual("failed", self.record_status())

    def test_add_failure_triggers_compensation(self):
        self.client.fail_next(2, "add")
        with self.assertRaisesRegex(HttpError, "金额已退回") as caught:
            self.transfer()
        self.assertEqual(502, caught.exception.status)
        operations = [(c["user_id"], c["operation"]) for c in self.client.adjust_calls]
        self.assertEqual([(1, "subtract"), (2, "add"), (1, "add")], operations)
        self.assertEqual("compensated", self.record_status())

    def test_compensation_failure_marks_needs_attention(self):
        self.client.fail_next(2, "add")
        self.client.fail_next(1, "add")
        with self.assertRaisesRegex(HttpError, "请联系管理员") as caught:
            self.transfer()
        self.assertEqual(502, caught.exception.status)
        self.assertEqual("needs_attention", self.record_status())

    def test_self_transfer_rejected(self):
        self.client.users = [{"id": 1, "email": "sender@example.com"}]
        with self.assertRaisesRegex(HttpError, "不能划转给自己") as caught:
            self.transfer(email="sender@example.com")
        self.assertEqual(400, caught.exception.status)
        self.assertEqual([], self.client.adjust_calls)

    def test_invalid_token_returns_401(self):
        with self.assertRaises(AuthError) as caught:
            self.service.transfer(
                "bad-token", {"recipient_email": "receiver@example.com", "amount": 10}, "k"
            )
        self.assertEqual(401, caught.exception.status)
        count = self.store.db.execute("SELECT count(*) FROM transfers").fetchone()[0]
        self.assertEqual(0, count)


class IdempotencyTests(ServiceTestCase):
    def test_replay_returns_previous_result_without_reexecuting(self):
        status, first = self.transfer(key="dup-key")
        self.assertEqual(200, status)
        status, second = self.transfer(key="dup-key")
        self.assertEqual(200, status)
        self.assertTrue(second["duplicate"])
        self.assertEqual("succeeded", second["status"])
        self.assertEqual(2, len(self.client.adjust_calls))
        count = self.store.db.execute("SELECT count(*) FROM transfers").fetchone()[0]
        self.assertEqual(1, count)

    def test_replay_of_failed_transfer_returns_error(self):
        self.client.fail_next(1, "subtract")
        with self.assertRaises(HttpError):
            self.transfer(key="fail-key")
        with self.assertRaisesRegex(HttpError, "扣减余额失败") as caught:
            self.transfer(key="fail-key")
        self.assertEqual(400, caught.exception.status)
        self.assertEqual(1, len(self.client.adjust_calls))

    def test_idempotency_key_scoped_per_sender(self):
        other = UserInfo(7, "other@example.com", "other", 50.0)
        self.client.profiles["other-token"] = other
        self.transfer(key="shared-key")
        status, payload = self.service.transfer(
            "other-token", {"recipient_email": "receiver@example.com", "amount": 5},
            "shared-key",
        )
        self.assertEqual(200, status)
        self.assertNotIn("duplicate", payload)
        self.assertEqual(4, len(self.client.adjust_calls))


class RateLimitTests(ServiceTestCase):
    def test_rate_limit_returns_429(self):
        service = TransferService(config(), self.client, self.store, RateLimiter(2))
        for index in range(2):
            service.transfer(
                "sender-token",
                {"recipient_email": "receiver@example.com", "amount": 10},
                f"key-{index}",
            )
        with self.assertRaisesRegex(HttpError, "过于频繁") as caught:
            service.transfer(
                "sender-token",
                {"recipient_email": "receiver@example.com", "amount": 10},
                "key-2",
            )
        self.assertEqual(429, caught.exception.status)

    def test_window_slides(self):
        clock = [1000.0]
        limiter = RateLimiter(1, window_seconds=60.0, clock=lambda: clock[0])
        self.assertTrue(limiter.allow(1))
        self.assertFalse(limiter.allow(1))
        clock[0] += 61.0
        self.assertTrue(limiter.allow(1))


class HistoryTests(ServiceTestCase):
    def test_history_shows_outgoing_and_incoming(self):
        self.transfer(amount=10, key="out-1")
        other = UserInfo(7, "other@example.com", "other", 50.0)
        self.client.profiles["other-token"] = other
        self.client.users = [{"id": 1, "email": "sender@example.com"}]
        self.service.transfer(
            "other-token",
            {"recipient_email": "sender@example.com", "amount": 3},
            "in-1",
        )
        status, rows = self.service.history("sender-token")
        self.assertEqual(200, status)
        self.assertEqual(2, len(rows))
        directions = {(row["direction"], row["counterpart_email"]) for row in rows}
        self.assertEqual(
            {("out", "receiver@example.com"), ("in", "other@example.com")}, directions
        )


class HttpLayerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        store = TransferStore(Path(self.directory.name) / "transfer.sqlite3")
        sender = UserInfo(1, "sender@example.com", "sender", 100.0)
        client = FakeClient(profiles={"good-token": sender})
        service = TransferService(config(), client, store, RateLimiter(5))
        self.server = TransferHttpServer(("127.0.0.1", 0), service)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.store = store

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.store.close()
        self.directory.cleanup()

    def request(self, path, headers=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        req = urllib.request.Request(url, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def test_page_served(self):
        status, body = self.request("/transfer/")
        self.assertEqual(200, status)
        self.assertIn("额度划转".encode("utf-8"), body)

    def test_me_without_token_returns_401(self):
        status, body = self.request("/transfer/api/me")
        self.assertEqual(401, status)
        self.assertEqual(False, json.loads(body)["ok"])

    def test_me_with_invalid_token_returns_401(self):
        status, _ = self.request(
            "/transfer/api/me", {"Authorization": "Bearer bad-token"}
        )
        self.assertEqual(401, status)

    def test_me_with_valid_token(self):
        status, body = self.request(
            "/transfer/api/me", {"Authorization": "Bearer good-token"}
        )
        self.assertEqual(200, status)
        data = json.loads(body)["data"]
        self.assertEqual("sender@example.com", data["email"])
        self.assertEqual(100.0, data["balance"])

    def test_unknown_path_returns_404_json(self):
        status, body = self.request("/transfer/nope")
        self.assertEqual(404, status)
        self.assertEqual(False, json.loads(body)["ok"])


if __name__ == "__main__":
    unittest.main()
