import unittest
from datetime import datetime, timedelta, timezone

from quota_sync import Config, Snapshot, State, decide


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
        "postgres_container": "sub2api-postgres",
        "redis_container": "sub2api-redis",
        "redis_db": 0,
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
        state = State(self.reset_at - timedelta(seconds=15), self.sample_at - timedelta(minutes=1), 29.0)
        self.assertEqual("observe", decide(state, self.snapshot, config()).action)

    def test_full_cycle_advance_triggers_reset(self):
        state = State(self.reset_at - timedelta(days=7), self.sample_at - timedelta(minutes=1), 98.0)
        self.assertEqual("reset", decide(state, self.snapshot, config()).action)

    def test_cycle_advance_must_be_rearmed(self):
        snapshot = Snapshot(self.sample_at + timedelta(days=2), self.sample_at, 2.0, 172800)
        state = State(snapshot.reset_at - timedelta(days=7), self.sample_at - timedelta(minutes=1), 98.0)
        self.assertEqual("not_rearmed", decide(state, snapshot, config()).action)

    def test_backward_reset_at_is_suspicious(self):
        state = State(self.reset_at + timedelta(days=1), self.sample_at - timedelta(minutes=1), 29.0)
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
        parsed = Config.from_env({"ENABLED": "true", "GROUP_IDS": "4,3,4"})
        self.assertEqual((4, 3), parsed.group_ids)

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


if __name__ == "__main__":
    unittest.main()
