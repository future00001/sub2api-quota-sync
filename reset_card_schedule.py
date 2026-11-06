"""单次用卡：持久化领取后先关闭开关，再调用一次宿主 API；未知结果不重试。"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

UTC = timezone.utc
PLUGIN_KEY = "com.hzyhz.sub2api-quota-sync"
UNKNOWN = "执行中断或结果未知；开关已关闭，请核对账号用卡记录，不会自动重试"


def timestamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("定时用卡时间必须包含时区")
    return result


def validate_schedule(schedule):
    if not isinstance(schedule, dict) or type(schedule.get("enabled")) is not bool:
        raise ValueError("定时用卡配置无效")
    if not re.fullmatch(r"[a-fA-F0-9-]{36}", schedule.get("id", "")):
        raise ValueError("定时任务 ID 无效")
    if type(schedule.get("account_id")) is not int or schedule["account_id"] <= 0:
        raise ValueError("定时用卡账号无效")
    run = timestamp(schedule["run_at"])
    expires = timestamp(schedule["credit_expires_at"])
    if run >= expires:
        raise ValueError("执行时间必须早于重置卡到期时间")
    return run, expires


def same_task(a, b):
    return all(a.get(k) == b.get(k) for k in
               ("id", "account_id", "run_at", "credit_expires_at"))


def plugin_path(api):
    plugins = api.request("GET", "/admin/plugins")
    for plugin in plugins:
        if plugin.get("plugin_key") == PLUGIN_KEY:
            return f"/admin/plugins/{int(plugin['id'])}/config"
    raise RuntimeError("找不到配额同步插件")


def update_result(api, path, schedule, result):
    # 每次回写重新读取，避免覆盖用户已编辑的同步设置或替换后的定时任务。
    latest = api.request("GET", path)
    current = latest.get("reset_card_schedule") or {}
    if not same_task(current, schedule):
        return False
    if current.get("enabled") is False and current.get("result") == result:
        return True
    latest["reset_card_schedule"] = {**current, "enabled": False, "result": result}
    api.request("PUT", path, latest)
    return True


def process_schedule(api, config_source, state_dir, now=None):
    # 调度失败不能阻断既有的订阅同步逻辑。
    try:
        return _process_schedule(api, config_source, state_dir, now or datetime.now(UTC))
    except Exception as exc:
        # 避免把上游响应、账号凭据写进日志。
        print(f"reset_card_schedule=error type={type(exc).__name__}", file=sys.stderr)
        return "error"


def _process_schedule(api, config_source, state_dir, now):
    if config_source == "environment":
        return "disabled"
    raw = json.loads(Path(config_source).read_text(encoding="utf-8"))
    schedule = raw.get("reset_card_schedule")
    if not schedule:
        return "disabled"
    run, expires = validate_schedule(schedule)
    if schedule["enabled"] and run > now:
        return "waiting"
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(state_dir / "reset-card-schedule.sqlite3", timeout=10) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, task TEXT NOT NULL, status TEXT NOT NULL,
            result TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        row = db.execute("SELECT status, result FROM jobs WHERE id=?", (schedule["id"],)).fetchone()
        if row:
            result = UNKNOWN if row[0] == "claimed" else row[1]
            if schedule["enabled"] is False and schedule.get("result") == result:
                return row[0]
            path = plugin_path(api)
            update_result(api, path, schedule, result)
            return row[0]
        if not schedule["enabled"]:
            return "disabled"

        path = plugin_path(api)
        latest = api.request("GET", path)
        current = latest.get("reset_card_schedule") or {}
        if not current.get("enabled") or not same_task(current, schedule):
            return "cancelled"
        # INSERT OR IGNORE + 持久提交：请求发出前即形成永久防重屏障。
        inserted = db.execute(
            "INSERT OR IGNORE INTO jobs VALUES (?, ?, 'claimed', ?, ?)",
            (schedule["id"], json.dumps(schedule), UNKNOWN, now.isoformat()),
        ).rowcount
        db.commit()
        if not inserted:
            return "claimed"
        status, result = "skipped", ""
        try:
            if not update_result(api, path, schedule, UNKNOWN):
                result = "任务已被替换，未使用重置卡"
            elif now - run > timedelta(minutes=15):
                result = "错过执行时间超过 15 分钟，已关闭且未使用重置卡"
            elif now >= expires:
                result = "预约时的重置卡已到期，已关闭且未使用"
            elif latest.get("dry_run", True):
                result = "演练完成：未使用重置卡，单次开关已关闭"
                status = "dry_run"
            else:
                account = api.account(schedule["account_id"])
                if account.get("platform") != "openai" or account.get("type") != "oauth":
                    raise ValueError("账号不是 OpenAI OAuth")
                if (account.get("extra") or {}).get("auto_reset_credit_enabled"):
                    result = "账号已开启按阈值自动用卡；为避免双重消费，本次定时用卡已取消"
                else:
                    quota = api.request("GET", f"/admin/openai/accounts/{schedule['account_id']}/quota")
                    credits = quota.get("rate_limit_reset_credits") or {}
                    available = [timestamp(c["expires_at"]) for c in credits.get("credits", [])]
                    if expires not in available or credits.get("available_count", 0) < 1:
                        result = "预约时的重置卡已不可用或无法确认，未使用其他卡"
                    else:
                        # 手动入口没有可传入的幂等键，因此绝不能对这个 POST 自动重试。
                        response = api.request(
                            "POST", f"/admin/openai/accounts/{schedule['account_id']}/reset-quota", {}
                        )
                        if not isinstance(response, dict) or not response.get("windows_reset"):
                            status, result = "unknown", UNKNOWN
                        else:
                            status = "success"
                            result = "已自动使用一次重置卡，单次开关已关闭"
                            if response.get("warning_code"):
                                result += "；宿主后处理有警告，请检查账号配额与状态"
        except Exception:
            status, result = "unknown", UNKNOWN
        db.execute("UPDATE jobs SET status=?, result=?, updated_at=? WHERE id=?",
                   (status, result, now.isoformat(), schedule["id"]))
        db.commit()
        update_result(api, path, schedule, result)
        print(f"reset_card_schedule={status} account_id={schedule['account_id']}")
        return status
