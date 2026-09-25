from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import settings

SENSITIVE = {"password", "token", "authorization", "secret", "password_hash", "token_hash"}


def password_hash(password: str, *, salt: str | None = None) -> str:
    actual_salt = salt or secrets.token_hex(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode(), actual_salt.encode(), 120_000)
    return f"pbkdf2_sha256${actual_salt}${derived.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        _, salt, expected = encoded.split("$", 2)
    except ValueError:
        return False
    return hmac.compare_digest(password_hash(password, salt=salt).split("$")[-1], expected)


def issue_token() -> tuple[str, str]:
    raw = secrets.token_urlsafe(32)
    return raw, hashlib.sha256((settings().token_secret + raw).encode()).hexdigest()


def token_hash(raw: str) -> str:
    return hashlib.sha256((settings().token_secret + raw).encode()).hexdigest()


def expiry(hours: int = 12) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec="seconds")


def sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: ("[FILTERED]" if key.casefold() in SENSITIVE else sanitize(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    return value


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def request_hash(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode()).hexdigest()


def chain_hash(prev_hash: str, fields: dict[str, Any]) -> str:
    """审计/事件哈希链：当前记录摘要 = sha256(前一条摘要 + 规范化字段)。"""
    return hashlib.sha256((prev_hash + "|" + stable_json(fields)).encode()).hexdigest()
