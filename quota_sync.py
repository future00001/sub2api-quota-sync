#!/usr/bin/env python3
"""将 OpenAI 账号 7d 周期重置同步到指定订阅分组。"""

from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from glob import glob
from pathlib import Path
from typing import Iterable, Sequence


UTC = timezone.utc


def parse_bool(value: str, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError(f"{name} 必须是 true/false")


def parse_positive_int(value: str, name: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是正整数") from exc
    if result <= 0:
        raise ValueError(f"{name} 必须是正整数")
    return result


def parse_group_ids(value: str) -> tuple[int, ...]:
    if not value.strip():
        return ()
    result = tuple(dict.fromkeys(parse_positive_int(part.strip(), "GROUP_IDS") for part in value.split(",")))
    return result


def parse_duration(value: str, name: str) -> timedelta:
    try:
        seconds = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是整数秒") from exc
    if seconds < 0:
        raise ValueError(f"{name} 不能为负数")
    return timedelta(seconds=seconds)


def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("时间戳必须包含时区")
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class Config:
    enabled: bool
    dry_run: bool
    account_ids: tuple[int, ...]
    group_ids: tuple[int, ...]
    group_selectors: tuple[str, ...]
    reset_daily: bool
    reset_weekly: bool
    reset_monthly: bool
    min_cycle_shift: timedelta
    rearm_remaining: timedelta
    jitter_tolerance: timedelta
    postgres_container: str
    redis_container: str
    redis_db: int

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Config":
        source = os.environ if env is None else env
        enabled = parse_bool(source.get("ENABLED", "false"), "ENABLED")
        dry_run = parse_bool(source.get("DRY_RUN", "true"), "DRY_RUN")
        account_ids_raw = source.get("ACCOUNT_IDS", source.get("ACCOUNT_ID", "1"))
        account_ids = tuple(dict.fromkeys(parse_positive_int(part.strip(), "ACCOUNT_IDS") for part in account_ids_raw.split(",")))
        group_ids = parse_group_ids(source.get("GROUP_IDS", ""))
        group_selectors = tuple(str(group_id) for group_id in group_ids)
        reset_daily = parse_bool(source.get("RESET_DAILY", "false"), "RESET_DAILY")
        reset_weekly = parse_bool(source.get("RESET_WEEKLY", "true"), "RESET_WEEKLY")
        reset_monthly = parse_bool(source.get("RESET_MONTHLY", "false"), "RESET_MONTHLY")
        if enabled and not group_selectors:
            raise ValueError("启用时 GROUP_IDS 不能为空")
        if enabled and not (reset_daily or reset_weekly or reset_monthly):
            raise ValueError("至少要启用一个重置窗口")
        try:
            redis_db = int(source.get("REDIS_DB", "0"))
        except ValueError as exc:
            raise ValueError("REDIS_DB 必须是非负整数") from exc
        if redis_db < 0:
            raise ValueError("REDIS_DB 必须是非负整数")
        return cls(
            enabled=enabled,
            dry_run=dry_run,
            account_ids=account_ids,
            group_ids=group_ids,
            group_selectors=group_selectors,
            reset_daily=reset_daily,
            reset_weekly=reset_weekly,
            reset_monthly=reset_monthly,
            min_cycle_shift=parse_duration(source.get("MIN_CYCLE_SHIFT_SECONDS", "86400"), "MIN_CYCLE_SHIFT_SECONDS"),
            rearm_remaining=parse_duration(source.get("REARM_REMAINING_SECONDS", "432000"), "REARM_REMAINING_SECONDS"),
            jitter_tolerance=parse_duration(source.get("JITTER_TOLERANCE_SECONDS", "21600"), "JITTER_TOLERANCE_SECONDS"),
            postgres_container=source.get("POSTGRES_CONTAINER", "sub2api-postgres").strip(),
            redis_container=source.get("REDIS_CONTAINER", "sub2api-redis").strip(),
            redis_db=redis_db,
        )

    @classmethod
    def from_native_json(cls, base: "Config", value: object) -> "Config":
        if not isinstance(value, dict):
            raise ValueError("原生插件配置必须是 JSON 对象")
        allowed = {
            "enabled",
            "dry_run",
            "account_id",
            "account_ids",
            "catalog",
            "target_groups",
            "reset_daily",
            "reset_weekly",
            "reset_monthly",
            "min_cycle_shift_seconds",
            "rearm_remaining_seconds",
            "jitter_tolerance_seconds",
        }
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ValueError(f"原生插件配置包含未知字段: {', '.join(unknown)}")

        def bool_field(name: str, default: bool) -> bool:
            result = value.get(name, default)
            if not isinstance(result, bool):
                raise ValueError(f"{name} 必须是布尔值")
            return result

        def int_field(name: str, default: int, minimum: int = 0) -> int:
            result = value.get(name, default)
            if isinstance(result, bool) or not isinstance(result, int) or result < minimum:
                raise ValueError(f"{name} 必须是不小于 {minimum} 的整数")
            return result

        raw_groups = value.get("target_groups", list(base.group_selectors))
        if not isinstance(raw_groups, list) or any(not isinstance(item, str) for item in raw_groups):
            raise ValueError("target_groups 必须是名称或 ID 字符串数组")
        selectors = tuple(dict.fromkeys(item.strip() for item in raw_groups if item.strip()))
        enabled = bool_field("enabled", base.enabled)
        reset_daily = bool_field("reset_daily", base.reset_daily)
        reset_weekly = bool_field("reset_weekly", base.reset_weekly)
        reset_monthly = bool_field("reset_monthly", base.reset_monthly)
        if enabled and not selectors:
            raise ValueError("启用时至少要填写一个目标分组名称或 ID")
        if enabled and not (reset_daily or reset_weekly or reset_monthly):
            raise ValueError("启用时至少要选择一个重置窗口")
        raw_account_ids = value.get("account_ids")
        if raw_account_ids is None:
            raw_account_ids = [value.get("account_id", base.account_ids[0] if base.account_ids else 1)]
        if not isinstance(raw_account_ids, list):
            raise ValueError("account_ids 必须是账号 ID 数组")
        account_ids = []
        for item in raw_account_ids:
            if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
                raise ValueError("account_ids 必须只包含正整数")
            if item not in account_ids:
                account_ids.append(item)
        if enabled and not account_ids:
            raise ValueError("启用时至少要选择一个账号")
        return replace(
            base,
            enabled=enabled,
            dry_run=bool_field("dry_run", base.dry_run),
            account_ids=tuple(account_ids),
            group_ids=(),
            group_selectors=selectors,
            reset_daily=reset_daily,
            reset_weekly=reset_weekly,
            reset_monthly=reset_monthly,
            min_cycle_shift=timedelta(
                seconds=int_field("min_cycle_shift_seconds", int(base.min_cycle_shift.total_seconds()), 86400)
            ),
            rearm_remaining=timedelta(
                seconds=int_field("rearm_remaining_seconds", int(base.rearm_remaining.total_seconds()), 86400)
            ),
            jitter_tolerance=timedelta(
                seconds=int_field("jitter_tolerance_seconds", int(base.jitter_tolerance.total_seconds()), 0)
            ),
        )


DEFAULT_PLUGIN_CONFIG_GLOB = (
    "/opt/sub2api/deploy/data/plugins/installed/"
    "com.hzyhz.sub2api-quota-sync/*/runtimes/linux-amd64/quota-sync-config.json"
)


def load_config(env: dict[str, str] | None = None) -> tuple[Config, str]:
    source = os.environ if env is None else env
    base = Config.from_env(source)
    pattern = source.get("PLUGIN_CONFIG_GLOB", DEFAULT_PLUGIN_CONFIG_GLOB).strip()
    candidates = [Path(path) for path in glob(pattern) if Path(path).is_file()]
    if not candidates:
        return base, "environment"
    selected = max(candidates, key=lambda path: path.stat().st_mtime_ns)
    if selected.stat().st_size > 1024 * 1024:
        raise ValueError("原生插件配置文件超过 1 MiB")
    with selected.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    return Config.from_native_json(base, value), str(selected)


def write_executor_status(config_source: str, **payload: object) -> None:
    if config_source == "environment":
        return
    config_path = Path(config_source)
    try:
        config_stat = config_path.stat()
        config_mtime_ns = config_stat.st_mtime_ns
        status_path = config_path.with_name("quota-sync-status.json")
        status = {
            "config_mtime_ns": config_mtime_ns,
            "config_sha256": business_config_sha256(config_path),
            "checked_at": datetime.now(UTC).isoformat(),
            **payload,
        }
        temp_path = status_path.with_name(".quota-sync-status.tmp")
        temp_path.write_text(json.dumps(status, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        os.chown(temp_path, config_stat.st_uid, config_stat.st_gid)
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, status_path)
    except Exception as exc:
        print(f"status_write=failed error={exc}", file=sys.stderr)


def business_config_sha256(config_path: Path) -> str:
    value = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("插件配置必须是 JSON 对象")
    value.pop("catalog", None)
    canonical = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def write_plugin_json(config_source: str, filename: str, value: object) -> None:
    if config_source == "environment":
        return
    config_path = Path(config_source)
    config_stat = config_path.stat()
    target_path = config_path.with_name(filename)
    temp_path = target_path.with_name(f".{filename}.tmp")
    temp_path.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    os.chown(temp_path, config_stat.st_uid, config_stat.st_gid)
    os.chmod(temp_path, 0o600)
    os.replace(temp_path, target_path)


@dataclass(frozen=True)
class Snapshot:
    reset_at: datetime
    sample_at: datetime
    used_percent: float | None
    reset_after_seconds: int


@dataclass(frozen=True)
class State:
    reset_at: datetime
    sample_at: datetime
    used_percent: float | None


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str


def decide(state: State | None, snapshot: Snapshot, config: Config) -> Decision:
    if state is None:
        return Decision("baseline", "首次运行只建立基线")
    if snapshot.sample_at <= state.sample_at:
        return Decision("stale", "账号用量快照没有更新")
    shift = snapshot.reset_at - state.reset_at
    if abs(shift) <= config.jitter_tolerance:
        return Decision("observe", "仍处于同一 7d 周期")
    if shift < -config.jitter_tolerance:
        return Decision("suspicious", "7d 重置时间异常后退，拒绝自动重置")
    if shift < config.min_cycle_shift:
        return Decision("suspicious", "7d 重置时间变化不足一个新周期，拒绝自动重置")
    remaining = snapshot.reset_at - snapshot.sample_at
    if remaining < config.rearm_remaining or snapshot.reset_after_seconds < int(config.rearm_remaining.total_seconds()):
        return Decision("not_rearmed", "新快照尚未回到完整 7d 周期")
    return Decision("reset", "检测到 7d 倒计时进入新周期")


class DockerRuntime:
    def __init__(self, config: Config):
        self.config = config

    def _run(self, args: Sequence[str], *, input_text: str | None = None) -> str:
        completed = subprocess.run(
            list(args),
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or f"exit={completed.returncode}"
            raise RuntimeError(detail)
        return completed.stdout.strip()

    def psql_json(self, sql: str) -> list[dict]:
        output = self._run(
            [
                "docker",
                "exec",
                "-i",
                self.config.postgres_container,
                "sh",
                "-lc",
                'exec psql -q -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At',
            ],
            input_text=sql,
        )
        if not output:
            return []
        return [json.loads(line) for line in output.splitlines() if line.strip()]

    def redis(self, *args: str) -> str:
        return self._run(
            ["docker", "exec", self.config.redis_container, "redis-cli", "--no-auth-warning", "-n", str(self.config.redis_db), *args]
        )


def resolve_group_ids(runtime: DockerRuntime, selectors: tuple[str, ...]) -> tuple[int, ...]:
    if not selectors:
        return ()
    rows = runtime.psql_json(
        """
SELECT json_build_object(
  'id', id, 'name', name, 'status', status, 'subscription_type', subscription_type
)
FROM groups
WHERE deleted_at IS NULL
ORDER BY id;
"""
    )
    resolved: list[int] = []
    for selector in selectors:
        if selector.isdecimal():
            matches = [row for row in rows if int(row["id"]) == int(selector)]
        else:
            matches = [row for row in rows if str(row["name"]).casefold() == selector.casefold()]
        if not matches:
            raise ValueError(f"目标分组不存在: {selector}")
        if len(matches) != 1:
            raise ValueError(f"目标分组名称不唯一，请改用 ID: {selector}")
        match = matches[0]
        if match.get("status") != "active" or match.get("subscription_type") != "subscription":
            raise ValueError(f"目标不是有效的订阅分组: {selector}")
        resolved.append(int(match["id"]))
    return tuple(dict.fromkeys(resolved))


def load_catalog(runtime: DockerRuntime) -> dict[str, object]:
    accounts = runtime.psql_json(
        """
SELECT json_build_object(
  'id', id,
  'label', coalesce(nullif(extra->>'email', ''), name),
  'name', name,
  'status', status,
  'schedulable', schedulable
)
FROM accounts
WHERE deleted_at IS NULL AND platform = 'openai' AND type = 'oauth'
ORDER BY id;
"""
    )
    groups = runtime.psql_json(
        """
SELECT json_build_object('id', id, 'name', name)
FROM groups
WHERE deleted_at IS NULL AND status = 'active' AND subscription_type = 'subscription'
ORDER BY sort_order, id;
"""
    )
    return {
        "accounts": accounts,
        "groups": groups,
        "generated_at": datetime.now(UTC).isoformat(),
    }


SCHEMA_SQL = r"""
CREATE TABLE IF NOT EXISTS sub2api_quota_sync_state (
    account_id bigint PRIMARY KEY,
    cycle_reset_at timestamptz NOT NULL,
    sample_updated_at timestamptz NOT NULL,
    used_percent numeric NULL,
    initialized_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS sub2api_quota_sync_events (
    id bigserial PRIMARY KEY,
    account_id bigint NOT NULL,
    cycle_reset_at timestamptz NOT NULL,
    detected_at timestamptz NOT NULL DEFAULT now(),
    group_ids bigint[] NOT NULL,
    reset_daily boolean NOT NULL,
    reset_weekly boolean NOT NULL,
    reset_monthly boolean NOT NULL,
    affected_count integer NOT NULL DEFAULT 0,
    targets jsonb NOT NULL DEFAULT '[]'::jsonb,
    cache_status varchar(16) NOT NULL DEFAULT 'pending',
    cache_error text NULL,
    cache_updated_at timestamptz NULL,
    UNIQUE (account_id, cycle_reset_at)
);
"""


def sql_bool(value: bool) -> str:
    return "true" if value else "false"


def sql_ids(values: Iterable[int]) -> str:
    return ",".join(str(item) for item in values)


def load_snapshot(runtime: DockerRuntime, account_id: int) -> Snapshot:
    rows = runtime.psql_json(
        f"""
SELECT json_build_object(
  'reset_at', extra->>'codex_7d_reset_at',
  'sample_at', extra->>'codex_usage_updated_at',
  'used_percent', extra->>'codex_7d_used_percent',
  'reset_after_seconds', extra->>'codex_7d_reset_after_seconds'
)
FROM accounts
WHERE id = {account_id} AND deleted_at IS NULL AND platform = 'openai' AND type = 'oauth';
"""
    )
    if len(rows) != 1:
        raise RuntimeError(f"找不到 OpenAI OAuth 账号 {account_id}")
    row = rows[0]
    required = ("reset_at", "sample_at", "reset_after_seconds")
    if any(row.get(key) in (None, "") for key in required):
        raise RuntimeError("账号尚无完整的 codex 7d 用量快照")
    return Snapshot(
        reset_at=parse_timestamp(str(row["reset_at"])),
        sample_at=parse_timestamp(str(row["sample_at"])),
        used_percent=float(row["used_percent"]) if row.get("used_percent") not in (None, "") else None,
        reset_after_seconds=int(row["reset_after_seconds"]),
    )


def ensure_schema(runtime: DockerRuntime) -> None:
    runtime.psql_json(SCHEMA_SQL)


def load_state(runtime: DockerRuntime, account_id: int) -> State | None:
    rows = runtime.psql_json(
        f"""
SELECT json_build_object(
  'reset_at', cycle_reset_at,
  'sample_at', sample_updated_at,
  'used_percent', used_percent
)
FROM sub2api_quota_sync_state WHERE account_id = {account_id};
"""
    )
    if not rows:
        return None
    row = rows[0]
    return State(
        reset_at=parse_timestamp(str(row["reset_at"])),
        sample_at=parse_timestamp(str(row["sample_at"])),
        used_percent=float(row["used_percent"]) if row.get("used_percent") is not None else None,
    )


def write_observation(runtime: DockerRuntime, account_id: int, snapshot: Snapshot, *, update_cycle: bool) -> None:
    used = "NULL" if snapshot.used_percent is None else str(snapshot.used_percent)
    reset_expr = "EXCLUDED.cycle_reset_at" if update_cycle else "sub2api_quota_sync_state.cycle_reset_at"
    runtime.psql_json(
        f"""
INSERT INTO sub2api_quota_sync_state(account_id, cycle_reset_at, sample_updated_at, used_percent)
VALUES ({account_id}, '{snapshot.reset_at.isoformat()}', '{snapshot.sample_at.isoformat()}', {used})
ON CONFLICT (account_id) DO UPDATE SET
  cycle_reset_at = {reset_expr},
  sample_updated_at = EXCLUDED.sample_updated_at,
  used_percent = EXCLUDED.used_percent,
  updated_at = now();
"""
    )


def eligible_targets(runtime: DockerRuntime, group_ids: tuple[int, ...]) -> list[dict]:
    return runtime.psql_json(
        f"""
SELECT json_build_object('subscription_id', id, 'user_id', user_id, 'group_id', group_id)
FROM user_subscriptions
WHERE group_id IN ({sql_ids(group_ids)})
  AND status = 'active' AND deleted_at IS NULL AND expires_at > now()
ORDER BY group_id, user_id;
"""
    )


def execute_reset(runtime: DockerRuntime, config: Config, account_id: int, snapshot: Snapshot) -> dict:
    used = "NULL" if snapshot.used_percent is None else str(snapshot.used_percent)
    daily = sql_bool(config.reset_daily)
    weekly = sql_bool(config.reset_weekly)
    monthly = sql_bool(config.reset_monthly)
    rows = runtime.psql_json(
        f"""
BEGIN;
SELECT pg_advisory_xact_lock(20260829, {account_id});
WITH target_rows AS MATERIALIZED (
  SELECT us.id AS subscription_id, us.user_id, us.group_id
  FROM user_subscriptions us
  WHERE us.group_id IN ({sql_ids(config.group_ids)})
    AND us.status = 'active' AND us.deleted_at IS NULL AND us.expires_at > now()
), new_event AS (
  INSERT INTO sub2api_quota_sync_events(
    account_id, cycle_reset_at, group_ids, reset_daily, reset_weekly, reset_monthly,
    affected_count, targets
  ) VALUES (
    {account_id}, '{snapshot.reset_at.isoformat()}', ARRAY[{sql_ids(config.group_ids)}]::bigint[],
    {daily}, {weekly}, {monthly},
    (SELECT count(*)::integer FROM target_rows),
    (SELECT coalesce(jsonb_agg(jsonb_build_object(
      'subscription_id', subscription_id, 'user_id', user_id, 'group_id', group_id
    ) ORDER BY group_id, user_id), '[]'::jsonb) FROM target_rows)
  )
  ON CONFLICT (account_id, cycle_reset_at) DO NOTHING
  RETURNING id, affected_count, targets
), changed AS (
  UPDATE user_subscriptions us SET
    daily_usage_usd = CASE WHEN {daily} THEN 0 ELSE daily_usage_usd END,
    daily_window_start = CASE WHEN {daily} THEN date_trunc('day', now()) ELSE daily_window_start END,
    weekly_usage_usd = CASE WHEN {weekly} THEN 0 ELSE weekly_usage_usd END,
    weekly_window_start = CASE WHEN {weekly} THEN '{snapshot.reset_at.isoformat()}'::timestamptz - interval '7 days' ELSE weekly_window_start END,
    monthly_usage_usd = CASE WHEN {monthly} THEN 0 ELSE monthly_usage_usd END,
    monthly_window_start = CASE WHEN {monthly} THEN now() ELSE monthly_window_start END,
    updated_at = now()
  FROM new_event
  WHERE us.id IN (SELECT subscription_id FROM target_rows)
  RETURNING new_event.id AS event_id, us.id AS subscription_id, us.user_id, us.group_id
), state_upsert AS (
  INSERT INTO sub2api_quota_sync_state(account_id, cycle_reset_at, sample_updated_at, used_percent)
  VALUES ({account_id}, '{snapshot.reset_at.isoformat()}', '{snapshot.sample_at.isoformat()}', {used})
  ON CONFLICT (account_id) DO UPDATE SET
    cycle_reset_at = EXCLUDED.cycle_reset_at,
    sample_updated_at = EXCLUDED.sample_updated_at,
    used_percent = EXCLUDED.used_percent,
    updated_at = now()
  RETURNING account_id
)
SELECT json_build_object(
  'event_id', (SELECT id FROM new_event),
  'affected_count', coalesce((SELECT affected_count FROM new_event), 0),
  'changed_count', (SELECT count(*) FROM changed),
  'targets', coalesce((SELECT targets FROM new_event), '[]'::jsonb),
  'state_account_id', (SELECT account_id FROM state_upsert)
);
COMMIT;
"""
    )
    result = next((row for row in rows if isinstance(row, dict) and "state_account_id" in row), None)
    if result is None:
        raise RuntimeError("重置事务没有返回结果")
    return result


def load_pending_events(runtime: DockerRuntime) -> list[dict]:
    return runtime.psql_json(
        """
SELECT json_build_object('event_id', id, 'targets', targets)
FROM sub2api_quota_sync_events
WHERE cache_status = 'pending'
ORDER BY id;
"""
    )


def finish_cache_event(runtime: DockerRuntime, event_id: int, error: str | None = None) -> None:
    if error is None:
        runtime.psql_json(
            f"UPDATE sub2api_quota_sync_events SET cache_status='done', cache_error=NULL, cache_updated_at=now() WHERE id={event_id};"
        )
        return
    escaped = error.replace("'", "''")[:1000]
    runtime.psql_json(
        f"UPDATE sub2api_quota_sync_events SET cache_error='{escaped}', cache_updated_at=now() WHERE id={event_id};"
    )


def invalidate_pending_events(runtime: DockerRuntime) -> None:
    for event in load_pending_events(runtime):
        event_id = int(event["event_id"])
        try:
            for target in event.get("targets") or []:
                user_id = int(target["user_id"])
                group_id = int(target["group_id"])
                runtime.redis("DEL", f"billing:sub:{user_id}:{group_id}")
                runtime.redis("PUBLISH", "subscription:cache:invalidate", f"sub:{user_id}:{group_id}")
            finish_cache_event(runtime, event_id)
            print(f"cache_invalidation=done event_id={event_id}")
        except Exception as exc:  # 下一轮定时任务会重试
            finish_cache_event(runtime, event_id, str(exc))
            print(f"cache_invalidation=pending event_id={event_id} error={exc}", file=sys.stderr)


def main() -> int:
    config_source = "environment"
    try:
        config, config_source = load_config()
        runtime = DockerRuntime(config)
        catalog = load_catalog(runtime)
        write_plugin_json(config_source, "quota-sync-catalog.json", catalog)
        if not config.enabled:
            write_executor_status(config_source, ok=True, enabled=False, message="业务开关关闭")
            print(f"status=disabled config_source={config_source}")
            return 0
        config = replace(config, group_ids=resolve_group_ids(runtime, config.group_selectors))
        validation_targets = eligible_targets(runtime, config.group_ids)
        write_executor_status(
            config_source,
            ok=True,
            enabled=True,
            dry_run=config.dry_run,
            selected_account_ids=list(config.account_ids),
            resolved_group_ids=list(config.group_ids),
            eligible_subscriptions=len(validation_targets),
            message="真实分组校验通过",
        )
        print(
            f"config_source={config_source} account_ids={config.account_ids} "
            f"target_groups={config.group_selectors} resolved_group_ids={config.group_ids} dry_run={config.dry_run}"
        )
        ensure_schema(runtime)
        invalidate_pending_events(runtime)
        exit_code = 0
        for account_id in config.account_ids:
            snapshot = load_snapshot(runtime, account_id)
            state = load_state(runtime, account_id)
            decision = decide(state, snapshot, config)
            print(
                f"action={decision.action} account_id={account_id} "
                f"reset_at={snapshot.reset_at.isoformat()} sample_at={snapshot.sample_at.isoformat()} reason={decision.reason}"
            )
            if decision.action == "baseline":
                write_observation(runtime, account_id, snapshot, update_cycle=True)
                continue
            if decision.action in {"stale", "not_rearmed"}:
                continue
            if decision.action == "observe":
                write_observation(runtime, account_id, snapshot, update_cycle=True)
                continue
            if decision.action == "suspicious":
                write_observation(runtime, account_id, snapshot, update_cycle=False)
                exit_code = 2
                continue
            if config.dry_run:
                print(f"dry_run=true account_id={account_id} eligible_subscriptions={len(validation_targets)} group_ids={config.group_ids}")
                continue
            result = execute_reset(runtime, config, account_id, snapshot)
            print(
                f"reset=done account_id={account_id} event_id={result.get('event_id')} "
                f"affected_count={result.get('affected_count')} group_ids={config.group_ids}"
            )
        invalidate_pending_events(runtime)
        return exit_code
    except Exception as exc:
        write_executor_status(config_source, ok=False, error=str(exc), message="后台执行器校验失败")
        print(f"status=error error={exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
