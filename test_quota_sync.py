import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from quota_sync import (
    Config,
    Snapshot,
    State,
    StateStore,
    decide,
    normalize_base_url,
    process_pending_events,
)

UTC = timezone.utc


def config(**overrides):
    values = {
        "enabled": True,
        "dry_run": True,
        "account_ids": (1,),
        "group_ids": (4,),
        "group_selectors": ("4",),
        "reset_daily": False,
        "reset_weekly": True,
        "reset_monthly": False,
        "min_cycle_shift": timedelta(days=3),
        "rearm_remaining": timedelta(days=5),
        "jitter_tolerance": timedelta(hours=6),
        "base_url": "http://127.0.0.1:18080",
        "admin_api_key": "test-admin-key",
        "request_timeout_seconds": 15,
        "state_path": Path("state.sqlite3"),
    }
    values.update(overrides)
    return Config(**values)


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.sample_at = datetime(2026, 8, 29, 6, 0, tzinfo=UTC)
        self.reset_at = datetime(2026, 9, 4, 16, 0, tzinfo=UTC)
        self.snapshot = Snapshot(self.reset_at, self.sample_at, 30.0, 554400)

    def test_first_run_only_establishes_baseline(self):
        self.assertEqual("baseline", decide(None, self.snapshot, config()).action)

    def test_same_snapshot_is_stale(self):
        state = State(self.reset_at, self.sample_at, 30.0)
        self.assertEqual("stale", decide(state, self.snapshot, config()).action)

    def test_small_reset_at_jitter_is_same_cycle(self):
        state = State(
            self.reset_at - timedelta(seconds=15),
            self.sample_at - timedelta(minutes=1),
            29.0,
        )
        self.assertEqual("observe", decide(state, self.snapshot, config()).action)

    def test_full_cycle_advance_triggers_reset(self):
        state = State(
            self.reset_at - timedelta(days=7),
            self.sample_at - timedelta(minutes=1),
            98.0,
        )
        self.assertEqual("reset", decide(state, self.snapshot, config()).action)

    def test_cycle_advance_must_be_rearmed(self):
        snapshot = Snapshot(
            self.sample_at + timedelta(days=2), self.sample_at, 2.0, 172800
        )
        state = State(
            snapshot.reset_at - timedelta(days=7),
            self.sample_at - timedelta(minutes=1),
            98.0,
        )
        self.assertEqual("not_rearmed", decide(state, snapshot, config()).action)

    def test_backward_reset_at_is_suspicious(self):
        state = State(
            self.reset_at + timedelta(days=1),
            self.sample_at - timedelta(minutes=1),
            29.0,
        )
        self.assertEqual("suspicious", decide(state, self.snapshot, config()).action)


class ConfigTests(unittest.TestCase):
    def test_enabled_requires_group(self):
        with self.assertRaisesRegex(ValueError, "GROUP_IDS"):
            Config.from_env({"ENABLED": "true", "GROUP_IDS": ""})

    def test_disabled_allows_empty_group(self):
        parsed = Config.from_env({"ENABLED": "false"})
        self.assertFalse(parsed.enabled)
        self.assertEqual((), parsed.group_ids)

    def test_group_ids_are_validated_and_deduplicated(self):
        parsed = Config.from_env(
            {
                "ENABLED": "true",
                "GROUP_IDS": "4,3,4",
                "SUB2API_ADMIN_API_KEY": "test-key",
            }
        )
        self.assertEqual((4, 3), parsed.group_ids)

    def test_enabled_requires_admin_api_key(self):
        with self.assertRaisesRegex(ValueError, "SUB2API_ADMIN_API_KEY"):
            Config.from_env({"ENABLED": "true", "GROUP_IDS": "4"})

    def test_enabled_rejects_daily_only(self):
        with self.assertRaisesRegex(ValueError, "至少要启用周或月窗口"):
            Config.from_env(
                {
                    "ENABLED": "true",
                    "GROUP_IDS": "4",
                    "SUB2API_ADMIN_API_KEY": "test-key",
                    "RESET_DAILY": "true",
                    "RESET_WEEKLY": "false",
                    "RESET_MONTHLY": "false",
                }
            )

    def test_base_url_rejects_credentials(self):
        with self.assertRaisesRegex(ValueError, "不能包含凭据"):
            normalize_base_url("https://admin:secret@example.com")

    def test_native_config_accepts_group_names_and_ids(self):
        base = Config.from_env({"ENABLED": "false"})
        parsed = Config.from_native_json(
            base,
            {
                "enabled": True,
                "dry_run": True,
                "account_ids": [1, 2],
                "target_groups": ["GPT订阅550每周", "3", "GPT订阅550每周"],
                "reset_daily": False,
                "reset_weekly": True,
                "reset_monthly": False,
                "min_cycle_shift_seconds": 259200,
                "rearm_remaining_seconds": 432000,
                "jitter_tolerance_seconds": 21600,
            },
        )
        self.assertEqual(("GPT订阅550每周", "3"), parsed.group_selectors)
        self.assertEqual((), parsed.group_ids)
        self.assertEqual((1, 2), parsed.account_ids)

    def test_native_config_migrates_legacy_account_id(self):
        base = Config.from_env({"ENABLED": "false"})
        parsed = Config.from_native_json(base, {"account_id": 7})
        self.assertEqual((7,), parsed.account_ids)

    def test_native_config_rejects_unknown_fields(self):
        base = Config.from_env({"ENABLED": "false"})
        with self.assertRaisesRegex(ValueError, "未知字段"):
            Config.from_native_json(base, {"unexpected": True})


class FakeAdminApi:
    def __init__(self, subscription, fail_after_reset=False):
        self.current = dict(subscription)
        self.fail_after_reset = fail_after_reset
        self.reset_calls = 0

    def subscription(self, subscription_id):
        if subscription_id != self.current["id"]:
            raise AssertionError("unexpected subscription")
        return dict(self.current)

    def reset_subscription(self, subscription_id, reset_config):
        self.reset_calls += 1
        if reset_config.reset_weekly:
            self.current["weekly_window_start"] = "2026-08-30T00:00:00+00:00"
        if self.fail_after_reset:
            self.fail_after_reset = False
            raise RuntimeError("simulated ambiguous response")
        return dict(self.current)


class StateStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.directory.name) / "state.sqlite3")
        self.snapshot = Snapshot(
            datetime(2026, 9, 6, 5, 22, tzinfo=UTC),
            datetime(2026, 8, 30, 5, 23, tzinfo=UTC),
            1.0,
            604740,
        )
        self.target = {
            "subscription_id": 10,
            "user_id": 20,
            "group_id": 4,
            "daily_window_start": "2026-08-29T00:00:00+00:00",
            "weekly_window_start": "2026-08-23T00:00:00+00:00",
            "monthly_window_start": "2026-08-01T00:00:00+00:00",
        }
        self.subscription = {
            "id": 10,
            "user_id": 20,
            "group_id": 4,
            "status": "active",
            "deleted_at": None,
            "expires_at": "2026-12-01T00:00:00+00:00",
            "daily_window_start": self.target["daily_window_start"],
            "weekly_window_start": self.target["weekly_window_start"],
            "monthly_window_start": self.target["monthly_window_start"],
        }

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_event_is_unique_and_state_advances_atomically(self):
        first = self.store.create_event(
            config(dry_run=False), 1, self.snapshot, [self.target]
        )
        second = self.store.create_event(
            config(dry_run=False), 1, self.snapshot, [self.target]
        )
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(self.snapshot.reset_at, self.store.load_state(1).reset_at)
        count = self.store.db.execute("SELECT count(*) FROM reset_events").fetchone()[0]
        self.assertEqual(1, count)

    def test_ambiguous_api_result_is_recovered_without_second_reset(self):
        event_id = self.store.create_event(
            config(dry_run=False), 1, self.snapshot, [self.target]
        )
        api = FakeAdminApi(self.subscription, fail_after_reset=True)

        process_pending_events(api, self.store, config(dry_run=False))
        self.assertEqual(1, api.reset_calls)
        self.assertEqual(
            "pending",
            self.store.db.execute(
                "SELECT status FROM reset_events WHERE id=?", (event_id,)
            ).fetchone()[0],
        )

        process_pending_events(api, self.store, config(dry_run=False))
        self.assertEqual(1, api.reset_calls)
        self.assertEqual(
            "complete",
            self.store.db.execute(
                "SELECT status FROM reset_events WHERE id=?", (event_id,)
            ).fetchone()[0],
        )

    def test_missing_window_marker_fails_closed(self):
        target = dict(self.target)
        target["weekly_window_start"] = None
        with self.assertRaisesRegex(RuntimeError, "无法幂等恢复"):
            self.store.create_event(config(dry_run=False), 1, self.snapshot, [target])


if __name__ == "__main__":
    unittest.main()
