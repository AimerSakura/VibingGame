"""Sign in with ChatGPT (SIWC) OAuth primitives and token lifecycle.

只依赖 Python 标准库与 ``cryptography``（仅用于 JWKS 验签）。本模块不读取、不写
入、不打印任何令牌明文；对外异常只携带操作名与官方错误码。
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import queue
import re
import secrets
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from vibe_tools.editor_credentials import secret

try:  # ``cryptography`` 是唯一新增运行时依赖；缺失时仅验签路径报错。
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
    _CRYPTO_AVAILABLE = True
except ImportError:  # pragma: no cover - 正常安装环境始终可用
    _CRYPTO_AVAILABLE = False

try:  # certifi 随包提供 CA bundle；打包环境找不到系统证书库时使用它。
    import certifi
except ImportError:  # pragma: no cover - 回退到系统默认证书库
    certifi = None  # type: ignore[assignment]

AUTHORIZE_ENDPOINT = "https://auth.openai.com/api/accounts/authorize"
TOKEN_ENDPOINT = "https://auth.openai.com/api/accounts/oauth/token"
DISCOVERY_URL = "https://auth.openai.com/.well-known/openid-configuration"
DYNAMIC_CLIENT_ID = "dynamic_agent_client"
RESOURCE = "https://api.openai.com/v1"
SCOPE = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
AGENT_NAME_HINT = "VibeGodot"
ISSUER = "https://auth.openai.com"
CALLBACK_PATH = "/auth/callback"
CALLBACK_HOST = "127.0.0.1"
CALLBACK_TIMEOUT_S = 300
REFRESH_SKEW_S = 120
DIRECT_SCOPE = "chatgpt.tokens.use.direct"
HTTP_TIMEOUT_S = 30
REVOKE_ATTEMPTS = 3
REVOKE_BACKOFF_S = 0.5

_ALLOWED_ALGS = ("RS256", "ES256")
# HTTP 错误响应只做有界读取；只接受官方风格的 OAuth error code。
_MAX_HTTP_ERROR_BODY_BYTES = 65536
_SAFE_OAUTH_ERROR_CODE_RE = re.compile(r"[a-z0-9_-]{1,128}")
_CALLBACK_KEYS = frozenset(
    {"code", "state", "error", "error_description", "scope", "client_id"})
_CALLBACK_HTML = (
    "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
    "<title>VibeGodot</title></head><body>"
    "<p>Authorization complete. You can close this browser window and return to VibeGodot.</p>"
    "</body></html>"
).encode("utf-8")

logger = logging.getLogger(__name__)

_DISCOVERY_CACHE: dict[str, Any] | None = None
_REFRESH_LOCKS: dict[str, threading.Lock] = {}
_REFRESH_LOCKS_GUARD = threading.Lock()


class CodexAuthError(RuntimeError):
    """消息只含操作名 + 官方错误码，绝不含令牌/授权码/verifier。"""


class _HttpError(Exception):
    """内部 HTTP 失败信号；只携带状态码与可选的官方 OAuth 错误码。"""

    def __init__(
        self,
        status: int,
        message: str = "",
        *,
        oauth_error: str | None = None,
    ) -> None:
        super().__init__("http_%s" % status)
        self.status = status
        self.message = message
        self.oauth_error = oauth_error


@dataclass(frozen=True)
class CodexAccount:
    account_id: str
    label: str
    email: str
    issuer: str
    subject: str
    client_id: str
    ext_agent_host_id: str
    id_token: str
    access_token: str
    refresh_token: str
    token_type: str
    expires_in: int
    earliest_refresh_at: int | None
    scopes: tuple[str, ...]
    saved_at: str
    plan_enabled: bool


# --------------------------------------------------------------------------- #
# 可注入的时钟 / 网络 / 睡眠
# --------------------------------------------------------------------------- #

def _now() -> float:
    return time.time()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _redact(value: str | None) -> str:
    return "***" if value else ""


def _b64url_decode(data: str) -> bytes:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _b64url_uint(data: str) -> int:
    return int.from_bytes(_b64url_decode(data), "big")


def _safe_oauth_error_code(payload: Any) -> str | None:
    """只接受小写字母/数字/下划线/连字符组成的 OAuth 错误码。"""
    if not isinstance(payload, dict):
        return None
    code = payload.get("error")
    if not isinstance(code, str):
        return None
    if not _SAFE_OAUTH_ERROR_CODE_RE.fullmatch(code):
        return None
    return code


def _http_error_oauth_code(exc: urllib.error.HTTPError) -> str | None:
    """有界读取并解析 HTTPError 正文，仅返回安全的 OAuth 错误码。

    正文（可能含 error_description）在函数返回后即被丢弃，不写日志、不进异常。
    """
    try:
        raw = exc.read(_MAX_HTTP_ERROR_BODY_BYTES)
    except Exception:
        return None
    if not isinstance(raw, (bytes, bytearray)):
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except ValueError:
        return None
    return _safe_oauth_error_code(payload)


def ssl_context() -> ssl.SSLContext:
    """Return a verifying TLS context using the bundled certifi CA bundle.

    打包后的解释器可能找不到系统证书库，因此所有 OpenAI auth 的 HTTPS 请求都
    显式使用随包的 certifi CA。certifi 不可用时回退到解释器的安全系统默认
    context；任何情况下都不关闭证书或主机名校验。
    """
    context = ssl.create_default_context()
    if certifi is not None:
        try:
            bundle = certifi.where()
            if bundle:
                context.load_verify_locations(cafile=bundle)
        except Exception:
            # certifi 或它的 CA 文件不可用时保留系统默认信任库，校验仍然开启。
            pass
    return context


def _post_form(url: str, data: dict[str, str]) -> dict[str, Any]:
    body = urllib.parse.urlencode(data).encode("ascii")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(
            request, timeout=HTTP_TIMEOUT_S, context=ssl_context()
        ) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise _HttpError(
            int(exc.code),
            oauth_error=_http_error_oauth_code(exc),
        ) from None
    except urllib.error.URLError:
        raise _HttpError(0, "network_error") from None
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        raise _HttpError(0, "invalid_json") from None


def _get_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(
            request, timeout=HTTP_TIMEOUT_S, context=ssl_context()
        ) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise CodexAuthError("discovery_failed_%d" % int(exc.code)) from None
    except urllib.error.URLError:
        raise CodexAuthError("discovery_failed") from None
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        raise CodexAuthError("discovery_invalid_json") from None


def discover(*, force: bool = False) -> dict[str, Any]:
    global _DISCOVERY_CACHE
    if _DISCOVERY_CACHE is None or force:
        _DISCOVERY_CACHE = _get_json(DISCOVERY_URL)
    return _DISCOVERY_CACHE


def _oauth_error(response: dict[str, Any]) -> CodexAuthError | None:
    code = response.get("error")
    if code:
        # 官方错误码不含令牌；原样上报以便按错误矩阵恢复。
        return CodexAuthError(str(code))
    return None


def _oauth_http_error(exc: _HttpError, generic: str) -> CodexAuthError:
    """HTTP 失败时优先透传可安全识别的官方错误码，否则回退通用错误码。"""
    return CodexAuthError(exc.oauth_error or generic)


# --------------------------------------------------------------------------- #
# Task 2：host id / PKCE / loopback / authorize URL
# --------------------------------------------------------------------------- #

_host_id_lock = threading.Lock()


def host_id() -> str:
    with _host_id_lock:
        existing = secret("codex_host_id")
        if existing:
            return existing
        value = "urn:uuid:" + str(uuid.uuid4())
        secret("codex_host_id", value)
        return value


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def new_pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    return verifier, pkce_challenge(verifier)


def build_authorize_url(
    *,
    client_id: str,
    host_id: str,
    state: str,
    nonce: str,
    challenge: str,
    redirect_uri: str,
    agent_name_hint: str | None = None,
    id_token_hint: str | None = None,
    login_hint: str | None = None,
) -> str:
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": SCOPE,
        "resource": RESOURCE,
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "ext_agent_host_id": host_id,
    }
    if agent_name_hint is not None:
        params["agent_name_hint"] = agent_name_hint
    if id_token_hint is not None:
        params["id_token_hint"] = id_token_hint
    if login_hint is not None:
        params["login_hint"] = login_hint
    return AUTHORIZE_ENDPOINT + "?" + urllib.parse.urlencode(params)


class _CallbackHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, *, callback_path: str, results: queue.Queue):
        self.callback_path = callback_path
        self.results = results
        super().__init__(address, handler)


class _CallbackHandler(BaseHTTPRequestHandler):
    server_version = "VibeGodotCodexAuth/1.0"

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 约定
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path != self.server.callback_path:
            self.send_error(404)
            return
        query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
        # 只回传官方回调定义的字段，避免把任意 query 带入后续流程。
        filtered = {key: value for key, value in query.items() if key in _CALLBACK_KEYS}
        self.server.results.put(filtered)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(_CALLBACK_HTML)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(_CALLBACK_HTML)

    def log_message(self, format, *args):  # noqa: A002 - 基类签名
        # 回调 query 可能含一次性 code；绝不写访问日志。
        return


class LoopbackCallback:
    """127.0.0.1 随机端口上的单次 OAuth 回调监听。"""

    def __init__(
        self,
        *,
        host: str = CALLBACK_HOST,
        path: str = CALLBACK_PATH,
    ) -> None:
        self._host = host
        self._path = path
        self._server: _CallbackHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._results: queue.Queue = queue.Queue(maxsize=1)
        self.redirect_uri: str | None = None

    def start(self) -> str:
        if self._server is not None:
            return self.redirect_uri  # type: ignore[return-value]
        server = _CallbackHTTPServer(
            (self._host, 0),
            _CallbackHandler,
            callback_path=self._path,
            results=self._results,
        )
        port = server.server_address[1]
        self._server = server
        self.redirect_uri = "http://%s:%d%s" % (self._host, port, self._path)
        self._thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread.start()
        return self.redirect_uri

    def wait(self, timeout_s: int = CALLBACK_TIMEOUT_S) -> dict[str, str]:
        try:
            return self._results.get(timeout=timeout_s)
        except queue.Empty:
            raise CodexAuthError("callback_timeout") from None

    def close(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2)


# --------------------------------------------------------------------------- #
# Task 3：回调校验 / code 兑换 / id_token 验签 / scope 判定
# --------------------------------------------------------------------------- #

def validate_callback(
    query: dict[str, str],
    *,
    expected_state: str,
    issued_client_id_expected: bool,
) -> dict[str, str]:
    error = query.get("error")
    if error:
        if error == "access_denied":
            raise CodexAuthError("access_denied")
        raise CodexAuthError("callback_error")
    state = query.get("state")
    if not state or state != expected_state:
        raise CodexAuthError("state_mismatch")
    code = query.get("code")
    if not code:
        raise CodexAuthError("missing_code")
    client_id = query.get("client_id")
    if issued_client_id_expected and not client_id:
        raise CodexAuthError("missing_client_id")
    return {
        "code": code,
        "state": state,
        "client_id": client_id or "",
        "scope": query.get("scope", ""),
    }


def exchange_code(
    *,
    client_id: str,
    code: str,
    verifier: str,
    redirect_uri: str,
) -> dict[str, Any]:
    data = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": redirect_uri,
        "resource": RESOURCE,
    }
    try:
        response = _post_form(TOKEN_ENDPOINT, data)
    except _HttpError as exc:
        raise _oauth_http_error(exc, "token_request_failed") from None
    error = _oauth_error(response)
    if error is not None:
        raise error
    return response


def plan_enabled(scopes: Any) -> bool:
    if isinstance(scopes, str):
        items = scopes.split()
    else:
        items = list(scopes or ())
    return DIRECT_SCOPE in items


def _jwk_public_key(jwk: dict[str, Any]):
    kty = jwk.get("kty")
    if kty == "RSA":
        n = _b64url_uint(jwk["n"])
        e = _b64url_uint(jwk["e"])
        return rsa.RSAPublicNumbers(e, n).public_key()
    if kty == "EC":
        if jwk.get("crv") != "P-256":
            raise CodexAuthError("unsupported_curve")
        x = _b64url_uint(jwk["x"])
        y = _b64url_uint(jwk["y"])
        return ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()
    raise CodexAuthError("unsupported_key")


def _select_jwk(jwks: dict[str, Any], kid: str | None, alg: str) -> dict[str, Any] | None:
    keys = (jwks or {}).get("keys", [])
    candidates = [key for key in keys if isinstance(key, dict)]
    if kid:
        candidates = [key for key in candidates if key.get("kid") == kid]
    for key in candidates:
        if alg == "RS256" and key.get("kty") == "RSA":
            return key
        if alg == "ES256" and key.get("kty") == "EC":
            return key
    return None


def _verify_signature(alg: str, jwk: dict[str, Any], signature: bytes, signing_input: bytes) -> None:
    public_key = _jwk_public_key(jwk)
    if alg == "RS256":
        public_key.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
        return
    if len(signature) != 64:
        raise CodexAuthError("bad_signature")
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    public_key.verify(encode_dss_signature(r, s), signing_input, ec.ECDSA(hashes.SHA256()))


def validate_id_token(
    id_token: str,
    *,
    jwks: dict[str, Any],
    client_id: str,
    nonce: str | None,
    issuer: str,
) -> dict[str, Any]:
    if not _CRYPTO_AVAILABLE:
        raise CodexAuthError("cryptography_unavailable")
    if not isinstance(id_token, str):
        raise CodexAuthError("invalid_id_token")
    parts = id_token.split(".")
    if len(parts) != 3:
        raise CodexAuthError("invalid_id_token")
    header_segment, payload_segment, signature_segment = parts
    try:
        header = json.loads(_b64url_decode(header_segment).decode("utf-8"))
        payload = json.loads(_b64url_decode(payload_segment).decode("utf-8"))
        signature = _b64url_decode(signature_segment)
    except Exception:
        raise CodexAuthError("invalid_id_token") from None
    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise CodexAuthError("invalid_id_token")
    alg = header.get("alg")
    if alg not in _ALLOWED_ALGS:
        raise CodexAuthError("unsupported_alg")
    jwk = _select_jwk(jwks, header.get("kid"), alg)
    if jwk is None:
        raise CodexAuthError("unknown_key")
    signing_input = (header_segment + "." + payload_segment).encode("ascii")
    try:
        _verify_signature(alg, jwk, signature, signing_input)
    except InvalidSignature:
        raise CodexAuthError("bad_signature") from None
    except CodexAuthError:
        raise
    except Exception:
        raise CodexAuthError("bad_signature") from None
    if issuer is not None and payload.get("iss") != issuer:
        raise CodexAuthError("bad_issuer")
    audience = payload.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    if client_id not in audiences:
        raise CodexAuthError("bad_audience")
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or isinstance(exp, bool) or exp <= _now():
        raise CodexAuthError("token_expired")
    if nonce is not None and payload.get("nonce") != nonce:
        raise CodexAuthError("bad_nonce")
    return payload


# --------------------------------------------------------------------------- #
# Task 4：刷新轮换 / 撤销 / 账号构造
# --------------------------------------------------------------------------- #

def _refresh_lock(account_id: str) -> threading.Lock:
    with _REFRESH_LOCKS_GUARD:
        lock = _REFRESH_LOCKS.get(account_id)
        if lock is None:
            lock = threading.Lock()
            _REFRESH_LOCKS[account_id] = lock
        return lock


def _account_from_tokens(
    account: CodexAccount,
    response: dict[str, Any],
    *,
    epoch: float,
) -> CodexAccount:
    scopes_raw = response.get("scope")
    scopes = tuple(str(scopes_raw).split()) if scopes_raw else account.scopes
    expires_in = int(response.get("expires_in", account.expires_in) or account.expires_in)
    return replace(
        account,
        id_token=response.get("id_token") or account.id_token,
        access_token=response.get("access_token") or account.access_token,
        refresh_token=response.get("refresh_token") or account.refresh_token,
        token_type=response.get("token_type") or account.token_type,
        expires_in=expires_in,
        earliest_refresh_at=int(epoch) + expires_in - REFRESH_SKEW_S,
        scopes=scopes,
        saved_at=_iso(epoch),
        plan_enabled=plan_enabled(scopes),
    )


def refresh(account: CodexAccount) -> CodexAccount:
    lock = _refresh_lock(account.account_id)
    with lock:
        epoch = _now()
        data = {
            "grant_type": "refresh_token",
            "client_id": account.client_id,
            "refresh_token": account.refresh_token,
            "resource": RESOURCE,
        }
        try:
            response = _post_form(TOKEN_ENDPOINT, data)
        except _HttpError as exc:
            raise _oauth_http_error(exc, "refresh_request_failed") from None
        error = _oauth_error(response)
        if error is not None:
            raise error
        updated = _account_from_tokens(account, response, epoch=epoch)
        logger.debug("codex token refresh ok account_id=%s", account.account_id)
        return updated


def revoke(account: CodexAccount) -> bool:
    try:
        metadata = discover()
    except CodexAuthError:
        metadata = {}
    endpoint = metadata.get("revocation_endpoint")
    if not endpoint:
        logger.debug("codex revoke skipped account_id=%s", account.account_id)
        return False
    data = {
        "token": account.refresh_token,
        "token_type_hint": "refresh_token",
        "client_id": account.client_id,
    }
    for attempt in range(REVOKE_ATTEMPTS):
        try:
            _post_form(endpoint, data)
            logger.debug("codex revoke ok account_id=%s", account.account_id)
            return True
        except _HttpError as exc:
            if exc.status and 400 <= exc.status < 500:
                logger.debug("codex revoke rejected account_id=%s", account.account_id)
                return False
            if attempt + 1 < REVOKE_ATTEMPTS:
                _sleep(REVOKE_BACKOFF_S * (attempt + 1))
        except CodexAuthError:
            if attempt + 1 < REVOKE_ATTEMPTS:
                _sleep(REVOKE_BACKOFF_S * (attempt + 1))
    logger.debug("codex revoke unconfirmed account_id=%s", account.account_id)
    return False
