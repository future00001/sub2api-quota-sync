#!/usr/bin/env python3
"""将 OpenAI 账号 7d 周期重置同步到指定订阅分组。"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from glob import glob
from pathlib import Path
from typing import Any

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
    result = tuple(
        dict.fromkeys(
            parse_positive_int(part.strip(), "GROUP_IDS") for part in value.split(",")
        )
    )
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


def normalize_base_url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("SUB2API_BASE_URL 必须是有效的 HTTP(S) 地址")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("SUB2API_BASE_URL 不能包含凭据、查询参数或片段")
    return normalized


def load_admin_api_key(source: dict[str, str]) -> str:
    direct = source.get("SUB2API_ADMIN_API_KEY", "").strip()
    key_file = source.get("SUB2API_ADMIN_API_KEY_FILE", "").strip()
    if direct and key_file:
        raise ValueError(
            "SUB2API_ADMIN_API_KEY 与 SUB2API_ADMIN_API_KEY_FILE 不能同时设置"
        )
    if not key_file:
        return direct
    path = Path(key_file)
    if path.stat().st_size > 16 * 1024:
        raise ValueError("Admin API Key 文件超过 16 KiB")
    return path.read_text(encoding="utf-8").strip()


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
    base_url: str
    admin_api_key: str
    request_timeout_seconds: int
    state_path: Path

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
        source = os.environ if env is None else env
        enabled = parse_bool(source.get("ENABLED", "false"), "ENABLED")
        dry_run = parse_bool(source.get("DRY_RUN", "true"), "DRY_RUN")
        account_ids_raw = source.get("ACCOUNT_IDS", source.get("ACCOUNT_ID", "1"))
        account_ids = tuple(
            dict.fromkeys(
                parse_positive_int(part.strip(), "ACCOUNT_IDS")
                for part in account_ids_raw.split(",")
            )
        )
        group_ids = parse_group_ids(source.get("GROUP_IDS", ""))
        group_selectors = tuple(str(group_id) for group_id in group_ids)
        reset_daily = parse_bool(source.get("RESET_DAILY", "false"), "RESET_DAILY")
        reset_weekly = parse_bool(source.get("RESET_WEEKLY", "true"), "RESET_WEEKLY")
        reset_monthly = parse_bool(
            source.get("RESET_MONTHLY", "false"), "RESET_MONTHLY"
        )
        if enabled and not group_selectors:
            raise ValueError("启用时 GROUP_IDS 不能为空")
        if enabled and not (reset_weekly or reset_monthly):
            raise ValueError(
                "Admin API 模式至少要启用周或月窗口；仅日窗口无法安全恢复不明确请求"
            )
        base_url = normalize_base_url(
            source.get("SUB2API_BASE_URL", "http://127.0.0.1:18080")
        )
        admin_api_key = load_admin_api_key(source)
        if enabled and not admin_api_key:
            raise ValueError("启用时 SUB2API_ADMIN_API_KEY 不能为空")
        return cls(
            enabled=enabled,
            dry_run=dry_run,
            account_ids=account_ids,
            group_ids=group_ids,
            group_selectors=group_selectors,
            reset_daily=reset_daily,
            reset_weekly=reset_weekly,
            reset_monthly=reset_monthly,
            min_cycle_shift=parse_duration(
                source.get("MIN_CYCLE_SHIFT_SECONDS", "86400"),
                "MIN_CYCLE_SHIFT_SECONDS",
            ),
            rearm_remaining=parse_duration(
                source.get("REARM_REMAINING_SECONDS", "432000"),
                "REARM_REMAINING_SECONDS",
            ),
            jitter_tolerance=parse_duration(
                source.get("JITTER_TOLERANCE_SECONDS", "21600"),
                "JITTER_TOLERANCE_SECONDS",
            ),
            base_url=base_url,
            admin_api_key=admin_api_key,
            request_timeout_seconds=parse_positive_int(
                source.get("REQUEST_TIMEOUT_SECONDS", "15"), "REQUEST_TIMEOUT_SECONDS"
            ),
            state_path=Path(
                source.get("STATE_PATH", "/var/lib/sub2api-quota-sync/state.sqlite3")
            ),
        )

    @classmethod
    def from_native_json(cls, base: Config, value: object) -> Config:
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
            if (
                isinstance(result, bool)
                or not isinstance(result, int)
                or result < minimum
            ):
                raise ValueError(f"{name} 必须是不小于 {minimum} 的整数")
            return result

        raw_groups = value.get("target_groups", list(base.group_selectors))
        if not isinstance(raw_groups, list) or any(
            not isinstance(item, str) for item in raw_groups
        ):
            raise ValueError("target_groups 必须是名称或 ID 字符串数组")
        selectors = tuple(
            dict.fromkeys(item.strip() for item in raw_groups if item.strip())
        )
        enabled = bool_field("enabled", base.enabled)
        reset_daily = bool_field("reset_daily", base.reset_daily)
        reset_weekly = bool_field("reset_weekly", base.reset_weekly)
        reset_monthly = bool_field("reset_monthly", base.reset_monthly)
        if enabled and not selectors:
            raise ValueError("启用时至少要填写一个目标分组名称或 ID")
        if enabled and not (reset_weekly or reset_monthly):
            raise ValueError("Admin API 模式至少要选择周或月窗口；日窗口可以与其组合")
        raw_account_ids = value.get("account_ids")
        if raw_account_ids is None:
            raw_account_ids = [
                value.get("account_id", base.account_ids[0] if base.account_ids else 1)
            ]
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
                seconds=int_field(
                    "min_cycle_shift_seconds",
                    int(base.min_cycle_shift.total_seconds()),
                    86400,
                )
            ),
            rearm_remaining=timedelta(
                seconds=int_field(
                    "rearm_remaining_seconds",
                    int(base.rearm_remaining.total_seconds()),
                    86400,
                )
            ),
            jitter_tolerance=timedelta(
                seconds=int_field(
                    "jitter_tolerance_seconds",
                    int(base.jitter_tolerance.total_seconds()),
                    0,
                )
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


def atomic_write_json(target_path: Path, value: object, owner: os.stat_result) -> None:
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{target_path.name}.", dir=target_path.parent
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), 0o600)
            try:
                os.fchown(handle.fileno(), owner.st_uid, owner.st_gid)
            except PermissionError:
                geteuid = getattr(os, "geteuid", lambda: owner.st_uid)
                getegid = getattr(os, "getegid", lambda: owner.st_gid)
                if geteuid() != owner.st_uid or getegid() != owner.st_gid:
                    raise
        os.replace(temp_path, target_path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


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
        atomic_write_json(status_path, status, config_stat)
    except Exception as exc:
        print(f"status_write=failed error={exc}", file=sys.stderr)


def business_config_sha256(config_path: Path) -> str:
    value = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("插件配置必须是 JSON 对象")
    value.pop("catalog", None)
    canonical = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(canonical).hexdigest()


def write_plugin_json(config_source: str, filename: str, value: object) -> None:
    if config_source == "environment":
        return
    config_path = Path(config_source)
    config_stat = config_path.stat()
    atomic_write_json(config_path.with_name(filename), value, config_stat)


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
    if remaining < config.rearm_remaining or snapshot.reset_after_seconds < int(
        config.rearm_remaining.total_seconds()
    ):
        return Decision("not_rearmed", "新快照尚未回到完整 7d 周期")
    return Decision("reset", "检测到 7d 倒计时进入新周期")


class ApiError(RuntimeError):
    def __init__(self, method: str, path: str, status: int, detail: str = "") -> None:
        self.method = method
        self.path = path
        self.status = status
        message = f"{method} {path} 返回 HTTP {status}"
        if detail:
            message += f": {detail}"
        super().__init__(message)


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


class AdminApi:
    max_response_bytes = 4 * 1024 * 1024

    def __init__(self, config: Config):
        self.config = config
        self.opener = urllib.request.build_opener(NoRedirectHandler())

    def request(
        self, method: str, path: str, body: dict[str, object] | None = None
    ) -> Any:
        if not self.config.admin_api_key:
            raise RuntimeError("SUB2API_ADMIN_API_KEY 尚未配置")
        data = None
        headers = {
            "Accept": "application/json",
            "User-Agent": "sub2api-quota-sync/0.4",
            "x-api-key": self.config.admin_api_key,
        }
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        # base_url 在配置解析阶段已限制为 HTTP(S)，并拒绝凭据和片段。
        request = urllib.request.Request(  # noqa: S310
            f"{self.config.base_url}/api/v1{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with self.opener.open(
                request, timeout=self.config.request_timeout_seconds
            ) as response:
                length = response.headers.get("Content-Length")
                if length and int(length) > self.max_response_bytes:
                    raise RuntimeError("Sub2API 响应超过 4 MiB")
                raw = response.read(self.max_response_bytes + 1)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                payload = json.loads(exc.read(4096).decode("utf-8"))
                if isinstance(payload, dict):
                    detail = str(payload.get("message") or payload.get("error") or "")[
                        :200
                    ]
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
            raise ApiError(method, path, exc.code, detail) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"{method} {path} 请求失败: {exc}") from exc
        if len(raw) > self.max_response_bytes:
            raise RuntimeError("Sub2API 响应超过 4 MiB")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"{method} {path} 返回了无效 JSON") from exc
        if isinstance(payload, dict) and "code" in payload:
            if payload.get("code") not in (0, "0", None):
                raise RuntimeError(
                    str(payload.get("message") or "Sub2API API 错误")[:200]
                )
            return payload.get("data")
        return payload

    def paginated(self, path: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for page in range(1, 101):
            separator = "&" if "?" in path else "?"
            payload = self.request("GET", f"{path}{separator}page={page}&page_size=200")
            if isinstance(payload, list):
                result.extend(item for item in payload if isinstance(item, dict))
                break
            if not isinstance(payload, dict) or not isinstance(
                payload.get("items"), list
            ):
                raise RuntimeError(f"{path} 分页响应格式无效")
            items = [item for item in payload["items"] if isinstance(item, dict)]
            result.extend(items)
            pagination = (
                payload.get("pagination")
                if isinstance(payload.get("pagination"), dict)
                else payload
            )
            pages = int(pagination.get("pages", page))
            if page >= pages or not items:
                break
        else:
            raise RuntimeError(f"{path} 分页超过 100 页")
        return result

    def accounts(self) -> list[dict[str, Any]]:
        return self.paginated("/admin/accounts?platform=openai")

    def groups(self) -> list[dict[str, Any]]:
        payload = self.request("GET", "/admin/groups/all?include_inactive=true")
        if not isinstance(payload, list):
            raise RuntimeError("分组响应格式无效")
        return [item for item in payload if isinstance(item, dict)]

    def account(self, account_id: int) -> dict[str, Any]:
        payload = self.request("GET", f"/admin/accounts/{account_id}")
        if not isinstance(payload, dict):
            raise RuntimeError(f"账号 {account_id} 响应格式无效")
        return payload

    def subscriptions(self, group_ids: tuple[int, ...]) -> list[dict[str, Any]]:
        targets: dict[int, dict[str, Any]] = {}
        for group_id in group_ids:
            for item in self.paginated(f"/admin/groups/{group_id}/subscriptions"):
                try:
                    targets[int(item["id"])] = item
                except (KeyError, TypeError, ValueError):
                    continue
        return [targets[key] for key in sorted(targets)]

    def subscription(self, subscription_id: int) -> dict[str, Any]:
        payload = self.request("GET", f"/admin/subscriptions/{subscription_id}")
        if not isinstance(payload, dict):
            raise RuntimeError(f"订阅 {subscription_id} 响应格式无效")
        return payload

    def reset_subscription(
        self, subscription_id: int, config: Config
    ) -> dict[str, Any]:
        payload = self.request(
            "POST",
            f"/admin/subscriptions/{subscription_id}/reset-quota",
            {
                "daily": config.reset_daily,
                "weekly": config.reset_weekly,
                "monthly": config.reset_monthly,
            },
        )
        if not isinstance(payload, dict):
            raise RuntimeError(f"订阅 {subscription_id} 重置响应格式无效")
        return payload


def resolve_group_ids(api: AdminApi, selectors: tuple[str, ...]) -> tuple[int, ...]:
    rows = api.groups()
    resolved: list[int] = []
    for selector in selectors:
        if selector.isdecimal():
            matches = [row for row in rows if int(row.get("id", 0)) == int(selector)]
        else:
            matches = [
                row
                for row in rows
                if str(row.get("name", "")).casefold() == selector.casefold()
            ]
        if not matches:
            raise ValueError(f"目标分组不存在: {selector}")
        if len(matches) != 1:
            raise ValueError(f"目标分组名称不唯一，请改用 ID: {selector}")
        match = matches[0]
        if (
            match.get("status") != "active"
            or match.get("subscription_type") != "subscription"
        ):
            raise ValueError(f"目标不是有效的订阅分组: {selector}")
        resolved.append(int(match["id"]))
    return tuple(dict.fromkeys(resolved))


def load_catalog(api: AdminApi) -> dict[str, object]:
    accounts = []
    for account in api.accounts():
        if account.get("platform") != "openai" or account.get("type") != "oauth":
            continue
        extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
        accounts.append(
            {
                "id": int(account["id"]),
                "label": extra.get("email")
                or account.get("name")
                or f"账号 {account['id']}",
                "name": account.get("name") or "",
                "status": account.get("status") or "",
                "schedulable": bool(account.get("schedulable")),
            }
        )
    groups = [
        {"id": int(group["id"]), "name": str(group.get("name") or "")}
        for group in api.groups()
        if group.get("status") == "active"
        and group.get("subscription_type") == "subscription"
    ]
    return {
        "accounts": accounts,
        "groups": groups,
        "generated_at": datetime.now(UTC).isoformat(),
    }


def load_snapshot(api: AdminApi, account_id: int) -> Snapshot:
    account = api.account(account_id)
    if account.get("platform") != "openai" or account.get("type") != "oauth":
        raise RuntimeError(f"找不到 OpenAI OAuth 账号 {account_id}")
    extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
    required = (
        "codex_7d_reset_at",
        "codex_usage_updated_at",
        "codex_7d_reset_after_seconds",
    )
    if any(extra.get(key) in (None, "") for key in required):
        raise RuntimeError(f"账号 {account_id} 尚无完整的 codex 7d 用量快照")
    snapshot = Snapshot(
        reset_at=parse_timestamp(str(extra["codex_7d_reset_at"])),
        sample_at=parse_timestamp(str(extra["codex_usage_updated_at"])),
        used_percent=float(extra["codex_7d_used_percent"])
        if extra.get("codex_7d_used_percent") not in (None, "")
        else None,
        reset_after_seconds=int(extra["codex_7d_reset_after_seconds"]),
    )
    if snapshot.used_percent is not None and (
        not math.isfinite(snapshot.used_percent)
        or not 0 <= snapshot.used_percent <= 100
    ):
        raise RuntimeError(f"账号 {account_id} 的 codex 7d 用量百分比无效")
    if snapshot.reset_after_seconds < 0:
        raise RuntimeError(f"账号 {account_id} 的 codex 7d 剩余时间无效")
    return snapshot


def subscription_is_eligible(
    subscription: dict[str, Any], group_ids: tuple[int, ...]
) -> bool:
    try:
        if (
            int(subscription.get("group_id", 0)) not in group_ids
            or subscription.get("status") != "active"
        ):
            return False
    except (TypeError, ValueError):
        return False
    if subscription.get("deleted_at") not in (None, ""):
        return False
    expires_at = subscription.get("expires_at")
    if expires_at not in (None, ""):
        try:
            if parse_timestamp(str(expires_at)) <= datetime.now(UTC):
                return False
        except ValueError:
            return False
    return True


def eligible_targets(api: AdminApi, group_ids: tuple[int, ...]) -> list[dict[str, Any]]:
    result = []
    for item in api.subscriptions(group_ids):
        if not subscription_is_eligible(item, group_ids):
            continue
        result.append(
            {
                "subscription_id": int(item["id"]),
                "user_id": int(item["user_id"]),
                "group_id": int(item["group_id"]),
                "daily_window_start": item.get("daily_window_start"),
                "weekly_window_start": item.get("weekly_window_start"),
                "monthly_window_start": item.get("monthly_window_start"),
            }
        )
    return result


class StateStore:
    def __init__(self, path: Path):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=5)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS account_state (
                account_id INTEGER PRIMARY KEY,
                cycle_reset_at TEXT NOT NULL,
                sample_updated_at TEXT NOT NULL,
                used_percent REAL,
                initialized_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reset_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT NOT NULL UNIQUE,
                account_id INTEGER NOT NULL,
                cycle_reset_at TEXT NOT NULL,
                detected_at TEXT NOT NULL,
                group_ids TEXT NOT NULL,
                reset_daily INTEGER NOT NULL,
                reset_weekly INTEGER NOT NULL,
                reset_monthly INTEGER NOT NULL,
                affected_count INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                last_error TEXT NOT NULL DEFAULT '',
                completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS reset_targets (
                event_id INTEGER NOT NULL,
                subscription_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                group_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                previous_daily_window_start TEXT,
                previous_weekly_window_start TEXT,
                previous_monthly_window_start TEXT,
                last_error TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (event_id, subscription_id),
                FOREIGN KEY (event_id) REFERENCES reset_events(id)
            );
            """
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def load_state(self, account_id: int) -> State | None:
        row = self.db.execute(
            "SELECT * FROM account_state WHERE account_id=?", (account_id,)
        ).fetchone()
        if row is None:
            return None
        return State(
            reset_at=parse_timestamp(row["cycle_reset_at"]),
            sample_at=parse_timestamp(row["sample_updated_at"]),
            used_percent=float(row["used_percent"])
            if row["used_percent"] is not None
            else None,
        )

    def write_observation(
        self, account_id: int, snapshot: Snapshot, *, update_cycle: bool
    ) -> None:
        existing = self.load_state(account_id)
        cycle_reset_at = (
            snapshot.reset_at if update_cycle or existing is None else existing.reset_at
        )
        now = datetime.now(UTC).isoformat()
        self.db.execute(
            """
            INSERT INTO account_state(
                account_id, cycle_reset_at, sample_updated_at, used_percent,
                initialized_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id) DO UPDATE SET
                cycle_reset_at=excluded.cycle_reset_at,
                sample_updated_at=excluded.sample_updated_at,
                used_percent=excluded.used_percent,
                updated_at=excluded.updated_at
            """,
            (
                account_id,
                cycle_reset_at.isoformat(),
                snapshot.sample_at.isoformat(),
                snapshot.used_percent,
                now,
                now,
            ),
        )
        self.db.commit()

    def create_event(
        self,
        config: Config,
        account_id: int,
        snapshot: Snapshot,
        targets: list[dict[str, Any]],
    ) -> int | None:
        for target in targets:
            for enabled, name in (
                (config.reset_weekly, "weekly_window_start"),
                (config.reset_monthly, "monthly_window_start"),
            ):
                if enabled and target.get(name) in (None, ""):
                    raise RuntimeError(
                        f"订阅 {target['subscription_id']} 缺少 {name}，"
                        "拒绝执行无法幂等恢复的重置"
                    )
        event_key = hashlib.sha256(
            f"{account_id}\0{snapshot.reset_at.isoformat()}".encode()
        ).hexdigest()
        now = datetime.now(UTC).isoformat()
        try:
            self.db.execute("BEGIN IMMEDIATE")
            cursor = self.db.execute(
                """
                INSERT INTO reset_events(
                    event_key, account_id, cycle_reset_at, detected_at, group_ids,
                    reset_daily, reset_weekly, reset_monthly, affected_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_key,
                    account_id,
                    snapshot.reset_at.isoformat(),
                    now,
                    json.dumps(config.group_ids),
                    int(config.reset_daily),
                    int(config.reset_weekly),
                    int(config.reset_monthly),
                    len(targets),
                ),
            )
            event_id = int(cursor.lastrowid)
            for target in targets:
                self.db.execute(
                    """
                    INSERT INTO reset_targets(
                        event_id, subscription_id, user_id, group_id,
                        previous_daily_window_start, previous_weekly_window_start,
                        previous_monthly_window_start, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        target["subscription_id"],
                        target["user_id"],
                        target["group_id"],
                        target.get("daily_window_start"),
                        target.get("weekly_window_start"),
                        target.get("monthly_window_start"),
                        now,
                    ),
                )
            self._upsert_state(account_id, snapshot, now)
            self.db.commit()
            return event_id
        except sqlite3.IntegrityError:
            self.db.rollback()
            self.write_observation(account_id, snapshot, update_cycle=True)
            return None
        except Exception:
            self.db.rollback()
            raise

    def _upsert_state(self, account_id: int, snapshot: Snapshot, now: str) -> None:
        self.db.execute(
            """
            INSERT INTO account_state(
                account_id, cycle_reset_at, sample_updated_at, used_percent,
                initialized_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id) DO UPDATE SET
                cycle_reset_at=excluded.cycle_reset_at,
                sample_updated_at=excluded.sample_updated_at,
                used_percent=excluded.used_percent,
                updated_at=excluded.updated_at
            """,
            (
                account_id,
                snapshot.reset_at.isoformat(),
                snapshot.sample_at.isoformat(),
                snapshot.used_percent,
                now,
                now,
            ),
        )

    def pending_events(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM reset_events WHERE status!='complete' ORDER BY id"
        ).fetchall()

    def pending_targets(self, event_id: int) -> list[sqlite3.Row]:
        return self.db.execute(
            """SELECT * FROM reset_targets
               WHERE event_id=? AND status NOT IN ('done','skipped')
               ORDER BY subscription_id""",
            (event_id,),
        ).fetchall()

    def mark_target(
        self, event_id: int, subscription_id: int, status: str, error: str = ""
    ) -> None:
        self.db.execute(
            """UPDATE reset_targets SET status=?, last_error=?, updated_at=?
               WHERE event_id=? AND subscription_id=?""",
            (
                status,
                error[:500],
                datetime.now(UTC).isoformat(),
                event_id,
                subscription_id,
            ),
        )
        self.db.commit()

    def finish_event(self, event_id: int) -> None:
        remaining = self.db.execute(
            """SELECT count(*) FROM reset_targets
               WHERE event_id=? AND status NOT IN ('done','skipped')""",
            (event_id,),
        ).fetchone()[0]
        if remaining == 0:
            self.db.execute(
                """UPDATE reset_events
                   SET status='complete', last_error='', completed_at=?
                   WHERE id=?""",
                (datetime.now(UTC).isoformat(), event_id),
            )
        else:
            self.db.execute(
                """UPDATE reset_events
                   SET status='pending', last_error='部分订阅等待重试'
                   WHERE id=?""",
                (event_id,),
            )
        self.db.commit()


def reset_config_from_event(base: Config, event: sqlite3.Row) -> Config:
    return replace(
        base,
        group_ids=tuple(int(item) for item in json.loads(event["group_ids"])),
        reset_daily=bool(event["reset_daily"]),
        reset_weekly=bool(event["reset_weekly"]),
        reset_monthly=bool(event["reset_monthly"]),
    )


def selected_windows_changed(
    subscription: dict[str, Any], target: sqlite3.Row, config: Config
) -> bool:
    selected = []
    for enabled, name in (
        (config.reset_weekly, "weekly_window_start"),
        (config.reset_monthly, "monthly_window_start"),
    ):
        if enabled:
            before = target[f"previous_{name}"]
            after = subscription.get(name)
            selected.append(
                before not in (None, "")
                and after not in (None, "")
                and str(before) != str(after)
            )
    return bool(selected) and all(selected)


def process_pending_events(
    api: AdminApi, store: StateStore, base_config: Config
) -> None:
    for event in store.pending_events():
        event_id = int(event["id"])
        config = reset_config_from_event(base_config, event)
        for target in store.pending_targets(event_id):
            subscription_id = int(target["subscription_id"])
            try:
                subscription = api.subscription(subscription_id)
                if not subscription_is_eligible(subscription, config.group_ids):
                    store.mark_target(
                        event_id,
                        subscription_id,
                        "skipped",
                        "订阅不再有效或已移出目标分组",
                    )
                    continue
                if selected_windows_changed(subscription, target, config):
                    store.mark_target(event_id, subscription_id, "done")
                    print(
                        f"subscription_reset=recovered event_id={event_id} "
                        f"subscription_id={subscription_id}"
                    )
                    continue
                store.mark_target(event_id, subscription_id, "processing")
                updated = api.reset_subscription(subscription_id, config)
                if not selected_windows_changed(updated, target, config):
                    updated = api.subscription(subscription_id)
                if not selected_windows_changed(updated, target, config):
                    raise RuntimeError("Admin API 返回成功但配额窗口没有变化")
                store.mark_target(event_id, subscription_id, "done")
                print(
                    f"subscription_reset=done event_id={event_id} "
                    f"subscription_id={subscription_id}"
                )
            except ApiError as exc:
                if exc.status == 404:
                    store.mark_target(
                        event_id, subscription_id, "skipped", "订阅不存在"
                    )
                    continue
                store.mark_target(event_id, subscription_id, "failed", str(exc))
                print(
                    f"subscription_reset=pending event_id={event_id} "
                    f"subscription_id={subscription_id} error={exc}",
                    file=sys.stderr,
                )
            except Exception as exc:
                store.mark_target(event_id, subscription_id, "failed", str(exc))
                print(
                    f"subscription_reset=pending event_id={event_id} "
                    f"subscription_id={subscription_id} error={exc}",
                    file=sys.stderr,
                )
        store.finish_event(event_id)


def main() -> int:
    config_source = "environment"
    store: StateStore | None = None
    try:
        config, config_source = load_config()
        if not config.admin_api_key:
            if config.enabled:
                raise ValueError("启用时 SUB2API_ADMIN_API_KEY 不能为空")
            write_executor_status(
                config_source,
                ok=True,
                enabled=False,
                message="业务开关关闭；Admin API Key 尚未配置",
            )
            print(f"status=disabled config_source={config_source}")
            return 0
        api = AdminApi(config)
        catalog = load_catalog(api)
        write_plugin_json(config_source, "quota-sync-catalog.json", catalog)
        if not config.enabled:
            write_executor_status(
                config_source, ok=True, enabled=False, message="业务开关关闭"
            )
            print(f"status=disabled config_source={config_source}")
            return 0
        config = replace(
            config, group_ids=resolve_group_ids(api, config.group_selectors)
        )
        validation_targets = eligible_targets(api, config.group_ids)
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
            f"target_groups={config.group_selectors} "
            f"resolved_group_ids={config.group_ids} dry_run={config.dry_run}"
        )
        store = StateStore(config.state_path)
        if not config.dry_run:
            process_pending_events(api, store, config)
        exit_code = 0
        for account_id in config.account_ids:
            snapshot = load_snapshot(api, account_id)
            state = store.load_state(account_id)
            decision = decide(state, snapshot, config)
            print(
                f"action={decision.action} account_id={account_id} "
                f"reset_at={snapshot.reset_at.isoformat()} "
                f"sample_at={snapshot.sample_at.isoformat()} "
                f"reason={decision.reason}"
            )
            if decision.action == "baseline":
                store.write_observation(account_id, snapshot, update_cycle=True)
                continue
            if decision.action in {"stale", "not_rearmed"}:
                continue
            if decision.action == "observe":
                store.write_observation(account_id, snapshot, update_cycle=True)
                continue
            if decision.action == "suspicious":
                store.write_observation(account_id, snapshot, update_cycle=False)
                exit_code = 2
                continue
            if config.dry_run:
                print(
                    f"dry_run=true account_id={account_id} "
                    f"eligible_subscriptions={len(validation_targets)} "
                    f"group_ids={config.group_ids}"
                )
                continue
            event_id = store.create_event(
                config, account_id, snapshot, validation_targets
            )
            print(
                f"reset=queued account_id={account_id} event_id={event_id} "
                f"affected_count={len(validation_targets)} group_ids={config.group_ids}"
            )
        if not config.dry_run:
            process_pending_events(api, store, config)
        return exit_code
    except Exception as exc:
        write_executor_status(
            config_source, ok=False, error=str(exc), message="后台执行器校验失败"
        )
        print(f"status=error error={exc}", file=sys.stderr)
        return 1
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
