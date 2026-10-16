"""Codex account registry and secure storage.

令牌 bundle 只存放在 OS 密钥库（``editor_credentials.secret("codex/<account_id>")``）；
非敏感的账号元数据与 active 指针存放在 ``~/.config/VibeGodot/codex/accounts.json``
（``0600``，原子写），绝不包含任何令牌或 subject 原文。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
from typing import Any

from vibe_tools import editor_credentials
from vibe_tools.codex_auth import CodexAccount

_REGISTRY_FILENAME = "accounts.json"
_ACCOUNT_ID_LENGTH = 22
_BUNDLE_FIELDS = (
    "account_id",
    "issuer",
    "subject",
    "client_id",
    "ext_agent_host_id",
    "id_token",
    "access_token",
    "refresh_token",
    "token_type",
    "expires_in",
    "earliest_refresh_at",
    "saved_at",
    "plan_enabled",
    "email",
    "label",
)


def account_id_for(*, issuer: str, subject: str, client_id: str) -> str:
    digest = hashlib.sha256(
        (issuer + "\x00" + subject + "\x00" + client_id).encode("utf-8")
    ).digest()
    encoded = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return encoded[:_ACCOUNT_ID_LENGTH]


def _state_dir() -> str:
    override = os.environ.get("VIBEGODOT_CODEX_STATE_DIR")
    if override:
        return override
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "VibeGodot", "codex")


def _registry_file() -> str:
    return os.path.join(_state_dir(), _REGISTRY_FILENAME)


def _empty_registry() -> dict[str, Any]:
    return {"active": None, "accounts": {}}


def _read_registry() -> dict[str, Any]:
    try:
        with open(_registry_file(), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return _empty_registry()
    if not isinstance(data, dict):
        return _empty_registry()
    accounts = data.get("accounts")
    if not isinstance(accounts, dict):
        accounts = {}
    return {"active": data.get("active"), "accounts": accounts}


def _write_registry(data: dict[str, Any]) -> None:
    directory = _state_dir()
    os.makedirs(directory, mode=0o700, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix="accounts.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, separators=(",", ":"), sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, _registry_file())
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _secret_name(account_id: str) -> str:
    return "codex/" + account_id


def _serialize(account: CodexAccount) -> str:
    payload = {field: getattr(account, field) for field in _BUNDLE_FIELDS}
    payload["scopes"] = list(account.scopes)
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _deserialize(raw: str) -> CodexAccount:
    data = json.loads(raw)
    return CodexAccount(
        account_id=data["account_id"],
        label=data.get("label", ""),
        email=data.get("email", ""),
        issuer=data["issuer"],
        subject=data["subject"],
        client_id=data["client_id"],
        ext_agent_host_id=data.get("ext_agent_host_id", ""),
        id_token=data.get("id_token", ""),
        access_token=data.get("access_token", ""),
        refresh_token=data.get("refresh_token", ""),
        token_type=data.get("token_type", "Bearer"),
        expires_in=int(data.get("expires_in", 0)),
        earliest_refresh_at=data.get("earliest_refresh_at"),
        scopes=tuple(data.get("scopes", ())),
        saved_at=data.get("saved_at", ""),
        plan_enabled=bool(data.get("plan_enabled", False)),
    )


def save_account(account: CodexAccount) -> None:
    # 令牌 bundle 只进 OS 密钥库。
    editor_credentials.secret(_secret_name(account.account_id), _serialize(account))
    registry = _read_registry()
    registry["accounts"][account.account_id] = {
        "label": account.label,
        "email": account.email,
        "plan_enabled": bool(account.plan_enabled),
    }
    if not registry.get("active"):
        registry["active"] = account.account_id
    _write_registry(registry)


def load_account(account_id: str) -> CodexAccount | None:
    raw = editor_credentials.secret(_secret_name(account_id))
    if not raw:
        return None
    try:
        return _deserialize(raw)
    except (ValueError, KeyError, TypeError):
        return None


def list_accounts() -> list[dict[str, Any]]:
    registry = _read_registry()
    result: list[dict[str, Any]] = []
    for account_id in sorted(registry["accounts"]):
        metadata = registry["accounts"][account_id]
        if not isinstance(metadata, dict):
            metadata = {}
        result.append(
            {
                "account_id": account_id,
                "label": metadata.get("label", ""),
                "email": metadata.get("email", ""),
                "plan_enabled": bool(metadata.get("plan_enabled", False)),
            }
        )
    return result


def delete_account(account_id: str) -> None:
    editor_credentials.delete_secret(_secret_name(account_id))
    registry = _read_registry()
    registry["accounts"].pop(account_id, None)
    if registry.get("active") == account_id:
        registry["active"] = None
    _write_registry(registry)


def active_account_id() -> str | None:
    return _read_registry().get("active") or None


def set_active_account(account_id: str | None) -> None:
    registry = _read_registry()
    registry["active"] = account_id or None
    _write_registry(registry)
