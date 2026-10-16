from __future__ import annotations

import contextlib
import json
import os
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from vibe_tools import editor_sidecar as sidecar_module
from vibe_tools.editor_credentials import CredentialError
from vibe_tools.editor_harness import AcceptanceHarness
from vibe_tools.editor_sidecar import Runtime, app
from vibe_tools.editor_workspace import Workspace
from vibe_tools.tests.test_editor_behavior import move_case


def _reset_auth_state() -> None:
    Runtime.auth_mode = "api_key"
    Runtime.auth_attempts = {}
    Runtime.chatgpt_account_id = None
    Runtime.chatgpt_account = None
    Runtime.app_server_status = "stopped"
    Runtime.app_server_account_id = None
    Runtime.codex_provider = None
    Runtime.codex_providers = []
    Runtime.codex_probed = True
    Runtime.codex_exe = ""
    Runtime.codex_version = ""


class EditorSidecarTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name) / "中文 project"
        self.project.mkdir()
        (self.project / "project.godot").write_text("config_version=5\n", encoding="utf-8")
        Runtime.project = self.project.resolve()
        Runtime.token = "sidecar-token"
        Runtime.parent_pid = 123
        Runtime.godot_exe = ""
        Runtime.runs = {}
        _reset_auth_state()
        self.client = TestClient(app)
        self.headers = {"Authorization": "Bearer sidecar-token"}
        self.credentials = patch("vibe_tools.editor_sidecar.credential", return_value="")
        self.credentials.start()
        self.backend = patch.object(sidecar_module, "credential_backend", return_value="macos")
        self.backend.start()
        self.state_dir = patch.dict(
            os.environ, {"VIBEGODOT_CODEX_STATE_DIR": str(Path(self.temp.name) / "codex-state"),
                         "GODOT_EXE": os.environ.get("GODOT_EXE") or sys.executable})
        self.state_dir.start()
        self.preflight = patch.object(AcceptanceHarness, "_preflight", return_value={"status": "blocked", "summary": "Godot unavailable in unit test"})
        self.preflight.start()

    def tearDown(self):
        self.preflight.stop()
        self.state_dir.stop()
        self.backend.stop()
        self.credentials.stop()
        _reset_auth_state()
        self.temp.cleanup()

    def test_project_bound_health_and_authentication(self):
        self.assertEqual(self.client.get("/v1/health").status_code, 401)
        response = self.client.get("/v1/health", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Path(response.json()["project"]), self.project.resolve())
        self.assertEqual(response.json()["protocol_version"], 1)

    def test_attachment_is_deduplicated_outside_export_tree(self):
        image = Path(self.temp.name) / "截图.png"
        image.write_bytes(b"fake-png")
        body = {"paths": [str(image), str(image)]}
        response = self.client.post("/v1/attachments", headers=self.headers, json=body)
        self.assertEqual(response.status_code, 200, response.text)
        values = response.json()["attachments"]
        self.assertEqual(values[0]["id"], values[1]["id"])
        self.assertTrue(values[0]["project_path"].startswith(".godot/agent/attachments/"))

    def test_asset_import_uses_res_assets(self):
        image = Path(self.temp.name) / "hero.png"
        image.write_bytes(b"fake-png")
        response = self.client.post("/v1/assets/import", headers=self.headers, json={"paths": [str(image)]})
        self.assertEqual(response.status_code, 200, response.text)
        item = response.json()["assets"][0]
        self.assertTrue(item["res_path"].startswith("res://assets/"))
        self.assertTrue((self.project / item["project_path"]).is_file())

    def test_legacy_chat_history_is_returned_without_migration(self):
        history = {"messages": [{"role": "user", "content": "旧消息", "steps": []}]}
        (self.project / "chat_history.json").write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")
        response = self.client.get("/v1/chat/history", headers=self.headers)
        self.assertEqual(response.json()["messages"][0]["content"], "旧消息")

    def test_harness_marks_results_stale_after_project_change(self):
        harness = AcceptanceHarness(self.project, "")
        plan = harness.save_plan({"cases": [move_case()]})
        plan["cases"][0]["status"] = "passed"
        plan["cases"][0]["verified_fingerprint"] = "old"
        plan["verified_fingerprint"] = "old"
        harness._persist(plan)
        loaded = harness.load_plan()
        self.assertEqual(loaded["cases"][0]["status"], "stale")

    def test_last_agent_transaction_can_be_rolled_back(self):
        run_id = uuid.uuid4().hex
        workspace = Workspace(self.project, self.project / ".godot/agent", run_id)
        (workspace.stage / "created.gd").write_text("extends Node\n", encoding="utf-8")
        workspace.commit([])
        workspace.close()
        health = self.client.get("/v1/health", headers=self.headers).json()
        self.assertEqual(health["last_revertible_run_id"], run_id)
        response = self.client.post(
            "/v1/agent/rollback", headers=self.headers,
            json={"run_id": run_id, "dirty_files": []},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["changed_files"], ["created.gd"])
        self.assertFalse((self.project / "created.gd").exists())

    def test_rollback_refuses_to_overwrite_a_later_user_change(self):
        run_id = uuid.uuid4().hex
        workspace = Workspace(self.project, self.project / ".godot/agent", run_id)
        (workspace.stage / "project.godot").write_text("agent\n", encoding="utf-8")
        workspace.commit([])
        workspace.close()
        (self.project / "project.godot").write_text("user\n", encoding="utf-8")
        response = self.client.post(
            "/v1/agent/rollback", headers=self.headers,
            json={"run_id": run_id, "dirty_files": []},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual((self.project / "project.godot").read_text(encoding="utf-8"), "user\n")

    def test_visual_case_blocks_cleanly_and_artifact_escape_is_rejected(self):
        harness = AcceptanceHarness(self.project, "")
        harness.save_plan({"cases": [{"title": "画面可见", "requires_visual": True,
                                     "steps": [{"op": "visual", "criteria": "画面可见"}]}]})
        events = []
        summary = harness.run([], lambda name, payload: events.append((name, payload)), threading.Event())
        self.assertEqual(summary["blocked"], 1)
        self.assertEqual(summary["failed"], 0)
        self.assertTrue(any(name == "harness_result" for name, _ in events))
        with self.assertRaises((ValueError, FileNotFoundError)):
            harness.artifact("../../project.godot")

    def test_harness_stream_uses_versioned_sse_events(self):
        AcceptanceHarness(self.project, "").save_plan({"cases": [move_case()]})
        with self.client.stream(
            "POST", "/v1/harness/run-stream", headers=self.headers,
            json={"case_ids": []},
        ) as response:
            body = "".join(response.iter_text())
        self.assertEqual(response.status_code, 200)
        self.assertIn("event: protocol_version", body)
        self.assertIn('"protocol_version": 1', body)
        self.assertIn("event: harness_observation", body)
        self.assertIn("event: harness_result", body)
        self.assertIn("event: done", body)


    def test_get_configure_reports_credential_store_and_legacy_fields(self):
        response = self.client.get("/v1/configure", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["credential_store"], "macos")
        self.assertIn("has_api_key", body)
        self.assertEqual(body["protocol_version"], 1)
        for key in ("response_language", "base_url", "model", "vision_model"):
            self.assertIn(key, body)

    def test_get_configure_degrades_when_keychain_read_fails(self):
        with patch.object(sidecar_module, "credential",
                          side_effect=CredentialError("无法读取 macOS 钥匙串（状态码 -25308）")):
            response = self.client.get("/v1/configure", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["has_api_key"])
        self.assertIn("-25308", response.json()["credential_error"])

    def test_post_configure_writes_key_only_when_present(self):
        with patch.object(sidecar_module, "credential", return_value="") as cred:
            response = self.client.post("/v1/configure", headers=self.headers,
                                        json={"api_key": "sk-test", "base_url": "http://x"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["credential_store"], "macos")
        cred.assert_any_call("api_key", "sk-test")

    def test_post_configure_without_key_only_reads(self):
        with patch.object(sidecar_module, "credential", return_value="") as cred:
            response = self.client.post("/v1/configure", headers=self.headers, json={})
        self.assertEqual(response.status_code, 200, response.text)
        cred.assert_called_once_with("api_key")

    def test_post_configure_write_failure_returns_500_without_key(self):
        with patch.object(sidecar_module, "credential",
                          side_effect=CredentialError("无法写入 macOS 钥匙串（状态码 -25308）")):
            response = self.client.post("/v1/configure", headers=self.headers,
                                        json={"api_key": "sk-秘密"})
        self.assertEqual(response.status_code, 500)
        self.assertIn("钥匙串", response.json()["detail"])
        self.assertNotIn("sk-秘密", response.text)

    def test_configure_rejects_change_while_task_running(self):
        Runtime.run_lock.acquire()
        try:
            response = self.client.post("/v1/configure", headers=self.headers,
                                        json={"base_url": "http://x"})
        finally:
            Runtime.run_lock.release()
        self.assertEqual(response.status_code, 409)


class _FakeLoopback:
    """Offline stand-in for ``codex_auth.LoopbackCallback``."""

    def __init__(self, query=None):
        self.query = query or {}
        self.redirect_uri = "http://127.0.0.1:4321/auth/callback"
        self.closed = False
        self.started = False

    def start(self):
        self.started = True
        return self.redirect_uri

    def wait(self, timeout_s=300):
        return dict(self.query)

    def close(self):
        self.closed = True


class _FakeJwksResponse:
    """Minimal urlopen response used to observe how much of a body is read."""

    def __init__(self, body: bytes):
        self._body = body
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        if size is None or size < 0:
            return self._body
        return self._body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class JwksResolutionTest(unittest.TestCase):
    """Regression tests for hardened JWKS discovery."""

    def test_http_url_is_rejected_without_fetching(self):
        body = json.dumps({"keys": [{"kty": "RSA"}]}).encode("utf-8")
        fake = _FakeJwksResponse(body)
        with patch.object(sidecar_module.urllib.request, "urlopen", return_value=fake) as urlopen:
            with self.assertRaises(sidecar_module.codex_auth.CodexAuthError) as ctx:
                sidecar_module._resolve_jwks({"jwks_uri": "http://example.com/jwks"})
        self.assertEqual(str(ctx.exception), "discovery_failed")
        urlopen.assert_not_called()

    def test_https_url_reads_body_with_bounded_size(self):
        body = json.dumps({"keys": [{"kty": "RSA"}]}).encode("utf-8")
        fake = _FakeJwksResponse(body)
        with patch.object(sidecar_module.urllib.request, "urlopen", return_value=fake):
            result = sidecar_module._resolve_jwks({"jwks_uri": "https://example.com/jwks"})
        self.assertEqual(result, {"keys": [{"kty": "RSA"}]})
        self.assertTrue(fake.read_sizes)
        self.assertNotEqual(fake.read_sizes[0], -1)
        self.assertLessEqual(fake.read_sizes[0], 64 * 1024 + 1)

    def test_oversized_body_is_rejected(self):
        fake = _FakeJwksResponse(b"{" * (64 * 1024 + 1))
        with patch.object(sidecar_module.urllib.request, "urlopen", return_value=fake):
            with self.assertRaises(sidecar_module.codex_auth.CodexAuthError) as ctx:
                sidecar_module._resolve_jwks({"jwks_uri": "https://example.com/jwks"})
        self.assertEqual(str(ctx.exception), "discovery_failed")
        self.assertNotEqual(fake.read_sizes[0], -1)

    def test_unparseable_body_reports_sanitized_error(self):
        secret = "SENSITIVE-JWKS-BODY"
        fake = _FakeJwksResponse(secret.encode("utf-8"))
        with patch.object(sidecar_module.urllib.request, "urlopen", return_value=fake):
            with self.assertRaises(sidecar_module.codex_auth.CodexAuthError) as ctx:
                sidecar_module._resolve_jwks({"jwks_uri": "https://secret.example.com/jwks"})
        self.assertEqual(str(ctx.exception), "discovery_failed")
        self.assertNotIn(secret, str(ctx.exception))
        self.assertNotIn("secret.example.com", str(ctx.exception))

    def test_https_jwks_fetch_uses_verifying_ssl_context(self):
        body = json.dumps({"keys": [{"kty": "RSA"}]}).encode("utf-8")
        captured = {}

        def fake_urlopen(request, timeout=None, context=None):
            captured["context"] = context
            return _FakeJwksResponse(body)

        with patch.object(sidecar_module.urllib.request, "urlopen", side_effect=fake_urlopen):
            result = sidecar_module._resolve_jwks({"jwks_uri": "https://example.com/jwks"})
        self.assertEqual(result, {"keys": [{"kty": "RSA"}]})
        context = captured["context"]
        self.assertIsInstance(context, ssl.SSLContext)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)


class ChatGptAuthTest(unittest.TestCase):
    """Offline, non-privileged tests for the SIWC auth state and endpoints."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name) / "project"
        self.project.mkdir()
        (self.project / "project.godot").write_text("config_version=5\n", encoding="utf-8")
        Runtime.project = self.project.resolve()
        Runtime.token = "sidecar-token"
        Runtime.parent_pid = 123
        Runtime.godot_exe = ""
        Runtime.runs = {}
        _reset_auth_state()
        self.client = TestClient(app)
        self.headers = {"Authorization": "Bearer sidecar-token"}
        self.credentials = patch.object(sidecar_module, "credential", return_value="")
        self.credentials.start()
        self.backend = patch.object(sidecar_module, "credential_backend", return_value="macos")
        self.backend.start()
        self.state_dir = patch.dict(
            os.environ, {"VIBEGODOT_CODEX_STATE_DIR": str(Path(self.temp.name) / "codex-state")})
        self.state_dir.start()

    def tearDown(self):
        self.state_dir.stop()
        self.backend.stop()
        self.credentials.stop()
        _reset_auth_state()
        self.temp.cleanup()

    # -- helpers ---------------------------------------------------------- #

    def _enable_chatgpt(self):
        Runtime.codex_probed = True
        Runtime.codex_exe = "/opt/codex"
        Runtime.codex_version = "1.2.3"

    def _attempt(self, login_id):
        return {
            "id": login_id, "status": "authorizing", "error": None, "account": None,
            "loopback": None, "state": "", "nonce": "", "verifier": "",
            "cancel": threading.Event(), "created": time.time(), "thread": None,
        }

    @contextlib.contextmanager
    def _login_success_stack(self, saved, *, loopback=None, exchange=None, claims=None):
        fake = loopback or _FakeLoopback(
            {"code": "CODE", "state": "STATE", "client_id": "oaiapp_issued", "scope": ""})
        token_response = exchange or {
            "id_token": "IDT", "access_token": "AT", "refresh_token": "RT",
            "token_type": "Bearer", "expires_in": 3600,
            "scope": "openid profile email chatgpt.tokens.use.direct",
        }
        claims = claims or {
            "iss": "https://auth.openai.com", "sub": "sub-1",
            "email": "user@example.com", "name": "User", "nonce": "N",
        }

        def fake_save(account):
            saved["account"] = account

        def fake_set_active(account_id):
            saved["active"] = account_id

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(sidecar_module.codex_auth, "host_id", return_value="urn:uuid:host"))
            stack.enter_context(patch.object(sidecar_module.codex_auth, "new_pkce", return_value=("verifier", "challenge")))
            stack.enter_context(patch.object(
                sidecar_module.codex_auth, "build_authorize_url",
                return_value="https://auth.openai.com/api/accounts/authorize"))
            stack.enter_context(patch.object(sidecar_module.codex_auth, "LoopbackCallback", return_value=fake))
            stack.enter_context(patch.object(
                sidecar_module.codex_auth, "validate_callback",
                return_value={"code": "CODE", "state": "STATE", "client_id": "oaiapp_issued", "scope": ""}))
            stack.enter_context(patch.object(sidecar_module.codex_auth, "exchange_code", return_value=token_response))
            stack.enter_context(patch.object(
                sidecar_module.codex_auth, "discover", return_value={"keys": [{"kty": "RSA"}]}))
            stack.enter_context(patch.object(sidecar_module.codex_auth, "validate_id_token", return_value=claims))
            stack.enter_context(patch.object(sidecar_module.codex_auth, "plan_enabled", return_value=True))
            stack.enter_context(patch.object(sidecar_module.codex_accounts, "save_account", side_effect=fake_save))
            stack.enter_context(patch.object(sidecar_module.codex_accounts, "set_active_account", side_effect=fake_set_active))
            browser = stack.enter_context(patch.object(sidecar_module.webbrowser, "open"))
            yield fake, browser

    @contextlib.contextmanager
    def _login_error_stack(self, raw_error):
        fake = _FakeLoopback({"code": "CODE", "state": "STATE", "client_id": "oaiapp_issued"})
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(sidecar_module.codex_auth, "host_id", return_value="urn:uuid:host"))
            stack.enter_context(patch.object(sidecar_module.codex_auth, "new_pkce", return_value=("verifier", "challenge")))
            stack.enter_context(patch.object(
                sidecar_module.codex_auth, "build_authorize_url",
                return_value="https://auth.openai.com/api/accounts/authorize"))
            stack.enter_context(patch.object(sidecar_module.codex_auth, "LoopbackCallback", return_value=fake))
            stack.enter_context(patch.object(
                sidecar_module.codex_auth, "validate_callback",
                return_value={"code": "CODE", "state": "STATE", "client_id": "oaiapp_issued", "scope": ""}))
            stack.enter_context(patch.object(
                sidecar_module.codex_auth, "exchange_code",
                side_effect=sidecar_module.codex_auth.CodexAuthError(raw_error)))
            stack.enter_context(patch.object(sidecar_module.codex_accounts, "save_account"))
            stack.enter_context(patch.object(sidecar_module.webbrowser, "open"))
            yield fake

    # -- configure / capability ------------------------------------------ #

    def test_configure_defaults_to_api_key_without_codex(self):
        body = self.client.get("/v1/configure", headers=self.headers).json()
        self.assertEqual(body["auth_mode"], "api_key")
        self.assertFalse(body["chatgpt_supported"])
        self.assertIsNone(body["chatgpt_account"])
        self.assertEqual(body["codex_version"], "")
        self.assertEqual(body["protocol_version"], 1)
        self.assertEqual(body["credential_store"], "macos")

    def test_configure_chatgpt_unsupported_returns_409(self):
        response = self.client.post("/v1/configure", headers=self.headers, json={"auth_mode": "chatgpt"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.client.get("/v1/configure", headers=self.headers).json()["auth_mode"], "api_key")

    def test_configure_chatgpt_supported_updates_mode(self):
        self._enable_chatgpt()
        with patch.object(sidecar_module.sys, "platform", "darwin"):
            response = self.client.post("/v1/configure", headers=self.headers, json={"auth_mode": "chatgpt"})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["auth_mode"], "chatgpt")
        self.assertTrue(body["chatgpt_supported"])
        self.assertEqual(body["codex_version"], "1.2.3")
        self.assertNotIn("auth_mode", Runtime.config)
        self.assertNotIn("chatgpt_account_id", Runtime.config)

    def test_linux_forces_api_key_and_rejects_chatgpt(self):
        self._enable_chatgpt()
        with patch.object(sidecar_module.sys, "platform", "linux"):
            body = self.client.get("/v1/configure", headers=self.headers).json()
            self.assertEqual(body["auth_mode"], "api_key")
            self.assertFalse(body["chatgpt_supported"])
            self.assertEqual(body["codex_version"], "")

            conflict = self.client.post(
                "/v1/configure", headers=self.headers, json={"auth_mode": "chatgpt"})
            self.assertEqual(conflict.status_code, 409)
            self.assertEqual(Runtime.auth_mode, "api_key")

            started = self.client.post("/v1/auth/chatgpt/start", headers=self.headers, json={})
            self.assertEqual(started.status_code, 409)
        self.assertFalse(
            any(attempt.get("status") == "authorizing" for attempt in Runtime.auth_attempts.values()))

    def test_windows_enables_chatgpt_capability(self):
        self._enable_chatgpt()
        with patch.object(sidecar_module.sys, "platform", "win32"):
            body = self.client.get("/v1/configure", headers=self.headers).json()
            self.assertTrue(body["chatgpt_supported"])
            self.assertEqual(body["codex_version"], "1.2.3")
            self.assertEqual(body["auth_mode"], "api_key")

            response = self.client.post(
                "/v1/configure", headers=self.headers, json={"auth_mode": "chatgpt"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["auth_mode"], "chatgpt")
            self.assertEqual(Runtime.auth_mode, "chatgpt")

            with patch.object(sidecar_module, "_start_login_thread") as starter:
                started = self.client.post("/v1/auth/chatgpt/start", headers=self.headers, json={})
        self.assertEqual(started.status_code, 200, started.text)
        self.assertEqual(started.json()["status"], "authorizing")
        starter.assert_called_once()

    def test_configure_stores_selected_account_id_without_tokens(self):
        self._enable_chatgpt()
        with patch.object(sidecar_module.sys, "platform", "darwin"), \
             patch.object(sidecar_module.codex_accounts, "active_account_id", return_value=None), \
             patch.object(sidecar_module.codex_accounts, "list_accounts", return_value=[]):
            response = self.client.post(
                "/v1/configure", headers=self.headers,
                json={"auth_mode": "chatgpt", "chatgpt_account_id": "acct-9"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(Runtime.chatgpt_account_id, "acct-9")
        self.assertNotIn("auth_mode", Runtime.config)
        self.assertNotIn("chatgpt_account_id", Runtime.config)
        self.assertNotIn("acct-9", json.dumps(Runtime.config))

    def test_resolve_codex_exe_prefers_env_and_requires_executable(self):
        exe = Path(self.temp.name) / "codex"
        exe.write_text("#!/bin/sh\n", encoding="utf-8")
        exe.chmod(0o755)
        with patch.dict(os.environ, {"VIBE_CODEX_BIN": str(exe), "CODEX_BIN": ""}), \
             patch.object(sidecar_module.shutil, "which", return_value="/path/from-path/codex"), \
             patch.object(sidecar_module.os.path, "expanduser", return_value=str(self.temp.name)):
            self.assertEqual(sidecar_module._resolve_codex_exe(), str(exe))

    def test_resolve_codex_exe_rejects_non_executable(self):
        exe = Path(self.temp.name) / "codex"
        exe.write_text("data", encoding="utf-8")
        exe.chmod(0o644)
        with patch.dict(os.environ, {"VIBE_CODEX_BIN": str(exe), "CODEX_BIN": ""}), \
             patch.object(sidecar_module.shutil, "which", return_value=None), \
             patch.object(sidecar_module.os.path, "expanduser",
                          return_value=str(Path(self.temp.name) / "missing-home")):
            self.assertEqual(sidecar_module._resolve_codex_exe(), "")

    def test_codex_version_returns_safe_string_only(self):
        completed = subprocess.CompletedProcess([], 0, stdout=b"codex-cli 1.2.3\n", stderr=b"secret")
        with patch.object(sidecar_module.subprocess, "run", return_value=completed):
            self.assertEqual(sidecar_module._codex_version("/fake/codex"), "1.2.3")
        with patch.object(sidecar_module.subprocess, "run", side_effect=OSError("boom")):
            self.assertEqual(sidecar_module._codex_version("/fake/codex"), "")
        with patch.object(sidecar_module.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 1, stdout=b"", stderr=b"creds")
            self.assertEqual(sidecar_module._codex_version("/fake/codex"), "")
        self.assertEqual(sidecar_module._codex_version(""), "")

    # -- authentication --------------------------------------------------- #

    def test_new_endpoints_require_bearer_token(self):
        cases = [
            ("post", "/v1/auth/chatgpt/start", {}),
            ("get", "/v1/auth/chatgpt/status?login_id=missing", None),
            ("post", "/v1/auth/chatgpt/cancel", {"login_id": "missing"}),
            ("post", "/v1/auth/chatgpt/logout", {"account_id": "missing"}),
            ("get", "/v1/auth/chatgpt/accounts", None),
        ]
        for method, path, body in cases:
            response = getattr(self.client, method)(path, json=body) if body is not None else getattr(self.client, method)(path)
            self.assertEqual(response.status_code, 401, path)

    def test_start_returns_only_login_id_and_authorizing(self):
        self._enable_chatgpt()
        with patch.object(sidecar_module, "_start_login_thread") as starter:
            response = self.client.post("/v1/auth/chatgpt/start", headers=self.headers, json={})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(set(body.keys()), {"login_id", "status"})
        self.assertEqual(body["status"], "authorizing")
        lowered = response.text.lower()
        for forbidden in ("http", "authorize", "code_challenge", "code_verifier", "access_token", "refresh_token"):
            self.assertNotIn(forbidden, lowered)
        starter.assert_called_once()
        attempt = Runtime.auth_attempts[body["login_id"]]
        self.assertEqual(attempt["status"], "authorizing")
        self.assertEqual(attempt["state"], "")
        self.assertEqual(attempt["verifier"], "")

    def test_start_unsupported_returns_409(self):
        response = self.client.post("/v1/auth/chatgpt/start", headers=self.headers, json={})
        self.assertEqual(response.status_code, 409)

    def test_start_allows_only_one_authorizing(self):
        self._enable_chatgpt()
        with patch.object(sidecar_module, "_start_login_thread"):
            first = self.client.post("/v1/auth/chatgpt/start", headers=self.headers, json={})
            second = self.client.post("/v1/auth/chatgpt/start", headers=self.headers, json={})
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 409)

    def test_status_reports_pending_then_completed_metadata(self):
        self._enable_chatgpt()
        with patch.object(sidecar_module, "_start_login_thread"):
            started = self.client.post("/v1/auth/chatgpt/start", headers=self.headers, json={}).json()
        login_id = started["login_id"]
        pending = self.client.get(
            "/v1/auth/chatgpt/status", headers=self.headers, params={"login_id": login_id}).json()
        self.assertEqual(pending["status"], "pending")
        saved = {}
        attempt = Runtime.auth_attempts[login_id]
        with self._login_success_stack(saved) as (fake, browser):
            sidecar_module._chatgpt_login_worker(attempt, None)
        self.assertEqual(attempt["status"], "completed")
        self.assertTrue(fake.closed)
        browser.assert_called_once()
        self.assertIn("authorize", browser.call_args.args[0])
        completed = self.client.get(
            "/v1/auth/chatgpt/status", headers=self.headers, params={"login_id": login_id}).json()
        self.assertEqual(completed["status"], "completed")
        account = completed["account"]
        self.assertEqual(set(account.keys()), {"account_id", "label", "email", "plan_enabled"})
        self.assertEqual(account["email"], "user@example.com")
        self.assertTrue(account["plan_enabled"])
        self.assertNotIn("AT", json.dumps(account))
        self.assertEqual(saved["active"], account["account_id"])
        self.assertEqual(Runtime.chatgpt_account_id, account["account_id"])
        self.assertEqual(attempt["state"], "")
        self.assertEqual(attempt["verifier"], "")
        self.assertEqual(attempt["nonce"], "")

    def test_status_reports_safe_failure_code(self):
        login_id = "login-failure"
        attempt = self._attempt(login_id)
        Runtime.auth_attempts[login_id] = attempt
        with self._login_error_stack("invalid_grant"):
            sidecar_module._chatgpt_login_worker(attempt, None)
        response = self.client.get(
            "/v1/auth/chatgpt/status", headers=self.headers, params={"login_id": login_id}).json()
        self.assertEqual(response["status"], "failed")
        self.assertEqual(response["error"], "invalid_grant")
        self.assertNotIn("CODE", json.dumps(response))
        self.assertNotIn("verifier", json.dumps(response))

    def test_status_maps_unsafe_error_to_generic_code(self):
        login_id = "login-unsafe"
        attempt = self._attempt(login_id)
        Runtime.auth_attempts[login_id] = attempt
        with self._login_error_stack("<script>alert(1)</script>"):
            sidecar_module._chatgpt_login_worker(attempt, None)
        response = self.client.get(
            "/v1/auth/chatgpt/status", headers=self.headers, params={"login_id": login_id}).json()
        self.assertEqual(response["status"], "failed")
        self.assertEqual(response["error"], "auth_failed")
        self.assertNotIn("<script>", json.dumps(response))

    def test_status_unknown_login_id_is_404(self):
        response = self.client.get(
            "/v1/auth/chatgpt/status", headers=self.headers, params={"login_id": "nope"})
        self.assertEqual(response.status_code, 404)

    def test_cancel_closes_loopback_and_discards_secrets(self):
        login_id = "login-cancel"
        attempt = self._attempt(login_id)
        fake = _FakeLoopback()
        attempt.update({"loopback": fake, "state": "STATE", "nonce": "NONCE", "verifier": "VERIFIER"})
        Runtime.auth_attempts[login_id] = attempt
        response = self.client.post(
            "/v1/auth/chatgpt/cancel", headers=self.headers, json={"login_id": login_id})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "cancelled"})
        self.assertTrue(fake.closed)
        self.assertTrue(attempt["cancel"].is_set())
        self.assertEqual(attempt["state"], "")
        self.assertEqual(attempt["nonce"], "")
        self.assertEqual(attempt["verifier"], "")
        status = self.client.get(
            "/v1/auth/chatgpt/status", headers=self.headers, params={"login_id": login_id}).json()
        self.assertEqual(status["status"], "cancelled")

    def test_cancel_unknown_login_id_is_404(self):
        response = self.client.post(
            "/v1/auth/chatgpt/cancel", headers=self.headers, json={"login_id": "missing"})
        self.assertEqual(response.status_code, 404)

    def test_logout_revokes_and_clears_local_state(self):
        account = object()
        deleted = {}
        with patch.object(sidecar_module.codex_accounts, "load_account", return_value=account), \
             patch.object(sidecar_module.codex_auth, "revoke", return_value=True) as revoke, \
             patch.object(sidecar_module.codex_accounts, "delete_account",
                          side_effect=lambda aid: deleted.__setitem__("id", aid)) as delete, \
             patch.object(sidecar_module.codex_accounts, "active_account_id", return_value="acct-1"), \
             patch.object(sidecar_module.codex_accounts, "set_active_account") as set_active:
            Runtime.chatgpt_account_id = "acct-1"
            Runtime.chatgpt_account = {"account_id": "acct-1", "label": "", "email": "", "plan_enabled": False}
            response = self.client.post(
                "/v1/auth/chatgpt/logout", headers=self.headers, json={"account_id": "acct-1"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"status": "revoked"})
        revoke.assert_called_once_with(account)
        self.assertEqual(deleted["id"], "acct-1")
        delete.assert_called_once_with("acct-1")
        set_active.assert_called_once_with(None)
        self.assertIsNone(Runtime.chatgpt_account_id)
        self.assertIsNone(Runtime.chatgpt_account)
        self.assertNotIn("access_token", response.text)

    def test_logout_is_unconfirmed_when_revoke_fails(self):
        with patch.object(sidecar_module.codex_accounts, "load_account", return_value=object()), \
             patch.object(sidecar_module.codex_auth, "revoke", side_effect=RuntimeError("boom")), \
             patch.object(sidecar_module.codex_accounts, "delete_account") as delete, \
             patch.object(sidecar_module.codex_accounts, "active_account_id", return_value=None):
            response = self.client.post(
                "/v1/auth/chatgpt/logout", headers=self.headers, json={"account_id": "acct-1"})
        self.assertEqual(response.json(), {"status": "revoke_unconfirmed"})
        delete.assert_called_once_with("acct-1")

    def test_logout_other_account_preserves_active_runtime_state(self):
        active_metadata = {"account_id": "acct-1", "label": "L", "email": "e@example.com", "plan_enabled": True}
        Runtime.chatgpt_account_id = "acct-1"
        Runtime.chatgpt_account = dict(active_metadata)
        Runtime.app_server_status = "running"
        Runtime.app_server_account_id = "acct-2"
        with patch.object(sidecar_module.codex_accounts, "load_account", return_value=None), \
             patch.object(sidecar_module.codex_accounts, "delete_account") as delete, \
             patch.object(sidecar_module.codex_accounts, "active_account_id", return_value="acct-1"), \
             patch.object(sidecar_module.codex_accounts, "set_active_account") as set_active:
            response = self.client.post(
                "/v1/auth/chatgpt/logout", headers=self.headers, json={"account_id": "acct-3"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"status": "revoke_unconfirmed"})
        delete.assert_called_once_with("acct-3")
        set_active.assert_not_called()
        self.assertEqual(Runtime.chatgpt_account_id, "acct-1")
        self.assertEqual(Runtime.chatgpt_account, active_metadata)
        self.assertEqual(Runtime.app_server_status, "running")
        self.assertEqual(Runtime.app_server_account_id, "acct-2")

    def test_logout_current_account_clears_only_chatgpt_state(self):
        Runtime.chatgpt_account_id = "acct-1"
        Runtime.chatgpt_account = {"account_id": "acct-1", "label": "L", "email": "e@example.com", "plan_enabled": True}
        Runtime.app_server_status = "running"
        Runtime.app_server_account_id = "acct-2"
        with patch.object(sidecar_module.codex_accounts, "load_account", return_value=None), \
             patch.object(sidecar_module.codex_accounts, "delete_account"), \
             patch.object(sidecar_module.codex_accounts, "active_account_id", return_value="acct-1"), \
             patch.object(sidecar_module.codex_accounts, "set_active_account"):
            response = self.client.post(
                "/v1/auth/chatgpt/logout", headers=self.headers, json={"account_id": "acct-1"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(Runtime.chatgpt_account_id)
        self.assertIsNone(Runtime.chatgpt_account)
        self.assertEqual(Runtime.app_server_status, "running")
        self.assertEqual(Runtime.app_server_account_id, "acct-2")

    def test_logout_app_server_account_clears_only_app_server_state(self):
        active_metadata = {"account_id": "acct-1", "label": "L", "email": "e@example.com", "plan_enabled": True}
        Runtime.chatgpt_account_id = "acct-1"
        Runtime.chatgpt_account = dict(active_metadata)
        Runtime.app_server_status = "running"
        Runtime.app_server_account_id = "acct-2"
        with patch.object(sidecar_module.codex_accounts, "load_account", return_value=None), \
             patch.object(sidecar_module.codex_accounts, "delete_account"), \
             patch.object(sidecar_module.codex_accounts, "active_account_id", return_value="acct-1"), \
             patch.object(sidecar_module.codex_accounts, "set_active_account"):
            response = self.client.post(
                "/v1/auth/chatgpt/logout", headers=self.headers, json={"account_id": "acct-2"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(Runtime.chatgpt_account_id, "acct-1")
        self.assertEqual(Runtime.chatgpt_account, active_metadata)
        self.assertEqual(Runtime.app_server_status, "stopped")
        self.assertIsNone(Runtime.app_server_account_id)

    def test_accounts_returns_metadata_only(self):
        metadata = [{"account_id": "a1", "label": "L", "email": "e@example.com", "plan_enabled": True}]
        with patch.object(sidecar_module.codex_accounts, "list_accounts", return_value=metadata):
            response = self.client.get("/v1/auth/chatgpt/accounts", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"accounts": metadata})
        self.assertNotIn("token", response.text.lower())

    def test_accounts_reports_active_account_metadata_in_configure(self):
        self._enable_chatgpt()
        metadata = {"account_id": "a1", "label": "L", "email": "e@example.com", "plan_enabled": True}
        with patch.object(sidecar_module.sys, "platform", "darwin"), \
             patch.object(sidecar_module.codex_accounts, "active_account_id", return_value="a1"), \
             patch.object(sidecar_module.codex_accounts, "list_accounts", return_value=[metadata]):
            body = self.client.get("/v1/configure", headers=self.headers).json()
        self.assertEqual(body["chatgpt_account"], metadata)

    def test_shutdown_cancels_pending_attempts_without_changing_response(self):
        login_id = "login-shutdown"
        attempt = self._attempt(login_id)
        fake = _FakeLoopback()
        attempt["loopback"] = fake
        attempt["state"] = "STATE"
        attempt["verifier"] = "VERIFIER"
        Runtime.auth_attempts[login_id] = attempt
        with patch.object(sidecar_module.threading, "Thread"):
            response = self.client.post("/v1/shutdown", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})
        self.assertTrue(fake.closed)
        self.assertTrue(attempt["cancel"].is_set())
        self.assertEqual(attempt["verifier"], "")


class CodexExeResolutionTest(unittest.TestCase):
    """Bundled-layout resolution and precedence for ``_resolve_codex_exe``.

    Everything is built under a temporary directory; the tests never read the
    real ``~/.codex`` or any credential store.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        # Packaged PyInstaller onedir sidecar layout.
        self.vibe_agent = self.root / "Contents" / "Resources" / "vibe_agent"
        self.sidecar_dir = self.vibe_agent / "godotvibe-agent"
        self.sidecar_dir.mkdir(parents=True)
        self.sidecar_exe = self.sidecar_dir / "godotvibe-agent"
        self.sidecar_exe.write_bytes(b"stub")
        self.bundled = self.vibe_agent / "codex" / "bin" / "codex"
        self.bundled.parent.mkdir(parents=True)
        self.bundled.write_text("#!/bin/sh\n", encoding="utf-8")
        self.bundled.chmod(0o755)
        self.env = patch.dict(os.environ, {"VIBE_CODEX_BIN": "", "CODEX_BIN": ""})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    @contextlib.contextmanager
    def _isolated_lookup(self, *, which=None):
        with patch.object(sidecar_module.sys, "executable", str(self.sidecar_exe)), \
             patch.object(sidecar_module.sys, "_MEIPASS", "", create=True), \
             patch.object(sidecar_module.shutil, "which", return_value=which), \
             patch.object(sidecar_module.os.path, "expanduser", return_value=str(self.root)):
            yield

    def _make_executable(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def test_resolves_bundled_codex_from_sidecar_layout(self):
        with self._isolated_lookup():
            self.assertEqual(sidecar_module._resolve_codex_exe(), str(self.bundled))

    def test_resolves_bundled_codex_exe_on_windows(self):
        bundled_exe = self._make_executable(self.vibe_agent / "codex" / "bin" / "codex.exe")
        with patch.object(sidecar_module.sys, "platform", "win32"), self._isolated_lookup():
            self.assertEqual(sidecar_module._resolve_codex_exe(), str(bundled_exe))

    def test_env_override_takes_precedence_over_bundled(self):
        override = self._make_executable(self.root / "custom" / "codex")
        with patch.dict(os.environ, {"VIBE_CODEX_BIN": str(override)}), self._isolated_lookup():
            self.assertEqual(sidecar_module._resolve_codex_exe(), str(override))

    def test_bundled_codex_takes_precedence_over_path(self):
        with self._isolated_lookup(which="/path/from-path/codex"):
            self.assertEqual(sidecar_module._resolve_codex_exe(), str(self.bundled))

    def test_invalid_bundled_codex_falls_back_to_home(self):
        self.bundled.chmod(0o644)
        home = self._make_executable(self.root / ".local" / "bin" / "codex")
        with self._isolated_lookup(which=None):
            self.assertEqual(sidecar_module._resolve_codex_exe(), str(home))

    def test_path_is_used_when_bundle_is_absent(self):
        self.bundled.unlink()
        path_exe = self._make_executable(self.root / "path-bin" / "codex")
        with self._isolated_lookup(which=str(path_exe)):
            self.assertEqual(sidecar_module._resolve_codex_exe(), str(path_exe))


@unittest.skipUnless(os.name != "nt", "temporary Codex stubs use a POSIX shell")
class CodexCapabilityPlatformTest(unittest.TestCase):
    """Platform gating for ``_codex_capability`` using real temporary stubs.

    The bundled layout and a working ``codex --version`` script are built under
    a temporary directory and executed for real, so these tests exercise the
    resolver and the version probe instead of asserting a mock against itself.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        # Packaged PyInstaller onedir sidecar layout (same shape on macOS/Windows).
        self.vibe_agent = self.root / "Contents" / "Resources" / "vibe_agent"
        self.sidecar_dir = self.vibe_agent / "godotvibe-agent"
        self.sidecar_dir.mkdir(parents=True)
        self.sidecar_exe = self.sidecar_dir / "godotvibe-agent.exe"
        self.sidecar_exe.write_bytes(b"stub")
        self.env = patch.dict(os.environ, {"VIBE_CODEX_BIN": "", "CODEX_BIN": ""})
        self.env.start()
        _reset_auth_state()
        Runtime.codex_probed = False

    def tearDown(self):
        self.env.stop()
        _reset_auth_state()
        self.temp.cleanup()

    @contextlib.contextmanager
    def _isolated_lookup(self):
        with patch.object(sidecar_module.sys, "executable", str(self.sidecar_exe)), \
             patch.object(sidecar_module.sys, "_MEIPASS", "", create=True), \
             patch.object(sidecar_module.shutil, "which", return_value=None), \
             patch.object(sidecar_module.os.path, "expanduser", return_value=str(self.root)):
            yield

    def _install_versioned_codex(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\necho 'codex-cli 1.2.3'\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def test_windows_capability_resolves_bundled_codex_exe(self):
        exe = self._install_versioned_codex(self.vibe_agent / "codex" / "bin" / "codex.exe")
        with self._isolated_lookup(), patch.object(sidecar_module.sys, "platform", "win32"):
            supported, version = sidecar_module._codex_capability()
        self.assertTrue(supported)
        self.assertEqual(version, "1.2.3")
        self.assertEqual(Runtime.codex_exe, str(exe))

    def test_macos_capability_resolves_bundled_codex(self):
        exe = self._install_versioned_codex(self.vibe_agent / "codex" / "bin" / "codex")
        with self._isolated_lookup(), patch.object(sidecar_module.sys, "platform", "darwin"):
            supported, version = sidecar_module._codex_capability()
        self.assertTrue(supported)
        self.assertEqual(version, "1.2.3")
        self.assertEqual(Runtime.codex_exe, str(exe))

    def test_unsupported_platform_never_probes_or_enables(self):
        # A perfectly valid bundled executable must stay invisible on Linux.
        self._install_versioned_codex(self.vibe_agent / "codex" / "bin" / "codex")
        self._install_versioned_codex(self.vibe_agent / "codex" / "bin" / "codex.exe")
        with self._isolated_lookup(), patch.object(sidecar_module.sys, "platform", "linux"):
            supported, version = sidecar_module._codex_capability()
        self.assertFalse(supported)
        self.assertEqual(version, "")
        self.assertEqual(Runtime.codex_exe, "")


class _FakeCodexProvider:
    """Offline stand-in for CodexProvider; records the stage it was handed."""

    instances: list["_FakeCodexProvider"] = []

    def __init__(self, *, codex_exe, model, account_id=None):
        self.codex_exe = codex_exe
        self.model = model
        self.account_id = account_id
        self.last_account_id = "acct-fake"
        self.runs: list[dict] = []
        self.stopped = False
        self.live_at_run: str | None = None
        type(self).instances.append(self)

    def run_agent(self, prompt, workspace_stage, project_root, cancel_event, on_event):
        self.runs.append({
            "prompt": prompt, "stage": str(workspace_stage),
            "project_root": str(project_root), "cancel": cancel_event,
        })
        self.live_at_run = (Path(project_root) / "game.gd").read_text(encoding="utf-8")
        stage = Path(workspace_stage)
        (stage / "game.gd").write_text("agent changed\n", encoding="utf-8")
        on_event("agent_delta", {"text": "work "})
        on_event("agent_delta", {"text": "done"})
        on_event("command_output", {"text": "echo hi"})
        on_event("file_change", {"path": "res://game.gd"})
        on_event("turn_completed", {"status": "completed"})
        return "work done"

    def stop(self):
        self.stopped = True

    def invoke(self, messages, **kwargs):
        return "{}"


class ChatGptProviderWiringTest(unittest.TestCase):
    """ChatGPT provider wiring for the sidecar, using only fake components."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name) / "project"
        self.project.mkdir()
        (self.project / "project.godot").write_text("config_version=5\n", encoding="utf-8")
        (self.project / "game.gd").write_text("extends Node\n", encoding="utf-8")
        Runtime.project = self.project.resolve()
        Runtime.token = "sidecar-token"
        Runtime.parent_pid = 123
        Runtime.godot_exe = sys.executable
        Runtime.runs = {}
        Runtime.config = {"response_language": "zh-CN", "base_url": "", "model": "gpt-5-codex",
                          "vision_model": "gemini-2.5-flash"}
        _reset_auth_state()
        Runtime.auth_mode = "chatgpt"
        Runtime.codex_exe = "/opt/fake-codex"
        Runtime.codex_version = "1.2.3"
        _FakeCodexProvider.instances = []
        self.client = TestClient(app)
        self.headers = {"Authorization": "Bearer sidecar-token"}
        self.credentials = patch.object(sidecar_module, "credential", return_value="")
        self.credentials.start()
        self.backend = patch.object(sidecar_module, "credential_backend", return_value="macos")
        self.backend.start()
        self.platform = patch.object(sidecar_module.sys, "platform", "darwin")
        self.platform.start()

    def tearDown(self):
        self.platform.stop()
        self.backend.stop()
        self.credentials.stop()
        _reset_auth_state()
        self.temp.cleanup()

    def test_provider_factory_dispatches_on_auth_mode(self):
        with patch.object(sidecar_module, "CodexProvider", _FakeCodexProvider):
            provider = sidecar_module._provider()
        self.assertIsInstance(provider, sidecar_module.LanguageProvider)
        self.assertIsInstance(provider.provider, _FakeCodexProvider)

        Runtime.auth_mode = "api_key"
        with patch.object(sidecar_module, "credential", return_value="test-key"), \
             patch("vibe_tools.llm_transfer.SimpleLLMProvider._init_client"):
            provider = sidecar_module._provider()
        self.assertIsInstance(provider.provider, sidecar_module.SimpleLLMProvider)

    def test_chatgpt_run_stream_commits_stage_changes(self):
        original = (self.project / "game.gd").read_text(encoding="utf-8")
        with patch.object(sidecar_module, "CodexProvider", _FakeCodexProvider), \
             patch.object(sidecar_module, "_generate_plan"), \
             patch.object(sidecar_module, "_repair_cycle", return_value=None):
            with self.client.stream(
                "POST", "/v1/agent/run-stream", headers=self.headers,
                json={"prompt": "fix jump", "editor_sync": False},
            ) as response:
                body = "".join(response.iter_text())

        self.assertEqual(response.status_code, 200, body)
        self.assertIn("event: agent_delta", body)
        self.assertIn("event: command_output", body)
        self.assertIn("event: file_change", body)
        self.assertIn("event: step", body)
        self.assertIn("event: done", body)
        self.assertIn('"reply": "work done"', body)
        self.assertIn('"status": "done"', body)

        provider = _FakeCodexProvider.instances[-1]
        run_info = provider.runs[0]
        # The app-server turn ran against the disposable stage, and the live
        # project still held its original content while the turn was running.
        self.assertNotEqual(run_info["stage"], str(Runtime.project))
        self.assertIn("transactions", run_info["stage"])
        self.assertEqual(run_info["project_root"], str(Runtime.project))
        self.assertEqual(provider.live_at_run, original)
        self.assertEqual((self.project / "game.gd").read_text(encoding="utf-8"), "agent changed\n")
        self.assertEqual(Runtime.app_server_status, "stopped")

    def test_chatgpt_run_stream_maps_cancel_and_safe_errors(self):
        class CancellingProvider(_FakeCodexProvider):
            def run_agent(self, prompt, workspace_stage, project_root, cancel_event, on_event):
                cancel_event.set()
                raise sidecar_module.CodexProviderError("codex_turn_interrupted")

        with patch.object(sidecar_module, "CodexProvider", CancellingProvider), \
             patch.object(sidecar_module, "_generate_plan"), \
             patch.object(sidecar_module, "_repair_cycle", return_value=None):
            with self.client.stream(
                "POST", "/v1/agent/run-stream", headers=self.headers,
                json={"prompt": "fix jump", "editor_sync": False},
            ) as response:
                body = "".join(response.iter_text())
        self.assertIn("event: done", body)
        self.assertIn('"status": "cancelled"', body)
        # The stream must have joined the worker, which releases run_lock in
        # its finally block, before the next request in this same session runs.
        self.assertFalse(Runtime.run_lock.locked())

        class MissingAccountProvider(_FakeCodexProvider):
            def run_agent(self, prompt, workspace_stage, project_root, cancel_event, on_event):
                raise sidecar_module.CodexProviderError("chatgpt_account_missing")

        with patch.object(sidecar_module, "CodexProvider", MissingAccountProvider), \
             patch.object(sidecar_module, "_generate_plan"), \
             patch.object(sidecar_module, "_repair_cycle", return_value=None):
            with self.client.stream(
                "POST", "/v1/agent/run-stream", headers=self.headers,
                json={"prompt": "fix jump", "editor_sync": False},
            ) as response:
                body = "".join(response.iter_text())
        self.assertIn("event: error", body)
        self.assertIn("请先登录 ChatGPT 账号", body)
        self.assertNotIn("chatgpt_account_missing", body)
        self.assertFalse(Runtime.run_lock.locked())

    def test_stream_joins_worker_before_returning(self):
        """The stream generator must not return while its worker is running."""
        run_id, run = sidecar_module._new_run("agent", {})
        worker_threads: list[threading.Thread] = []
        worker_exited = threading.Event()

        def worker():
            worker_threads.append(threading.current_thread())
            sidecar_module._emit(run, "done", status="done")
            # Stay alive until the generator cancels; without a join the
            # generator can return while this thread still holds resources.
            run["cancel"].wait(timeout=5)
            worker_exited.set()

        stream = sidecar_module._stream(run_id, worker)
        try:
            self.assertIn(b"event: protocol_version", next(stream))
            self.assertIn(b"event: done", next(stream))
            # Consuming past the done event runs the generator's finally block,
            # which must join the worker before the stream is exhausted.
            with self.assertRaises(StopIteration):
                next(stream)
            self.assertTrue(worker_exited.is_set())
            self.assertFalse(worker_threads[0].is_alive())
        finally:
            stream.close()

    def test_shutdown_stops_codex_providers(self):
        provider = _FakeCodexProvider(codex_exe="/opt/fake-codex", model="gpt-5-codex")
        Runtime.codex_providers = [provider]
        Runtime.codex_provider = provider
        Runtime.app_server_status = "running"
        Runtime.app_server_account_id = "acct-fake"
        with patch.object(sidecar_module.threading, "Thread"):
            response = self.client.post("/v1/shutdown", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(provider.stopped)
        self.assertEqual(Runtime.codex_providers, [])
        self.assertIsNone(Runtime.codex_provider)
        self.assertEqual(Runtime.app_server_status, "stopped")
        self.assertIsNone(Runtime.app_server_account_id)


if __name__ == "__main__":
    unittest.main()
