#!/usr/bin/env python3
"""Sub2API 用户额度划转 HTTP 服务。

常驻小服务，监听 127.0.0.1，由 nginx 以 /transfer/ 前缀反代，
经 custom_menu_items 以 iframe 嵌入 Sub2API 用户侧栏（自动附带 token 参数）。
仅使用 Python 标准库。
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

UTC = timezone.utc

log = logging.getLogger("transfer_server")

MAX_BODY_BYTES = 64 * 1024
MAX_IDEMPOTENCY_KEY_LENGTH = 128
HISTORY_LIMIT = 20
EMAIL_PATTERN = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,}$")

STATUS_TEXT = {
    "pending": "处理中",
    "succeeded": "成功",
    "failed": "失败",
    "compensated": "已退回",
    "needs_attention": "待人工处理",
}


def parse_positive_int(value: str, name: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是正整数") from exc
    if result <= 0:
        raise ValueError(f"{name} 必须是正整数")
    return result


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


def parse_listen(value: str) -> tuple[str, int]:
    host, separator, port_text = value.strip().rpartition(":")
    if not separator or not host or not port_text:
        raise ValueError("TRANSFER_LISTEN 必须是 host:port 形式")
    host = host.strip("[]")
    try:
        port = int(port_text)
    except ValueError as exc:
        raise ValueError("TRANSFER_LISTEN 端口必须是数字") from exc
    if not 1 <= port <= 65535:
        raise ValueError("TRANSFER_LISTEN 端口必须在 1-65535 之间")
    return host, port


def parse_amount_config(value: str, name: str) -> Decimal:
    try:
        result = Decimal(value.strip())
    except InvalidOperation as exc:
        raise ValueError(f"{name} 必须是数字") from exc
    if not result.is_finite() or result <= 0 or -result.as_tuple().exponent > 2:
        raise ValueError(f"{name} 必须是最多两位小数的正数")
    return result


@dataclass(frozen=True)
class Config:
    base_url: str
    admin_api_key: str
    listen_host: str
    listen_port: int
    min_amount: Decimal
    db_path: Path
    rate_limit_per_minute: int
    request_timeout_seconds: int

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
        source = os.environ if env is None else env
        listen_host, listen_port = parse_listen(
            source.get("TRANSFER_LISTEN", "127.0.0.1:18083")
        )
        return cls(
            base_url=normalize_base_url(
                source.get("SUB2API_BASE_URL", "http://127.0.0.1:18080")
            ),
            admin_api_key=load_admin_api_key(source),
            listen_host=listen_host,
            listen_port=listen_port,
            min_amount=parse_amount_config(
                source.get("TRANSFER_MIN_AMOUNT", "1"), "TRANSFER_MIN_AMOUNT"
            ),
            db_path=Path(
                source.get(
                    "TRANSFER_DB", "/var/lib/sub2api-quota-sync/transfer.sqlite3"
                )
            ),
            rate_limit_per_minute=parse_positive_int(
                source.get("TRANSFER_RATE_LIMIT", "5"), "TRANSFER_RATE_LIMIT"
            ),
            request_timeout_seconds=parse_positive_int(
                source.get("REQUEST_TIMEOUT_SECONDS", "15"), "REQUEST_TIMEOUT_SECONDS"
            ),
        )


class HttpError(RuntimeError):
    """可直接映射为 HTTP 响应的业务错误。"""

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(message)


class AuthError(HttpError):
    def __init__(self, message: str = "登录状态无效或已过期，请刷新页面重试") -> None:
        super().__init__(401, message)


class ApiError(RuntimeError):
    def __init__(self, method: str, path: str, status: int, detail: str = "") -> None:
        self.method = method
        self.path = path
        self.status = status
        message = f"{method} {path} 返回 HTTP {status}"
        if detail:
            message += f": {detail}"
        super().__init__(message)


class EnvelopeError(RuntimeError):
    """Sub2API 返回了非零业务状态码。"""


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


@dataclass(frozen=True)
class UserInfo:
    id: int
    email: str
    username: str
    balance: float


class Sub2ApiClient:
    max_response_bytes = 4 * 1024 * 1024

    def __init__(self, config: Config):
        self.config = config
        self.opener = urllib.request.build_opener(NoRedirectHandler())

    def request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        admin: bool = False,
        body: dict[str, object] | None = None,
    ) -> Any:
        headers = {
            "Accept": "application/json",
            "User-Agent": "sub2api-quota-sync-transfer/0.5",
        }
        if admin:
            if not self.config.admin_api_key:
                raise HttpError(503, "服务尚未配置 Admin API Key，请联系管理员")
            headers["x-api-key"] = self.config.admin_api_key
        elif token is not None:
            headers["Authorization"] = f"Bearer {token}"
        else:
            raise ValueError("request 需要 token 或 admin=True")
        data = None
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
                raise EnvelopeError(
                    str(payload.get("message") or "Sub2API API 错误")[:200]
                )
            return payload.get("data")
        return payload

    def user_profile(self, token: str) -> UserInfo:
        try:
            payload = self.request("GET", "/user/profile", token=token)
        except EnvelopeError as exc:
            # 用户 token 无效时 Sub2API 可能返回 200 + 非零 code。
            raise AuthError() from exc
        except ApiError as exc:
            if exc.status in (401, 403):
                raise AuthError() from exc
            raise
        if not isinstance(payload, dict):
            raise RuntimeError("用户信息响应格式无效")
        try:
            return UserInfo(
                id=int(payload["id"]),
                email=str(payload.get("email") or ""),
                username=str(payload.get("username") or ""),
                balance=float(payload.get("balance") or 0),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("用户信息响应格式无效") from exc

    def search_users(self, email: str) -> list[dict[str, Any]]:
        payload = self.request(
            "GET",
            "/admin/users?search=" + urllib.parse.quote(email),
            admin=True,
        )
        items = payload.get("items") if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            raise RuntimeError("用户搜索响应格式无效")
        return [item for item in items if isinstance(item, dict)]

    def adjust_balance(
        self, user_id: int, amount: Decimal, operation: str, notes: str
    ) -> None:
        self.request(
            "POST",
            f"/admin/users/{user_id}/balance",
            admin=True,
            body={
                "balance": float(amount),
                "operation": operation,
                "notes": notes,
            },
        )


def validate_email(value: object) -> str:
    if not isinstance(value, str):
        raise HttpError(400, "收款人邮箱格式无效")
    email = value.strip().lower()
    if len(email) > 254 or not EMAIL_PATTERN.match(email):
        raise HttpError(400, "收款人邮箱格式无效")
    return email


def validate_amount(value: object, min_amount: Decimal) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HttpError(400, "金额必须是数字")
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise HttpError(400, "金额必须是数字") from exc
    if not amount.is_finite():
        raise HttpError(400, "金额必须是有效数字")
    if amount <= 0:
        raise HttpError(400, "金额必须大于 0")
    if -amount.as_tuple().exponent > 2:
        raise HttpError(400, "金额最多保留两位小数")
    if amount < min_amount:
        raise HttpError(400, f"单笔划转金额不能低于 {format(min_amount, 'f')}")
    return amount


def resolve_recipient(client: Sub2ApiClient, email: str) -> dict[str, Any]:
    candidates = client.search_users(email)
    # search 同时匹配用户名，必须客户端精确匹配邮箱。
    matches = [
        item for item in candidates if str(item.get("email") or "").lower() == email
    ]
    if not matches:
        raise HttpError(404, "未找到该邮箱对应的用户，请确认邮箱是否正确")
    if len(matches) > 1:
        raise HttpError(409, "该邮箱匹配到多个用户，请联系管理员处理")
    return matches[0]


class RateLimiter:
    """按发送者内存滑动窗口限流。"""

    def __init__(
        self,
        limit: int,
        window_seconds: float = 60.0,
        clock: Any = time.monotonic,
    ) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self.clock = clock
        self.lock = threading.Lock()
        self.events: dict[int, deque[float]] = {}

    def allow(self, key: int) -> bool:
        now = self.clock()
        with self.lock:
            events = self.events.setdefault(key, deque())
            while events and events[0] <= now - self.window_seconds:
                events.popleft()
            if len(events) >= self.limit:
                return False
            events.append(now)
            return True


class TransferStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.lock:
            self.db.execute(
                """
                CREATE TABLE IF NOT EXISTS transfers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL UNIQUE,
                    sender_id INTEGER NOT NULL,
                    sender_email TEXT NOT NULL DEFAULT '',
                    receiver_id INTEGER,
                    receiver_email TEXT NOT NULL DEFAULT '',
                    amount TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
                """
            )
            self.db.execute(
                "CREATE INDEX IF NOT EXISTS idx_transfers_sender ON transfers(sender_id)"
            )
            self.db.execute(
                "CREATE INDEX IF NOT EXISTS idx_transfers_receiver ON transfers(receiver_id)"
            )
            self.db.commit()

    def create_pending(
        self, request_id: str, sender: UserInfo, amount: Decimal
    ) -> int | None:
        """返回新记录 id；request_id 已存在时返回 None（幂等去重）。"""
        with self.lock:
            try:
                cursor = self.db.execute(
                    "INSERT INTO transfers"
                    " (request_id, sender_id, sender_email, amount, status, created_at)"
                    " VALUES (?, ?, ?, ?, 'pending', ?)",
                    (
                        request_id,
                        sender.id,
                        sender.email,
                        format(amount, "f"),
                        datetime.now(UTC).isoformat(),
                    ),
                )
                self.db.commit()
                return int(cursor.lastrowid)
            except sqlite3.IntegrityError:
                return None

    def get_by_request_id(self, request_id: str) -> sqlite3.Row | None:
        with self.lock:
            return self.db.execute(
                "SELECT * FROM transfers WHERE request_id = ?", (request_id,)
            ).fetchone()

    def set_receiver(
        self, record_id: int, receiver_id: int, receiver_email: str
    ) -> None:
        with self.lock:
            self.db.execute(
                "UPDATE transfers SET receiver_id = ?, receiver_email = ? WHERE id = ?",
                (receiver_id, receiver_email, record_id),
            )
            self.db.commit()

    def finish(self, record_id: int, status: str, error: str = "") -> None:
        with self.lock:
            self.db.execute(
                "UPDATE transfers SET status = ?, error = ? WHERE id = ?",
                (status, error, record_id),
            )
            self.db.commit()

    def history(self, user_id: int, limit: int = HISTORY_LIMIT) -> list[sqlite3.Row]:
        with self.lock:
            return self.db.execute(
                "SELECT * FROM transfers WHERE sender_id = ? OR receiver_id = ?"
                " ORDER BY id DESC LIMIT ?",
                (user_id, user_id, limit),
            ).fetchall()

    def close(self) -> None:
        with self.lock:
            self.db.close()


class TransferService:
    """划转核心逻辑，HTTP 层只做解析与序列化。"""

    def __init__(
        self,
        config: Config,
        client: Sub2ApiClient,
        store: TransferStore,
        rate_limiter: RateLimiter,
    ) -> None:
        self.config = config
        self.client = client
        self.store = store
        self.rate_limiter = rate_limiter

    def _authenticate(self, token: str) -> UserInfo:
        if not token:
            raise AuthError("缺少登录凭证")
        return self.client.user_profile(token)

    def me(self, token: str) -> tuple[int, dict[str, Any]]:
        user = self._authenticate(token)
        return 200, {
            "email": user.email,
            "username": user.username,
            "balance": user.balance,
        }

    def history(self, token: str) -> tuple[int, list[dict[str, Any]]]:
        user = self._authenticate(token)
        result = []
        for row in self.store.history(user.id):
            outgoing = row["sender_id"] == user.id
            result.append(
                {
                    "direction": "out" if outgoing else "in",
                    "counterpart_email": row["receiver_email"]
                    if outgoing
                    else row["sender_email"],
                    "amount": row["amount"],
                    "status": row["status"],
                    "status_text": STATUS_TEXT.get(row["status"], row["status"]),
                    "error": row["error"],
                    "created_at": row["created_at"],
                }
            )
        return 200, result

    def transfer(
        self, token: str, payload: object, idempotency_key: str
    ) -> tuple[int, dict[str, Any]]:
        sender = self._authenticate(token)
        if not self.rate_limiter.allow(sender.id):
            raise HttpError(429, "操作过于频繁，请稍后再试")
        if not isinstance(payload, dict):
            raise HttpError(400, "请求格式无效")
        amount = validate_amount(payload.get("amount"), self.config.min_amount)
        recipient_email = validate_email(payload.get("recipient_email"))
        if recipient_email == sender.email.lower():
            raise HttpError(400, "不能划转给自己")
        # 格式校验通过后才落库：允许修正参数后用同一幂等键重试。
        request_id = f"{sender.id}:{idempotency_key}"
        record_id = self.store.create_pending(request_id, sender, amount)
        if record_id is None:
            return self._replay(request_id)
        return self._execute(record_id, sender, recipient_email, amount)

    def _execute(
        self, record_id: int, sender: UserInfo, recipient_email: str, amount: Decimal
    ) -> tuple[int, dict[str, Any]]:
        if Decimal(str(sender.balance)) < amount:
            self.store.finish(record_id, "failed", "余额不足")
            raise HttpError(400, "余额不足")
        try:
            receiver = resolve_recipient(self.client, recipient_email)
        except HttpError as exc:
            self.store.finish(record_id, "failed", exc.message)
            raise
        try:
            receiver_id = int(receiver["id"])
        except (KeyError, TypeError, ValueError) as exc:
            self.store.finish(record_id, "failed", "收款人信息无效")
            raise HttpError(502, "收款人信息无效，请联系管理员") from exc
        receiver_email = str(receiver.get("email") or recipient_email).lower()
        if receiver_id == sender.id:
            self.store.finish(record_id, "failed", "不能划转给自己")
            raise HttpError(400, "不能划转给自己")
        self.store.set_receiver(record_id, receiver_id, receiver_email)
        try:
            self.client.adjust_balance(
                sender.id,
                amount,
                "subtract",
                f"transfer to {receiver_email} via quota-sync",
            )
        except Exception as exc:
            log.warning("transfer id=%s subtract failed: %s", record_id, exc)
            self.store.finish(record_id, "failed", "扣减余额失败")
            raise HttpError(
                502, "扣减余额失败（余额可能不足或服务繁忙），划转未执行"
            ) from exc
        try:
            self.client.adjust_balance(
                receiver_id,
                amount,
                "add",
                f"transfer from {sender.email} via quota-sync",
            )
        except Exception as exc:
            log.warning("transfer id=%s credit failed: %s", record_id, exc)
            try:
                self.client.adjust_balance(
                    sender.id,
                    amount,
                    "add",
                    f"compensation for failed transfer to {receiver_email} via quota-sync",
                )
            except Exception as compensation_exc:
                log.critical(
                    "transfer id=%s compensation failed, manual intervention required:"
                    " %s",
                    record_id,
                    compensation_exc,
                )
                self.store.finish(
                    record_id, "needs_attention", "收款失败且自动退款失败，需人工处理"
                )
                raise HttpError(
                    502, "划转异常：收款失败且自动退款失败，请联系管理员处理"
                ) from compensation_exc
            self.store.finish(record_id, "compensated", "收款失败，金额已退回")
            raise HttpError(502, "收款方入账失败，金额已退回您的账户，请稍后再试") from exc
        self.store.finish(record_id, "succeeded")
        log.info(
            "transfer id=%s succeeded sender=%s receiver=%s amount=%s",
            record_id,
            sender.id,
            receiver_id,
            amount,
        )
        return 200, {
            "status": "succeeded",
            "amount": format(amount, "f"),
            "recipient_email": receiver_email,
            "message": "划转成功",
        }

    def _replay(self, request_id: str) -> tuple[int, dict[str, Any]]:
        record = self.store.get_by_request_id(request_id)
        if record is None:  # 并发下插入冲突但查询未命中，防御性处理
            raise HttpError(409, "相同请求正在处理中，请稍候")
        payload = {
            "duplicate": True,
            "status": record["status"],
            "status_text": STATUS_TEXT.get(record["status"], record["status"]),
            "amount": record["amount"],
            "recipient_email": record["receiver_email"],
            "created_at": record["created_at"],
        }
        if record["status"] == "succeeded":
            payload["message"] = "划转成功（重复请求，已自动去重）"
            return 200, payload
        if record["status"] == "pending":
            raise HttpError(409, "相同请求正在处理中，请稍候")
        raise HttpError(400, record["error"] or "划转失败（重复请求，已自动去重）")


class TransferHttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], service: TransferService):
        self.service = service
        super().__init__(address, TransferRequestHandler)


class TransferRequestHandler(BaseHTTPRequestHandler):
    server_version = "Sub2APIQuotaSyncTransfer/0.5"
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        # 请求行可能携带 token 查询参数，日志只保留路径部分。
        path = urllib.parse.urlsplit(self.path).path if self.path else ""
        log.info("%s %s %s", self.address_string(), self.command, path)

    def log_error(self, format: str, *args: Any) -> None:
        log.warning("%s %s", self.address_string(), format % args)

    def do_GET(self) -> None:
        try:
            path = urllib.parse.urlsplit(self.path).path
            if path in ("/transfer", "/transfer/"):
                self._send_html()
            elif path == "/transfer/api/me":
                self._call_api(lambda token: self.server.service.me(token))
            elif path == "/transfer/api/history":
                self._call_api(lambda token: self.server.service.history(token))
            else:
                self._send_json(404, {"ok": False, "error": "页面不存在"})
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self) -> None:
        try:
            path = urllib.parse.urlsplit(self.path).path
            if path != "/transfer/api/transfer":
                self._send_json(404, {"ok": False, "error": "接口不存在"})
                return
            length = self.headers.get("Content-Length")
            if length is None:
                self._send_json(411, {"ok": False, "error": "缺少 Content-Length"})
                return
            try:
                size = int(length)
            except ValueError:
                self._send_json(400, {"ok": False, "error": "请求长度无效"})
                return
            if size < 0 or size > MAX_BODY_BYTES:
                self._send_json(413, {"ok": False, "error": "请求体过大"})
                return
            raw = self.rfile.read(size)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json(400, {"ok": False, "error": "请求体必须是 JSON"})
                return
            key = self.headers.get("X-Idempotency-Key", "").strip()
            if len(key) > MAX_IDEMPOTENCY_KEY_LENGTH:
                self._send_json(400, {"ok": False, "error": "幂等键过长"})
                return
            if not key:
                key = uuid.uuid4().hex
            self._call_api(
                lambda token: self.server.service.transfer(token, payload, key)
            )
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _bearer_token(self) -> str:
        header = self.headers.get("Authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise HttpError(401, "缺少有效的登录凭证")
        return token.strip()

    def _call_api(self, handler: Any) -> None:
        try:
            token = self._bearer_token()
            status, data = handler(token)
            self._send_json(status, {"ok": True, "data": data})
        except HttpError as exc:
            self._send_json(exc.status, {"ok": False, "error": exc.message})
        except Exception as exc:
            log.warning("Sub2API 请求失败: %s", exc)
            self._send_json(
                502, {"ok": False, "error": "Sub2API 服务暂时不可用，请稍后再试"}
            )

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self) -> None:
        body = PAGE_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>额度划转</title>
<style>
* { box-sizing: border-box; }
body { margin: 0; font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; background: #f5f6f8; color: #1f2937; }
.container { max-width: 480px; margin: 0 auto; padding: 20px 16px 40px; }
.card { background: #fff; border: 1px solid #e5e7eb; border-radius: 10px; padding: 16px 18px; margin-bottom: 16px; }
h1 { font-size: 17px; margin: 0 0 8px; }
.balance { font-size: 28px; font-weight: 600; margin: 8px 0 2px; }
.muted { color: #6b7280; font-size: 13px; }
label { display: block; font-size: 13px; margin: 12px 0 4px; }
input { width: 100%; padding: 9px 10px; border: 1px solid #d1d5db; border-radius: 6px; font-size: 14px; }
input:focus { outline: none; border-color: #2563eb; }
button { width: 100%; margin-top: 14px; padding: 10px; border: 0; border-radius: 6px; background: #2563eb; color: #fff; font-size: 15px; cursor: pointer; }
button:disabled { background: #9ca3af; cursor: not-allowed; }
button.secondary { background: #e5e7eb; color: #111827; }
.msg { margin-top: 12px; padding: 10px 12px; border-radius: 6px; font-size: 14px; display: none; }
.msg.error { display: block; background: #fef2f2; color: #b91c1c; }
.msg.ok { display: block; background: #f0fdf4; color: #15803d; }
.confirm-box { border: 1px dashed #d1d5db; border-radius: 8px; padding: 4px 12px 12px; margin-top: 14px; display: none; }
.confirm-box p { margin: 10px 0 0; font-size: 14px; }
.history-item { display: flex; justify-content: space-between; gap: 8px; padding: 10px 0; border-bottom: 1px solid #f3f4f6; font-size: 13px; }
.history-item:last-child { border-bottom: 0; }
.amount-out { color: #b91c1c; white-space: nowrap; }
.amount-in { color: #15803d; white-space: nowrap; }
</style>
</head>
<body>
<div class="container">
  <div class="card">
    <h1>额度划转</h1>
    <div class="muted" id="user-line">加载中…</div>
    <div class="balance" id="balance">--</div>
    <div class="muted">当前余额（美元）</div>
  </div>
  <div class="card">
    <label for="email">收款人邮箱</label>
    <input id="email" type="email" placeholder="user@example.com" autocomplete="off">
    <label for="amount">划转金额</label>
    <input id="amount" type="number" min="0" step="0.01" placeholder="0.00">
    <button id="prepare">划转</button>
    <div class="confirm-box" id="confirm-box">
      <p>向 <b id="confirm-email"></b> 划转 <b id="confirm-amount"></b> 美元？</p>
      <p class="muted">提交后立即从您的余额中扣减，请确认收款邮箱无误。</p>
      <button id="submit">确认划转</button>
      <button class="secondary" id="cancel">取消</button>
    </div>
    <div class="msg" id="msg"></div>
  </div>
  <div class="card">
    <h1>最近划转记录</h1>
    <div id="history" class="muted">加载中…</div>
  </div>
</div>
<script>
(function () {
  var token = new URLSearchParams(location.search).get('token') || '';
  var idempotencyKey = null;
  var $ = function (id) { return document.getElementById(id); };

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function api(path, options) {
    options = options || {};
    options.headers = Object.assign({ 'Authorization': 'Bearer ' + token }, options.headers || {});
    return fetch(path, options).then(function (resp) {
      return resp.json().catch(function () { return null; }).then(function (body) {
        if (!resp.ok || !body || body.ok === false) {
          throw new Error((body && body.error) || ('请求失败（HTTP ' + resp.status + '）'));
        }
        return body.data;
      });
    });
  }

  function showMsg(text, ok) {
    var el = $('msg');
    el.textContent = text;
    el.className = 'msg ' + (ok ? 'ok' : 'error');
  }

  function loadMe() {
    return api('/transfer/api/me').then(function (me) {
      $('user-line').textContent = (me.username || '') + '（' + me.email + '）';
      $('balance').textContent = Number(me.balance).toFixed(2);
    }).catch(function (e) {
      $('user-line').textContent = '身份验证失败：' + e.message;
      $('prepare').disabled = true;
    });
  }

  function loadHistory() {
    return api('/transfer/api/history').then(function (rows) {
      var box = $('history');
      if (!rows.length) { box.textContent = '暂无记录'; return; }
      box.className = '';
      box.innerHTML = rows.map(function (r) {
        var out = r.direction === 'out';
        var time = (r.created_at || '').replace('T', ' ').slice(0, 16);
        return '<div class="history-item">' +
          '<span>' + (out ? '转给 ' : '来自 ') + escapeHtml(r.counterpart_email || '') +
          '<br><span class="muted">' + escapeHtml(time) + ' · ' + escapeHtml(r.status_text || r.status) + '</span></span>' +
          '<span class="' + (out ? 'amount-out' : 'amount-in') + '">' +
          (out ? '-' : '+') + Number(r.amount).toFixed(2) + '</span></div>';
      }).join('');
    }).catch(function () {
      $('history').textContent = '记录加载失败';
    });
  }

  $('prepare').addEventListener('click', function () {
    var email = $('email').value.trim();
    var amount = $('amount').value.trim();
    if (!email || !amount || !(Number(amount) > 0)) {
      showMsg('请填写收款人邮箱和正确的金额', false);
      return;
    }
    $('confirm-email').textContent = email;
    $('confirm-amount').textContent = Number(amount).toFixed(2);
    $('confirm-box').style.display = 'block';
    idempotencyKey = (window.crypto && crypto.randomUUID)
      ? crypto.randomUUID()
      : String(Date.now()) + '-' + Math.random().toString(36).slice(2);
    $('msg').className = 'msg';
  });

  $('cancel').addEventListener('click', function () {
    $('confirm-box').style.display = 'none';
    idempotencyKey = null;
  });

  $('submit').addEventListener('click', function () {
    $('submit').disabled = true;
    api('/transfer/api/transfer', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Idempotency-Key': idempotencyKey },
      body: JSON.stringify({
        recipient_email: $('email').value.trim(),
        amount: Number($('amount').value),
      }),
    }).then(function (data) {
      showMsg(data.message || '划转成功', true);
      $('confirm-box').style.display = 'none';
      idempotencyKey = null;
      $('amount').value = '';
      loadMe();
      loadHistory();
    }).catch(function (e) {
      showMsg(e.message, false);
      loadHistory();
    }).finally(function () {
      $('submit').disabled = false;
    });
  });

  if (!token) {
    showMsg('缺少登录凭证，请从 Sub2API 用户面板进入本页面', false);
    $('prepare').disabled = true;
  } else {
    loadMe();
    loadHistory();
  }
})();
</script>
</body>
</html>
"""


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        config = Config.from_env()
    except (ValueError, OSError) as exc:
        log.error("配置错误: %s", exc)
        return 1
    store = TransferStore(config.db_path)
    client = Sub2ApiClient(config)
    service = TransferService(
        config, client, store, RateLimiter(config.rate_limit_per_minute)
    )
    server = TransferHttpServer((config.listen_host, config.listen_port), service)
    log.info("划转服务已启动 http://%s:%d", config.listen_host, config.listen_port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
