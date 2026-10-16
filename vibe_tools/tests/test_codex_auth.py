from __future__ import annotations

import base64
import io
import json
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import patch

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from vibe_tools import codex_auth as ca


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _sign_rs256(private_key, payload: dict, *, kid: str = "test-kid") -> str:
    header = {"alg": "RS256", "typ": "JWT", "kid": kid}
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
        + "."
        + _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    ).encode("ascii")
    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return signing_input.decode("ascii") + "." + _b64url(signature)


def _sign_es256(private_key, payload: dict, *, kid: str = "test-kid") -> str:
    header = {"alg": "ES256", "typ": "JWT", "kid": kid}
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
        + "."
        + _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    ).encode("ascii")
    der = private_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return signing_input.decode("ascii") + "." + _b64url(raw)


def _rsa_jwks(public_key, *, kid: str = "test-kid") -> dict:
    numbers = public_key.public_numbers()
    return {
        "keys": [
            {
                "kty": "RSA",
                "kid": kid,
                "use": "sig",
                "alg": "RS256",
                "n": _b64url(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
                "e": _b64url(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
            }
        ]
    }


def _ec_jwks(public_key, *, kid: str = "test-kid") -> dict:
    numbers = public_key.public_numbers()
    return {
        "keys": [
            {
                "kty": "EC",
                "kid": kid,
                "use": "sig",
                "alg": "ES256",
                "crv": "P-256",
                "x": _b64url(numbers.x.to_bytes(32, "big")),
                "y": _b64url(numbers.y.to_bytes(32, "big")),
            }
        ]
    }


class _TrackingBytesIO(io.BytesIO):
    """记录 read() 传入的 size，用于验证响应体是有界读取。"""

    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return super().read(size)


def _http_error(code: int, body: bytes = b"") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        ca.TOKEN_ENDPOINT,
        code,
        "error",
        {"Content-Type": "application/json"},
        io.BytesIO(body),
    )


def make_account(**overrides) -> ca.CodexAccount:
    values = dict(
        account_id="AbC123_-xyz",
        label="测试账号",
        email="user@example.com",
        issuer=ca.ISSUER,
        subject="subject-1",
        client_id="issued-client",
        ext_agent_host_id="urn:uuid:host",
        id_token="PLACEHOLDER-ID",
        access_token="PLACEHOLDER-ACCESS",
        refresh_token="PLACEHOLDER-REFRESH",
        token_type="Bearer",
        expires_in=3600,
        earliest_refresh_at=None,
        scopes=(ca.DIRECT_SCOPE,),
        saved_at="2026-10-16T00:00:00+00:00",
        plan_enabled=True,
    )
    values.update(overrides)
    return ca.CodexAccount(**values)


class PkceTest(unittest.TestCase):
    def test_s256_against_rfc7636_vector(self):
        verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        self.assertEqual(
            ca.pkce_challenge(verifier),
            "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        )

    def test_new_pkce_is_url_safe_and_matches(self):
        verifier, challenge = ca.new_pkce()
        self.assertGreaterEqual(len(verifier), 43)
        self.assertLessEqual(len(verifier), 128)
        self.assertNotIn("=", verifier)
        self.assertEqual(challenge, ca.pkce_challenge(verifier))


class AuthorizeUrlTest(unittest.TestCase):
    def _qs(self, url):
        return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))

    def test_new_registration_params(self):
        url = ca.build_authorize_url(
            client_id=ca.DYNAMIC_CLIENT_ID,
            host_id="urn:uuid:abc",
            state="S",
            nonce="N",
            challenge="C",
            redirect_uri="http://127.0.0.1:1455/auth/callback",
            agent_name_hint=ca.AGENT_NAME_HINT,
        )
        q = self._qs(url)
        self.assertEqual(q["client_id"], "dynamic_agent_client")
        self.assertEqual(q["agent_name_hint"], "VibeGodot")
        self.assertEqual(q["ext_agent_host_id"], "urn:uuid:abc")
        self.assertEqual(q["scope"], ca.SCOPE)
        self.assertEqual(q["resource"], ca.RESOURCE)
        self.assertEqual(q["code_challenge_method"], "S256")
        self.assertEqual(q["response_type"], "code")
        self.assertEqual(q["state"], "S")
        self.assertEqual(q["nonce"], "N")
        self.assertNotIn("localhost", url)
        self.assertEqual(urllib.parse.urlparse(url).path, "/api/accounts/authorize")

    def test_reauth_omits_agent_name_hint_and_keeps_host(self):
        q = self._qs(
            ca.build_authorize_url(
                client_id="oaiapp_x",
                host_id="urn:uuid:abc",
                state="S",
                nonce="N",
                challenge="C",
                redirect_uri="http://127.0.0.1:2/auth/callback",
                id_token_hint="IDT",
                login_hint="u@example.com",
            )
        )
        self.assertNotIn("agent_name_hint", q)
        self.assertEqual(q["id_token_hint"], "IDT")
        self.assertEqual(q["login_hint"], "u@example.com")
        self.assertEqual(q["ext_agent_host_id"], "urn:uuid:abc")


class HostIdTest(unittest.TestCase):
    def test_generates_urn_uuid_once_and_reuses(self):
        store = {}

        def fake_secret(name, value=None):
            if value is None:
                return store.get(name, "")
            store[name] = value
            return value

        with patch.object(ca, "secret", side_effect=fake_secret):
            first = ca.host_id()
            second = ca.host_id()
        self.assertTrue(first.startswith("urn:uuid:"))
        self.assertEqual(first, second)
        self.assertEqual(store["codex_host_id"], first)

    def test_reuses_existing_host_id(self):
        with patch.object(ca, "secret", return_value="urn:uuid:existing") as mocked:
            self.assertEqual(ca.host_id(), "urn:uuid:existing")
        self.assertEqual(mocked.call_count, 1)


class LoopbackCallbackTest(unittest.TestCase):
    def test_receives_query_and_closes_idempotently(self):
        callback = ca.LoopbackCallback()
        redirect = callback.start()
        try:
            self.assertTrue(redirect.startswith("http://127.0.0.1:"))
            self.assertTrue(redirect.endswith("/auth/callback"))
            self.assertNotIn("localhost", redirect)
            with urllib.request.urlopen(redirect + "?code=CODE&state=S&client_id=cid", timeout=5) as response:
                self.assertEqual(response.status, 200)
            result = callback.wait(5)
            self.assertEqual(result["code"], "CODE")
            self.assertEqual(result["state"], "S")
            self.assertEqual(result["client_id"], "cid")
        finally:
            callback.close()
            callback.close()

    def test_wrong_path_is_not_captured_then_timeout(self):
        callback = ca.LoopbackCallback()
        redirect = callback.start()
        try:
            root = redirect.rsplit("/auth/callback", 1)[0] + "/other"
            try:
                urllib.request.urlopen(root, timeout=5)
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 404)
            with self.assertRaises(ca.CodexAuthError) as ctx:
                callback.wait(0.1)
            self.assertEqual(str(ctx.exception), "callback_timeout")
        finally:
            callback.close()


class CallbackValidationTest(unittest.TestCase):
    def test_success(self):
        result = ca.validate_callback(
            {"code": "CODE", "state": "S", "client_id": "cid", "scope": ca.SCOPE},
            expected_state="S",
            issued_client_id_expected=True,
        )
        self.assertEqual(result["code"], "CODE")
        self.assertEqual(result["client_id"], "cid")
        self.assertEqual(result["scope"], ca.SCOPE)

    def test_state_mismatch(self):
        with self.assertRaises(ca.CodexAuthError) as ctx:
            ca.validate_callback({"code": "CODE", "state": "other", "client_id": "cid"},
                                 expected_state="S", issued_client_id_expected=True)
        self.assertEqual(str(ctx.exception), "state_mismatch")

    def test_access_denied(self):
        with self.assertRaises(ca.CodexAuthError) as ctx:
            ca.validate_callback({"error": "access_denied", "state": "S"},
                                 expected_state="S", issued_client_id_expected=True)
        self.assertEqual(str(ctx.exception), "access_denied")

    def test_missing_client_id(self):
        with self.assertRaises(ca.CodexAuthError) as ctx:
            ca.validate_callback({"code": "CODE", "state": "S"},
                                 expected_state="S", issued_client_id_expected=True)
        self.assertEqual(str(ctx.exception), "missing_client_id")

    def test_missing_code(self):
        with self.assertRaises(ca.CodexAuthError) as ctx:
            ca.validate_callback({"state": "S", "client_id": "cid"},
                                 expected_state="S", issued_client_id_expected=True)
        self.assertEqual(str(ctx.exception), "missing_code")


class ExchangeCodeTest(unittest.TestCase):
    def test_posts_form_without_client_secret(self):
        response = {"access_token": "PLACEHOLDER-ACCESS", "refresh_token": "PLACEHOLDER-REFRESH"}
        with patch.object(ca, "_post_form", return_value=response) as post:
            result = ca.exchange_code(
                client_id="issued-client",
                code="CODE",
                verifier="VER",
                redirect_uri="http://127.0.0.1:1/auth/callback",
            )
        self.assertEqual(result, response)
        url, data = post.call_args.args
        self.assertEqual(url, ca.TOKEN_ENDPOINT)
        self.assertEqual(data["grant_type"], "authorization_code")
        self.assertEqual(data["client_id"], "issued-client")
        self.assertEqual(data["code"], "CODE")
        self.assertEqual(data["code_verifier"], "VER")
        self.assertEqual(data["redirect_uri"], "http://127.0.0.1:1/auth/callback")
        self.assertEqual(data["resource"], ca.RESOURCE)
        self.assertNotIn("client_secret", data)

    def test_maps_oauth_error_without_secret(self):
        with patch.object(ca, "_post_form", return_value={"error": "invalid_grant"}):
            with self.assertRaises(ca.CodexAuthError) as ctx:
                ca.exchange_code(client_id="c", code="CODE", verifier="VER", redirect_uri="r")
        self.assertEqual(str(ctx.exception), "invalid_grant")

    def test_http_error_preserves_official_oauth_code(self):
        body = json.dumps({
            "error": "invalid_grant",
            "error_description": "DO-NOT-LEAK-DESCRIPTION",
        }).encode("utf-8")
        with patch.object(urllib.request, "urlopen", side_effect=_http_error(400, body)):
            with self.assertRaises(ca.CodexAuthError) as ctx:
                ca.exchange_code(client_id="c", code="CODE", verifier="VER", redirect_uri="r")
        self.assertEqual(str(ctx.exception), "invalid_grant")
        self.assertNotIn("DO-NOT-LEAK-DESCRIPTION", str(ctx.exception))

    def test_http_error_unsafe_code_falls_back_to_generic(self):
        body = json.dumps({"error": "Invalid Grant"}).encode("utf-8")
        with patch.object(urllib.request, "urlopen", side_effect=_http_error(400, body)):
            with self.assertRaises(ca.CodexAuthError) as ctx:
                ca.exchange_code(client_id="c", code="CODE", verifier="VER", redirect_uri="r")
        self.assertEqual(str(ctx.exception), "token_request_failed")

    def test_http_error_code_with_trailing_newline_falls_back_to_generic(self):
        body = json.dumps({"error": "invalid_grant\n"}).encode("utf-8")
        with patch.object(urllib.request, "urlopen", side_effect=_http_error(400, body)):
            with self.assertRaises(ca.CodexAuthError) as ctx:
                ca.exchange_code(client_id="c", code="CODE", verifier="VER", redirect_uri="r")
        self.assertEqual(str(ctx.exception), "token_request_failed")

    def test_http_error_invalid_json_falls_back_to_generic(self):
        with patch.object(
            urllib.request, "urlopen", side_effect=_http_error(400, b"<html>oops</html>")
        ):
            with self.assertRaises(ca.CodexAuthError) as ctx:
                ca.exchange_code(client_id="c", code="CODE", verifier="VER", redirect_uri="r")
        self.assertEqual(str(ctx.exception), "token_request_failed")

    def test_http_error_missing_code_falls_back_to_generic(self):
        body = json.dumps({"error_description": "DO-NOT-LEAK-DESCRIPTION"}).encode("utf-8")
        with patch.object(urllib.request, "urlopen", side_effect=_http_error(400, body)):
            with self.assertRaises(ca.CodexAuthError) as ctx:
                ca.exchange_code(client_id="c", code="CODE", verifier="VER", redirect_uri="r")
        self.assertEqual(str(ctx.exception), "token_request_failed")
        self.assertNotIn("DO-NOT-LEAK-DESCRIPTION", str(ctx.exception))

    def test_network_error_falls_back_to_generic(self):
        with patch.object(
            urllib.request, "urlopen", side_effect=urllib.error.URLError("boom")
        ):
            with self.assertRaises(ca.CodexAuthError) as ctx:
                ca.exchange_code(client_id="c", code="CODE", verifier="VER", redirect_uri="r")
        self.assertEqual(str(ctx.exception), "token_request_failed")

    def test_http_error_body_read_is_bounded(self):
        body = _TrackingBytesIO(json.dumps({"error": "invalid_grant"}).encode("utf-8"))
        error = urllib.error.HTTPError(
            ca.TOKEN_ENDPOINT, 400, "error", {"Content-Type": "application/json"}, body
        )
        with patch.object(urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(ca.CodexAuthError) as ctx:
                ca.exchange_code(client_id="c", code="CODE", verifier="VER", redirect_uri="r")
        self.assertEqual(str(ctx.exception), "invalid_grant")
        self.assertTrue(body.read_sizes)
        self.assertTrue(all(size >= 0 for size in body.read_sizes))


class IdTokenTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.public_key = cls.private_key.public_key()
        cls.jwks = _rsa_jwks(cls.public_key, kid="test-kid")
        cls.issuer = ca.ISSUER
        cls.client_id = "issued-client"
        cls.nonce = "nonce-value"

    def _payload(self, **overrides):
        now = int(time.time())
        payload = {
            "iss": self.issuer,
            "aud": self.client_id,
            "sub": "subject-1",
            "exp": now + 3600,
            "iat": now,
            "nonce": self.nonce,
            "email": "user@example.com",
        }
        payload.update(overrides)
        return payload

    def _validate(self, token, **overrides):
        kwargs = dict(jwks=self.jwks, client_id=self.client_id, nonce=self.nonce, issuer=self.issuer)
        kwargs.update(overrides)
        return ca.validate_id_token(token, **kwargs)

    def test_valid_rs256_token(self):
        claims = self._validate(_sign_rs256(self.private_key, self._payload()))
        self.assertEqual(claims["sub"], "subject-1")

    def test_valid_es256_token(self):
        key = ec.generate_private_key(ec.SECP256R1())
        jwks = _ec_jwks(key.public_key(), kid="ec-kid")
        token = _sign_es256(key, self._payload(), kid="ec-kid")
        claims = self._validate(token, jwks=jwks)
        self.assertEqual(claims["sub"], "subject-1")

    def test_rejects_alg_none(self):
        with self.assertRaises(ca.CodexAuthError):
            self._validate("eyJhbGciOiJub25lIn0.e30.")

    def test_rejects_hs256(self):
        header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode("utf-8"))
        payload = _b64url(json.dumps(self._payload()).encode("utf-8"))
        with self.assertRaises(ca.CodexAuthError):
            self._validate(header + "." + payload + "." + _b64url(b"sig"))

    def test_rejects_bad_signature(self):
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        with self.assertRaises(ca.CodexAuthError):
            self._validate(_sign_rs256(other, self._payload()))

    def test_rejects_unknown_kid(self):
        token = _sign_rs256(self.private_key, self._payload(), kid="other-kid")
        with self.assertRaises(ca.CodexAuthError):
            self._validate(token)

    def test_rejects_bad_issuer(self):
        with self.assertRaises(ca.CodexAuthError):
            self._validate(_sign_rs256(self.private_key, self._payload(iss="https://evil.example")))

    def test_rejects_bad_audience(self):
        with self.assertRaises(ca.CodexAuthError):
            self._validate(_sign_rs256(self.private_key, self._payload(aud="other-client")))

    def test_rejects_expired(self):
        with self.assertRaises(ca.CodexAuthError):
            self._validate(_sign_rs256(self.private_key, self._payload(exp=int(time.time()) - 10)))

    def test_rejects_bad_nonce(self):
        with self.assertRaises(ca.CodexAuthError):
            self._validate(_sign_rs256(self.private_key, self._payload(nonce="other-nonce")))

    def test_error_message_never_contains_token(self):
        token = _sign_rs256(self.private_key, self._payload(nonce="other-nonce"))
        with self.assertRaises(ca.CodexAuthError) as ctx:
            self._validate(token)
        self.assertNotIn(token, str(ctx.exception))


class PlanEnabledTest(unittest.TestCase):
    def test_detects_direct_scope(self):
        self.assertTrue(ca.plan_enabled(ca.SCOPE))
        self.assertTrue(ca.plan_enabled(["openid", ca.DIRECT_SCOPE]))
        self.assertFalse(ca.plan_enabled("openid profile email"))
        self.assertFalse(ca.plan_enabled(()))


class RefreshTest(unittest.TestCase):
    def _ok_response(self):
        return {
            "access_token": "PLACEHOLDER-ACCESS-2",
            "refresh_token": "PLACEHOLDER-REFRESH-2",
            "expires_in": 3600,
            "scope": ca.SCOPE,
            "token_type": "Bearer",
            "id_token": "PLACEHOLDER-ID-2",
        }

    def test_refresh_rotates_and_omits_scope(self):
        account = make_account()
        with patch.object(ca, "_post_form", return_value=self._ok_response()) as post, \
             patch.object(ca, "_now", return_value=1000.0):
            updated = ca.refresh(account)
        url, data = post.call_args.args
        self.assertEqual(url, ca.TOKEN_ENDPOINT)
        self.assertEqual(data["grant_type"], "refresh_token")
        self.assertEqual(data["client_id"], account.client_id)
        self.assertEqual(data["refresh_token"], account.refresh_token)
        self.assertEqual(data["resource"], ca.RESOURCE)
        self.assertNotIn("scope", data)
        self.assertEqual(updated.access_token, "PLACEHOLDER-ACCESS-2")
        self.assertEqual(updated.refresh_token, "PLACEHOLDER-REFRESH-2")
        self.assertEqual(updated.id_token, "PLACEHOLDER-ID-2")
        self.assertEqual(updated.earliest_refresh_at, 1000 + 3600 - ca.REFRESH_SKEW_S)
        self.assertTrue(updated.plan_enabled)

    def test_refresh_keeps_old_refresh_token_when_not_rotated(self):
        account = make_account()
        response = {"access_token": "PLACEHOLDER-ACCESS-2", "expires_in": 3600, "scope": ca.SCOPE}
        with patch.object(ca, "_post_form", return_value=response):
            updated = ca.refresh(account)
        self.assertEqual(updated.refresh_token, account.refresh_token)

    def test_refresh_maps_error_codes(self):
        account = make_account()
        for code in (
            "invalid_grant",
            "invalid_refresh_token",
            "token_expired",
            "refresh_token_expired",
            "refresh_token_invalidated",
            "refresh_token_reused",
            "invalid_client",
        ):
            with patch.object(ca, "_post_form", return_value={"error": code, "error_description": "ignored"}):
                with self.assertRaises(ca.CodexAuthError) as ctx:
                    ca.refresh(account)
            self.assertEqual(str(ctx.exception), code)
            self.assertNotIn(account.refresh_token, str(ctx.exception))

    def test_refresh_http_error_is_codex_auth_error(self):
        account = make_account()
        with patch.object(ca, "_post_form", side_effect=ca._HttpError(503)):
            with self.assertRaises(ca.CodexAuthError):
                ca.refresh(account)

    def test_refresh_http_error_preserves_official_oauth_code(self):
        account = make_account()
        body = json.dumps({
            "error": "invalid_grant",
            "error_description": "DO-NOT-LEAK-DESCRIPTION",
        }).encode("utf-8")
        with patch.object(urllib.request, "urlopen", side_effect=_http_error(400, body)):
            with self.assertRaises(ca.CodexAuthError) as ctx:
                ca.refresh(account)
        self.assertEqual(str(ctx.exception), "invalid_grant")
        self.assertNotIn("DO-NOT-LEAK-DESCRIPTION", str(ctx.exception))
        self.assertNotIn(account.refresh_token, str(ctx.exception))

    def test_refresh_http_error_without_code_is_generic(self):
        account = make_account()
        with patch.object(urllib.request, "urlopen", side_effect=_http_error(400, b"not-json")):
            with self.assertRaises(ca.CodexAuthError) as ctx:
                ca.refresh(account)
        self.assertEqual(str(ctx.exception), "refresh_request_failed")

    def test_refresh_http_error_unsafe_code_falls_back_to_generic(self):
        account = make_account()
        body = json.dumps({"error": "invalid_grant\nLEAK"}).encode("utf-8")
        with patch.object(urllib.request, "urlopen", side_effect=_http_error(400, body)):
            with self.assertRaises(ca.CodexAuthError) as ctx:
                ca.refresh(account)
        self.assertEqual(str(ctx.exception), "refresh_request_failed")

    def test_refresh_does_not_log_secrets(self):
        account = make_account(access_token="AT-SECRET", refresh_token="RT-SECRET", id_token="ID-SECRET")
        with self.assertLogs("vibe_tools.codex_auth", level="DEBUG") as cm:
            with patch.object(ca, "_post_form", return_value=self._ok_response()):
                ca.refresh(account)
        joined = "\n".join(cm.output)
        self.assertNotIn("AT-SECRET", joined)
        self.assertNotIn("RT-SECRET", joined)
        self.assertNotIn("ID-SECRET", joined)


class RevokeTest(unittest.TestCase):
    def test_revoke_success(self):
        account = make_account()
        with patch.object(ca, "discover", return_value={"revocation_endpoint": "https://auth.openai.com/revoke"}), \
             patch.object(ca, "_post_form", return_value={}) as post:
            self.assertTrue(ca.revoke(account))
        url, data = post.call_args.args
        self.assertEqual(url, "https://auth.openai.com/revoke")
        self.assertEqual(data["token"], account.refresh_token)
        self.assertEqual(data["token_type_hint"], "refresh_token")
        self.assertEqual(data["client_id"], account.client_id)

    def test_revoke_retries_then_false_on_5xx(self):
        account = make_account()
        with patch.object(ca, "discover", return_value={"revocation_endpoint": "https://auth.openai.com/revoke"}), \
             patch.object(ca, "_post_form", side_effect=ca._HttpError(503)) as post, \
             patch.object(ca, "_sleep", return_value=None):
            self.assertFalse(ca.revoke(account))
        self.assertGreater(post.call_count, 1)
        self.assertLessEqual(post.call_count, ca.REVOKE_ATTEMPTS)

    def test_revoke_missing_endpoint_returns_false(self):
        account = make_account()
        with patch.object(ca, "discover", return_value={}):
            self.assertFalse(ca.revoke(account))

    def test_revoke_does_not_log_secrets(self):
        account = make_account(refresh_token="RT-SECRET")
        with self.assertLogs("vibe_tools.codex_auth", level="DEBUG") as cm:
            with patch.object(ca, "discover", return_value={}):
                ca.revoke(account)
        self.assertNotIn("RT-SECRET", "\n".join(cm.output))


class DiscoverTest(unittest.TestCase):
    def test_discovery_is_cached(self):
        ca._DISCOVERY_CACHE = None
        with patch.object(ca, "_get_json", return_value={"jwks_uri": "https://x/jwks"}) as getter:
            first = ca.discover()
            second = ca.discover()
        self.assertEqual(first, second)
        self.assertEqual(getter.call_count, 1)
        ca._DISCOVERY_CACHE = None


if __name__ == "__main__":
    unittest.main()
