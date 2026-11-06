import copy
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from reset_card_schedule import process_schedule, PLUGIN_KEY, UNKNOWN

NOW = datetime(2026, 11, 6, 18, 0, tzinfo=timezone.utc)
TASK = {"id": "11111111-1111-4111-8111-111111111111", "enabled": True,
        "account_id": 1, "run_at": NOW.isoformat(),
        "credit_expires_at": (NOW + timedelta(hours=5)).isoformat()}


class FakeApi:
    def __init__(self, config):
        self.config = copy.deepcopy(config)
        self.posts = 0
        self.fail_post = False
        self.fail_put = False
        self.fail_final_put = False
        self.credits = [TASK["credit_expires_at"]]
        self.auto_reset = False
        self.result = {"windows_reset": 2}

    def account(self, account_id):
        return {"platform": "openai", "type": "oauth",
                "extra": {"auto_reset_credit_enabled": self.auto_reset}}

    def request(self, method, path, body=None):
        if path == "/admin/plugins":
            return [{"id": 1, "plugin_key": PLUGIN_KEY}]
        if path.endswith("/config"):
            if method == "GET":
                return copy.deepcopy(self.config)
            if self.fail_put or (self.fail_final_put and self.posts):
                raise TimeoutError()
            self.config = copy.deepcopy(body)
            return body
        if path.endswith("/quota"):
            return {"rate_limit_reset_credits": {"available_count": len(self.credits),
                    "credits": [{"expires_at": e} for e in self.credits]}}
        if path.endswith("/reset-quota"):
            assert self.config["reset_card_schedule"]["enabled"] is False
            self.posts += 1
            if self.fail_post:
                raise TimeoutError()
            return self.result
        raise AssertionError(path)


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / "config.json"
        self.config = {"enabled": False, "dry_run": False, "target_groups": ["keep"],
                       "reset_card_schedule": copy.deepcopy(TASK)}
        self.api = FakeApi(self.config)
        self.save_local()

    def tearDown(self):
        self.tmp.cleanup()

    def save_local(self):
        self.path.write_text(json.dumps(self.config), encoding="utf-8")

    def run_schedule(self, now=NOW):
        return process_schedule(self.api, str(self.path), self.root, now)

    def test_future_waits(self):
        self.assertEqual(self.run_schedule(NOW-timedelta(seconds=1)), "waiting")
        self.assertEqual(self.api.posts, 0)

    def test_runs_once_independent_of_sync_switch(self):
        self.assertEqual(self.run_schedule(), "success")
        self.assertEqual(self.run_schedule(), "success")
        self.assertEqual(self.api.posts, 1)
        self.assertFalse(self.api.config["reset_card_schedule"]["enabled"])
        self.assertEqual(self.api.config["target_groups"], ["keep"])

    def test_disable_cancels(self):
        self.api.config["reset_card_schedule"]["enabled"] = False
        self.assertEqual(self.run_schedule(), "cancelled")
        self.assertEqual(self.api.posts, 0)

    def test_replaced_task_not_overwritten(self):
        self.api.config["reset_card_schedule"]["id"] = "22222222-2222-4222-8222-222222222222"
        self.assertEqual(self.run_schedule(), "cancelled")
        self.assertEqual(self.api.posts, 0)

    def test_dry_run_closes_once_without_consumption(self):
        self.api.config["dry_run"] = True
        self.assertEqual(self.run_schedule(), "dry_run")
        self.assertFalse(self.api.config["reset_card_schedule"]["enabled"])
        self.assertEqual(self.api.posts, 0)

    def test_late_skipped(self):
        self.assertEqual(self.run_schedule(NOW+timedelta(minutes=16)), "skipped")
        self.assertEqual(self.api.posts, 0)

    def test_expired_skipped(self):
        self.assertEqual(self.run_schedule(NOW+timedelta(hours=6)), "skipped")
        self.assertEqual(self.api.posts, 0)

    def test_missing_original_credit_does_not_consume_another(self):
        self.api.credits = [(NOW+timedelta(days=10)).isoformat()]
        self.assertEqual(self.run_schedule(), "skipped")
        self.assertEqual(self.api.posts, 0)

    def test_threshold_automation_conflict(self):
        self.api.auto_reset = True
        self.assertEqual(self.run_schedule(), "skipped")
        self.assertEqual(self.api.posts, 0)

    def test_ambiguous_post_never_retried(self):
        self.api.fail_post = True
        self.assertEqual(self.run_schedule(), "unknown")
        self.api.fail_post = False
        self.assertEqual(self.run_schedule(), "unknown")
        self.assertEqual(self.api.posts, 1)

    def test_cannot_disable_never_posts(self):
        self.api.fail_put = True
        self.assertEqual(self.run_schedule(), "error")
        self.api.fail_put = False
        self.assertEqual(self.run_schedule(), "unknown")
        self.assertEqual(self.api.posts, 0)
        self.assertFalse(self.api.config["reset_card_schedule"]["enabled"])

    def test_final_save_failure_reconciles_without_post(self):
        self.api.fail_final_put = True
        self.assertEqual(self.run_schedule(), "error")
        self.api.fail_final_put = False
        self.assertEqual(self.run_schedule(), "success")
        self.assertEqual(self.api.posts, 1)
        self.assertIn("已自动使用", self.api.config["reset_card_schedule"]["result"])

    def test_crash_after_claim_never_posts(self):
        with patch.object(self.api, "account", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_schedule()
        self.assertEqual(self.run_schedule(), "claimed")
        self.assertEqual(self.api.posts, 0)
        self.assertEqual(self.api.config["reset_card_schedule"]["result"], UNKNOWN)

    def test_malformed_response_is_unknown(self):
        self.api.result = {}
        self.assertEqual(self.run_schedule(), "unknown")
        self.assertEqual(self.run_schedule(), "unknown")
        self.assertEqual(self.api.posts, 1)

    def test_naive_time_rejected(self):
        self.config["reset_card_schedule"]["run_at"] = "2026-11-07T02:00:00"
        self.save_local()
        self.assertEqual(self.run_schedule(), "error")
        self.assertEqual(self.api.posts, 0)

    def test_disabled_old_task_is_not_executed(self):
        self.config["reset_card_schedule"]["enabled"] = False
        self.save_local()
        self.assertEqual(self.run_schedule(), "disabled")
        self.assertEqual(self.api.posts, 0)

    def test_preserve_unrelated_concurrent_settings(self):
        self.api.config["target_groups"] = ["new"]
        self.run_schedule()
        self.assertEqual(self.api.config["target_groups"], ["new"])

    def test_new_explicit_schedule_can_run(self):
        self.run_schedule()
        self.config["reset_card_schedule"]["id"] = "22222222-2222-4222-8222-222222222222"
        self.api.config = copy.deepcopy(self.config)
        self.save_local()
        self.assertEqual(self.run_schedule(), "success")
        self.assertEqual(self.api.posts, 2)


if __name__ == "__main__":
    unittest.main()
