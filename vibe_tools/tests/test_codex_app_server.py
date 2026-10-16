"""Tests for the Codex app-server stdio/JSONL client.

These tests drive an offline fake app-server (``tests/fixtures/fake_codex_app_server.py``)
so no real ``codex`` binary or ChatGPT token is required.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

from vibe_tools.codex_app_server import (
    APP_SERVER_CONFIG,
    CodexAppServer,
    CodexAppServerError,
    CodexModel,
    TurnEvent,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "fake_codex_app_server.py"
FAKE_TOKEN = "AT-FAKE-TOKEN-0123456789"
FAKE_MODEL = "gpt-5.1-codex"

#: Self-contained offline app-server used to exercise turn-id recording.  It is
#: generated into the per-test temporary directory so the shared fixture stays
#: untouched.  ``INLINE_FAKE_MODE`` selects how the single turn terminates.
INLINE_FAKE_SOURCE = r'''#!/usr/bin/env python3
"""Minimal offline app-server that exercises turn-id recording."""
import json
import os
import sys

TURN_ID = "turn-inline-1"
THREAD_ID = "thread-inline-1"
TRANSCRIPT = os.environ.get("INLINE_FAKE_TRANSCRIPT")


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def record(message):
    if not TRANSCRIPT:
        return
    with open(TRANSCRIPT, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": "recv", "msg": message}) + "\n")


def main():
    mode = os.environ.get("INLINE_FAKE_MODE", "interrupt")
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            message = json.loads(raw)
        except ValueError:
            continue
        record(message)
        method = message.get("method")
        request_id = message.get("id")
        if method == "initialize":
            send({"id": request_id, "result": {}})
        elif method == "initialized":
            continue
        elif method == "thread/start":
            send({"id": request_id, "result": {"thread": {"id": THREAD_ID}}})
        elif method == "turn/start":
            send({"id": request_id, "result": {"turn": {"id": TURN_ID}}})
            send({"method": "turn/started",
                  "params": {"threadId": THREAD_ID, "turn": {"id": TURN_ID}}})
            send({"method": "item/agentMessage/delta",
                  "params": {"threadId": THREAD_ID, "delta": "partial", "itemId": "msg-1"}})
            if mode == "complete":
                send({"method": "turn/completed",
                      "params": {"threadId": THREAD_ID,
                                 "turn": {"id": TURN_ID, "status": "completed"}}})
            elif mode == "failed":
                send({"method": "turn/completed",
                      "params": {"threadId": THREAD_ID,
                                 "turn": {"id": TURN_ID, "status": "failed",
                                          "error": {"message": "boom"}}}})
        elif method == "turn/interrupt":
            send({"id": request_id, "result": {}})
            send({"method": "turn/completed",
                  "params": {"threadId": THREAD_ID,
                             "turn": {"id": TURN_ID, "status": "interrupted"}}})


if __name__ == "__main__":
    main()
'''


def _launcher(fixture: Path, workdir: str) -> str:
    """Return an executable that runs the fixture on every platform."""
    if os.name == "nt":  # pragma: no cover - exercised on Windows only
        wrapper = Path(workdir) / "fake_codex.cmd"
        wrapper.write_text(
            '@echo off\r\n"{}" "{}" %*\r\n'.format(sys.executable, fixture),
            encoding="utf-8",
        )
        return str(wrapper)
    os.chmod(fixture, 0o755)
    return str(fixture)


class _ServerTestCase(unittest.TestCase):
    scenario = "happy"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.transcript = Path(self.temp.name) / "transcript.jsonl"
        self.logs: list[str] = []

    def tearDown(self):
        self.temp.cleanup()

    def _server(self, scenario: str | None = None, timeout: float = 5.0) -> CodexAppServer:
        env = dict(os.environ)
        env["FAKE_CODEX_SCENARIO"] = scenario or self.scenario
        env["FAKE_CODEX_TRANSCRIPT"] = str(self.transcript)
        return CodexAppServer(
            codex_exe=_launcher(FIXTURE, self.temp.name),
            cwd=self.temp.name,
            access_token=FAKE_TOKEN,
            model=FAKE_MODEL,
            env=env,
            log=self.logs.append,
            request_timeout=timeout,
        )

    def _records(self) -> list[dict]:
        if not self.transcript.exists():
            return []
        return [
            json.loads(line)
            for line in self.transcript.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def _received(self) -> list[dict]:
        return [record["msg"] for record in self._records() if record.get("event") == "recv"]

    def _methods(self) -> list[str]:
        return [message.get("method") for message in self._received()]

    def _wait_for_received(self, method: str, timeout: float = 5.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if method in self._methods():
                return
            time.sleep(0.01)
        self.fail(f"timed out waiting for {method!r} in fake transcript")

    def _assert_no_token(self) -> None:
        joined = "\n".join(self.logs)
        self.assertNotIn(FAKE_TOKEN, joined)
        if self.transcript.exists():
            self.assertNotIn(FAKE_TOKEN, self.transcript.read_text(encoding="utf-8"))


class InjectionTest(_ServerTestCase):
    def test_argv_is_fixed_config_only(self):
        server = self._server()
        server.start()
        try:
            argv = server._argv
            self.assertEqual(argv[1:4], ["app-server", "--listen", "stdio://"])
            rest = argv[4:]
            self.assertEqual(len(rest), len(APP_SERVER_CONFIG) * 2)
            self.assertEqual(rest[0::2], ["-c"] * len(APP_SERVER_CONFIG))
            configs = rest[1::2]
            self.assertEqual(tuple(configs), APP_SERVER_CONFIG)
            for expected in (
                'model_provider="openai_chatgpt_plan"',
                'model_providers.openai_chatgpt_plan.env_key="ACCESS_TOKEN"',
                'model_providers.openai_chatgpt_plan.requires_openai_auth=false',
                'model_providers.openai_chatgpt_plan.supports_websockets=false',
            ):
                self.assertIn(expected, configs)
            joined = " ".join(argv)
            self.assertNotIn(FAKE_TOKEN, joined)
            self.assertNotIn(FAKE_MODEL, joined)
            self.assertNotIn(self.temp.name, joined)
        finally:
            server.stop()

    def test_access_token_only_in_child_env(self):
        before = os.environ.get("ACCESS_TOKEN")
        server = self._server()
        server.start()
        try:
            self.assertEqual(os.environ.get("ACCESS_TOKEN"), before)
            self.assertNotEqual(os.environ.get("ACCESS_TOKEN"), FAKE_TOKEN)
            self.assertIn("ACCESS_TOKEN", server._child_env)
            self.assertEqual(server._child_env["ACCESS_TOKEN"], FAKE_TOKEN)
            self.assertNotIn(FAKE_TOKEN, " ".join(server._argv))
            self.assertNotIn(FAKE_TOKEN, "\n".join(self.logs))
        finally:
            server.stop()

        self._assert_no_token()
        start = self._records()[0]
        self.assertEqual(start["event"], "start")
        self.assertTrue(start["access_token_present"])
        self.assertEqual(start["argv"], server._argv[1:])

    def test_injected_env_is_the_base_copy(self):
        server = self._server()
        server.start()
        try:
            self.assertEqual(server._child_env["FAKE_CODEX_SCENARIO"], "happy")
            self.assertEqual(server._child_env["FAKE_CODEX_TRANSCRIPT"], str(self.transcript))
        finally:
            server.stop()


class InitializeTest(_ServerTestCase):
    def test_initialize_order_and_client_info(self):
        server = self._server()
        server.start()
        try:
            methods = self._methods()
            self.assertEqual(methods[0], "initialize")
            self._wait_for_received("initialized")
            methods = self._methods()
            self.assertEqual(methods[1], "initialized")
            server.start_thread()
            methods = self._methods()
            self.assertLess(methods.index("initialize"), methods.index("initialized"))
            self.assertLess(methods.index("initialized"), methods.index("thread/start"))
            initialize = self._received()[0]
            self.assertEqual(initialize["params"]["clientInfo"]["name"], "VibeGodot")
            self.assertEqual(initialize["params"]["clientInfo"]["title"], "VibeGodot")
        finally:
            server.stop()

    def test_initialize_timeout_raises(self):
        server = self._server("no_initialize_response", timeout=0.3)
        with self.assertRaises(CodexAppServerError):
            server.start()
        self.assertFalse(server.alive())
        self.assertNotIn(FAKE_TOKEN, "\n".join(self.logs))
        server.stop()

    def test_initialize_error_response_raises(self):
        server = self._server("initialize_error", timeout=2.0)
        with self.assertRaises(CodexAppServerError) as context:
            server.start()
        self.assertIn("initialize", str(context.exception))
        self.assertNotIn(FAKE_TOKEN, str(context.exception))
        server.stop()

    def test_initialize_malformed_response_raises(self):
        server = self._server("initialize_empty", timeout=2.0)
        with self.assertRaises(CodexAppServerError):
            server.start()
        server.stop()

    def test_token_never_in_exception(self):
        server = self._server("no_initialize_response", timeout=0.3)
        try:
            server.start()
        except CodexAppServerError as exc:
            self.assertNotIn(FAKE_TOKEN, str(exc))
        else:  # pragma: no cover - start must fail
            self.fail("expected CodexAppServerError")
        server.stop()


class ThreadTest(_ServerTestCase):
    def test_start_and_resume_thread(self):
        server = self._server()
        server.start()
        try:
            self.assertEqual(server.start_thread(), "thread-fake-1")
            self.assertEqual(server.resume_thread("thread-existing"), "thread-existing")
            methods = self._methods()
            self.assertIn("thread/start", methods)
            self.assertIn("thread/resume", methods)
        finally:
            server.stop()

    def test_flat_thread_id_shape(self):
        server = self._server("flat_thread_id")
        server.start()
        try:
            self.assertEqual(server.start_thread(), "thread-flat")
        finally:
            server.stop()


class TurnTest(_ServerTestCase):
    def test_event_mapping_and_delta_concatenation(self):
        server = self._server()
        server.start()
        try:
            thread_id = server.start_thread()
            events = list(server.run_turn(thread_id, "hello sensitive prompt"))
            self.assertEqual(
                [event.kind for event in events],
                ["agent_delta", "agent_delta", "command_output", "file_change", "turn_completed"],
            )
            self.assertEqual(
                "".join(event.text for event in events if event.kind == "agent_delta"),
                "Hello world",
            )
            self.assertEqual(events[0].item_id, "msg-1")
            self.assertEqual(events[2].text, "out\n")
            self.assertEqual(events[3].text, "res://main.gd")
            self.assertNotIn("SECRET-FILE-CONTENT", events[3].text)
            self.assertEqual(events[-1].status, "completed")
            self.assertNotIn("SECRET-FILE-CONTENT", "\n".join(self.logs))
            self.assertNotIn("hello sensitive prompt", "\n".join(self.logs))
        finally:
            server.stop()

    def test_bad_json_lines_are_skipped(self):
        server = self._server("bad_json")
        server.start()
        try:
            thread_id = server.start_thread()
            events = list(server.run_turn(thread_id, "go"))
            self.assertEqual(events[-1].kind, "turn_completed")
            self.assertEqual(events[-1].status, "completed")
            self.assertEqual(
                "".join(event.text for event in events if event.kind == "agent_delta"),
                "Hello world",
            )
        finally:
            server.stop()

    def test_failed_turn_maps_to_error(self):
        server = self._server("failed_turn")
        server.start()
        try:
            thread_id = server.start_thread()
            events = list(server.run_turn(thread_id, "go"))
            self.assertEqual(events[-1].kind, "error")
            self.assertEqual(events[-1].status, "failed")
            self.assertIn("model exploded", events[-1].message or "")
            self.assertNotIn(FAKE_TOKEN, events[-1].message or "")
        finally:
            server.stop()

    def test_error_notification_maps_to_error(self):
        server = self._server("error_notification")
        server.start()
        try:
            thread_id = server.start_thread()
            events = list(server.run_turn(thread_id, "go"))
            self.assertEqual(events[-1].kind, "error")
            self.assertIn("stream blew up", events[-1].message or "")
        finally:
            server.stop()

    def test_notifications_without_thread_id_do_not_hang(self):
        server = self._server("missing_thread_id")
        server.start()
        try:
            thread_id = server.start_thread()
            events = list(server.run_turn(thread_id, "go"))
            self.assertEqual([event.kind for event in events], ["agent_delta", "turn_completed"])
            self.assertEqual(events[0].text, "loose")
            self.assertEqual(events[-1].status, "completed")
        finally:
            server.stop()

    def test_concurrent_turn_for_same_thread_rejected(self):
        server = self._server("interrupt_wait")
        server.start()
        try:
            thread_id = server.start_thread()
            generator = server.run_turn(thread_id, "go")
            first = next(generator)
            self.assertEqual(first.kind, "agent_delta")
            with self.assertRaises(CodexAppServerError):
                second = server.run_turn(thread_id, "again")
                next(second)
            server.interrupt(thread_id, "turn-fake-1")
            list(generator)
        finally:
            server.stop()


class InterruptTest(_ServerTestCase):
    def test_interrupt_sends_turn_interrupt(self):
        server = self._server("interrupt_wait")
        server.start()
        try:
            thread_id = server.start_thread()
            generator = server.run_turn(thread_id, "go")
            first = next(generator)
            self.assertEqual(first.text, "partial")
            server.interrupt(thread_id, "turn-fake-1")
            events = list(generator)
            self.assertEqual(events[-1].kind, "turn_completed")
            self.assertEqual(events[-1].status, "interrupted")
            interrupts = [
                message
                for message in self._received()
                if message.get("method") == "turn/interrupt"
            ]
            self.assertEqual(len(interrupts), 1)
            self.assertEqual(interrupts[0]["params"]["threadId"], thread_id)
            self.assertEqual(interrupts[0]["params"]["turnId"], "turn-fake-1")
        finally:
            server.stop()


class ApprovalTest(_ServerTestCase):
    def test_server_request_is_declined(self):
        server = self._server("approval")
        server.start()
        try:
            thread_id = server.start_thread()
            events = list(server.run_turn(thread_id, "run the command"))
            self.assertEqual(events[-1].kind, "turn_completed")
            decisions = [
                record for record in self._records() if record.get("event") == "approval_decision"
            ]
            self.assertEqual(len(decisions), 1)
            self.assertEqual(decisions[0]["decision"], "decline")
            decline = [
                message
                for message in self._received()
                if message.get("id") == 9001 and "method" not in message
            ]
            self.assertEqual(len(decline), 1)
            self.assertEqual(decline[0]["result"], {"decision": "decline"})
            self._assert_no_token()
        finally:
            server.stop()


class LifecycleTest(_ServerTestCase):
    def test_stop_is_idempotent_and_alive_reflects_process(self):
        server = self._server()
        server.start()
        self.assertTrue(server.alive())
        server.stop()
        self.assertFalse(server.alive())
        server.stop()
        self.assertFalse(server.alive())

    def test_exit_during_turn_raises(self):
        server = self._server("exit_during_turn")
        server.start()
        try:
            thread_id = server.start_thread()
            with self.assertRaises(CodexAppServerError):
                list(server.run_turn(thread_id, "go"))
            deadline = time.time() + 5.0
            while server.alive() and time.time() < deadline:
                time.sleep(0.02)
            self.assertFalse(server.alive())
        finally:
            server.stop()
        server.stop()

    def test_exit_immediately_start_fails(self):
        server = self._server("exit_immediately", timeout=2.0)
        with self.assertRaises(CodexAppServerError):
            server.start()
        self.assertFalse(server.alive())
        server.stop()

    def test_request_after_stop_raises(self):
        server = self._server()
        server.start()
        server.stop()
        with self.assertRaises(CodexAppServerError):
            server.start_thread()


class TurnEventTest(unittest.TestCase):
    def test_defaults(self):
        event = TurnEvent("agent_delta")
        self.assertEqual(event.text, "")
        self.assertIsNone(event.item_id)
        self.assertIsNone(event.status)
        self.assertIsNone(event.message)


class TurnIdTest(_ServerTestCase):
    """Turn-id recording, read-only lookup and terminal cleanup."""

    def _inline_server(
        self, mode: str = "interrupt", timeout: float = 5.0
    ) -> CodexAppServer:
        fake = Path(self.temp.name) / "inline_fake_codex_app_server.py"
        fake.write_text(INLINE_FAKE_SOURCE, encoding="utf-8")
        env = dict(os.environ)
        env["INLINE_FAKE_MODE"] = mode
        env["INLINE_FAKE_TRANSCRIPT"] = str(self.transcript)
        return CodexAppServer(
            codex_exe=_launcher(fake, self.temp.name),
            cwd=self.temp.name,
            access_token=FAKE_TOKEN,
            model=FAKE_MODEL,
            env=env,
            log=self.logs.append,
            request_timeout=timeout,
        )

    def test_turn_id_from_turn_start_response_shapes(self):
        # Common response shapes for turn/start results.
        self.assertEqual(CodexAppServer._turn_id_from({"turn": {"id": "turn-1"}}), "turn-1")
        self.assertEqual(
            CodexAppServer._turn_id_from({"turn": {"turnId": "turn-2"}}), "turn-2"
        )
        self.assertEqual(CodexAppServer._turn_id_from({"turnId": "turn-3"}), "turn-3")
        self.assertEqual(CodexAppServer._turn_id_from({"turn_id": "turn-4"}), "turn-4")
        self.assertEqual(CodexAppServer._turn_id_from({"id": "turn-5"}), "turn-5")
        self.assertIsNone(CodexAppServer._turn_id_from({}))
        self.assertIsNone(CodexAppServer._turn_id_from({"turn": {}}))
        self.assertIsNone(CodexAppServer._turn_id_from("not-a-dict"))

    def test_current_turn_id_defaults_to_none(self):
        server = self._server()
        self.assertIsNone(server.current_turn_id("unknown-thread"))
        self.assertIsNone(server.current_turn_id(""))

    def test_interrupt_without_recorded_turn_id_raises(self):
        server = self._server()
        with self.assertRaises(CodexAppServerError):
            server.interrupt("unknown-thread")

    def test_turn_started_notification_records_supported_shapes(self):
        server = self._server()
        server._handle_message(
            {"method": "turn/started", "params": {"threadId": "t-flat", "turnId": "turn-flat"}}
        )
        self.assertEqual(server.current_turn_id("t-flat"), "turn-flat")
        server._handle_message(
            {
                "method": "turn/started",
                "params": {"thread_id": "t-snake", "turn_id": "turn-snake"},
            }
        )
        self.assertEqual(server.current_turn_id("t-snake"), "turn-snake")
        server._handle_message(
            {
                "method": "turn/started",
                "params": {"threadId": "t-nested", "turn": {"id": "turn-nested"}},
            }
        )
        self.assertEqual(server.current_turn_id("t-nested"), "turn-nested")
        # Unknown notifications must stay ignored and must not record anything.
        server._handle_message({"method": "turn/unknown", "params": {"threadId": "t-flat"}})
        self.assertEqual(server.current_turn_id("t-flat"), "turn-flat")

    def test_turn_start_response_records_and_interrupt_uses_current(self):
        server = self._inline_server("interrupt")
        server.start()
        try:
            thread_id = server.start_thread()
            self.assertIsNone(server.current_turn_id(thread_id))
            generator = server.run_turn(thread_id, "go")
            first = next(generator)
            self.assertEqual(first.kind, "agent_delta")
            # Recorded from the turn/start response (and turn/started).
            self.assertEqual(server.current_turn_id(thread_id), "turn-inline-1")
            server.interrupt(thread_id)  # turn_id omitted -> current record
            events = list(generator)
            self.assertEqual(events[-1].kind, "turn_completed")
            self.assertEqual(events[-1].status, "interrupted")
            interrupts = [
                message
                for message in self._received()
                if message.get("method") == "turn/interrupt"
            ]
            self.assertEqual(len(interrupts), 1)
            self.assertEqual(interrupts[0]["params"]["threadId"], thread_id)
            self.assertEqual(interrupts[0]["params"]["turnId"], "turn-inline-1")
            # Terminal state clears the record.
            self.assertIsNone(server.current_turn_id(thread_id))
            self._assert_no_token()
        finally:
            server.stop()

    def test_turn_completion_clears_current_turn_id(self):
        server = self._inline_server("complete")
        server.start()
        try:
            thread_id = server.start_thread()
            generator = server.run_turn(thread_id, "go")
            first = next(generator)
            self.assertEqual(first.kind, "agent_delta")
            self.assertEqual(server.current_turn_id(thread_id), "turn-inline-1")
            events = list(generator)
            self.assertEqual(events[-1].kind, "turn_completed")
            self.assertIsNone(server.current_turn_id(thread_id))
        finally:
            server.stop()

    def test_turn_failure_clears_current_turn_id(self):
        server = self._inline_server("failed")
        server.start()
        try:
            thread_id = server.start_thread()
            generator = server.run_turn(thread_id, "go")
            first = next(generator)
            self.assertEqual(first.kind, "agent_delta")
            self.assertEqual(server.current_turn_id(thread_id), "turn-inline-1")
            events = list(generator)
            self.assertEqual(events[-1].kind, "error")
            self.assertEqual(events[-1].status, "failed")
            self.assertIsNone(server.current_turn_id(thread_id))
        finally:
            server.stop()

    def test_exit_clears_current_turn_id(self):
        server = self._server()
        server._handle_message(
            {"method": "turn/started", "params": {"threadId": "t-exit", "turnId": "turn-exit"}}
        )
        self.assertEqual(server.current_turn_id("t-exit"), "turn-exit")
        server._mark_closed()
        self.assertIsNone(server.current_turn_id("t-exit"))


MODEL_LIST_FAKE_SOURCE = r'''#!/usr/bin/env python3
"""Inline offline app-server that answers model/list."""
import json
import os
import sys


def _send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def _record(message):
    path = os.environ.get("MODEL_LIST_TRANSCRIPT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": "recv", "msg": message}) + "\n")


def main():
    page = os.environ.get("MODEL_LIST_PAGE", "all")
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            message = json.loads(raw)
        except ValueError:
            continue
        _record(message)
        method = message.get("method")
        request_id = message.get("id")
        if method == "initialize":
            _send({"id": request_id, "result": {}})
        elif method == "initialized":
            pass
        elif method == "model/list":
            params = message.get("params") or {}
            include_hidden = bool(params.get("includeHidden"))
            data = [
                {"id": "gpt-5.1-codex", "model": "gpt-5.1-codex",
                 "displayName": "GPT-5.1 Codex", "hidden": False, "isDefault": True,
                 "inputModalities": ["text", "image"],
                 "supportedReasoningEfforts": [{"effort": "low"}, {"effort": "high"}]},
                {"id": "gpt-5-codex", "displayName": "GPT-5 Codex", "hidden": True,
                 "isDefault": False, "inputModalities": ["text"]},
                {"id": "gpt-legacy", "displayName": "Legacy", "hidden": False,
                 "isDefault": False, "inputModalities": ["text"]},
            ]
            if not include_hidden:
                data = [item for item in data if not item.get("hidden")]
            if page == "two" and not params.get("cursor"):
                _send({"id": request_id, "result": {"data": data[:1], "nextCursor": "page-2"}})
            elif page == "two" and params.get("cursor") == "page-2":
                _send({"id": request_id, "result": {"data": data[1:], "nextCursor": None}})
            else:
                _send({"id": request_id, "result": {"data": data, "nextCursor": None}})
        elif request_id is not None:
            _send({"id": request_id, "result": {}})


if __name__ == "__main__":
    main()
'''


class ModelListTest(_ServerTestCase):
    """``model/list`` parsing and request shape, offline."""

    def _inline_server(self, page: str = "all", timeout: float = 5.0) -> CodexAppServer:
        fake = Path(self.temp.name) / "model_list_fake.py"
        fake.write_text(MODEL_LIST_FAKE_SOURCE, encoding="utf-8")
        env = dict(os.environ)
        env["MODEL_LIST_TRANSCRIPT"] = str(self.transcript)
        env["MODEL_LIST_PAGE"] = page
        return CodexAppServer(
            codex_exe=_launcher(fake, self.temp.name),
            cwd=self.temp.name,
            access_token=FAKE_TOKEN,
            model=FAKE_MODEL,
            env=env,
            log=self.logs.append,
            request_timeout=timeout,
        )

    def test_models_page_parses_codex_shape(self):
        payload = {
            "data": [
                {
                    "id": "gpt-5.1-codex",
                    "model": "gpt-5.1-codex",
                    "displayName": "GPT-5.1 Codex",
                    "hidden": False,
                    "isDefault": True,
                    "inputModalities": ["text", "image"],
                    "supportedReasoningEfforts": [{"effort": "low"}, {"effort": "high"}],
                },
                {"model": "gpt-no-id", "displayName": "No Id", "inputModalities": ["text"]},
                {"id": "", "displayName": "Empty"},
                "not-a-dict",
            ],
            "nextCursor": "cursor-1",
        }
        models, cursor = CodexAppServer._models_page(payload, include_hidden=False)
        self.assertEqual(cursor, "cursor-1")
        self.assertEqual([model.id for model in models], ["gpt-5.1-codex", "gpt-no-id"])
        self.assertEqual(models[0].display_name, "GPT-5.1 Codex")
        self.assertTrue(models[0].is_default)
        self.assertTrue(models[0].supports_image)
        self.assertEqual(models[0].supported_reasoning_efforts, ("low", "high"))
        self.assertFalse(models[1].supports_image)

    def test_models_page_filters_hidden_and_unknown_shapes(self):
        payload = {"data": [{"id": "visible"}, {"id": "hidden", "hidden": True}]}
        models, cursor = CodexAppServer._models_page(payload, include_hidden=False)
        self.assertEqual([model.id for model in models], ["visible"])
        self.assertIsNone(cursor)
        models, _ = CodexAppServer._models_page(payload, include_hidden=True)
        self.assertEqual([model.id for model in models], ["visible", "hidden"])
        # A flat list is accepted; a non-list payload yields nothing.
        models, cursor = CodexAppServer._models_page([{"id": "flat"}])
        self.assertEqual([model.id for model in models], ["flat"])
        self.assertIsNone(cursor)
        self.assertEqual(CodexAppServer._models_page("nonsense"), ([], None))

    def test_input_modalities_are_required_for_image_support(self):
        models, _ = CodexAppServer._models_page(
            {"data": [
                {"id": "no-modalities"},
                {"id": "image", "inputModalities": ["text", "IMAGE"]},
                {"id": "missing-id-modalities", "inputModalities": ["image"]},
            ]}
        )
        self.assertFalse(models[0].supports_image)
        self.assertTrue(models[1].supports_image)
        self.assertTrue(models[2].supports_image)

    def test_list_models_sends_model_list_request(self):
        server = self._inline_server()
        server.start()
        try:
            models = server.list_models()
        finally:
            server.stop()
        self.assertEqual([model.id for model in models], ["gpt-5.1-codex", "gpt-legacy"])
        requests = [message for message in self._received() if message.get("method") == "model/list"]
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["params"]["includeHidden"], False)
        self._assert_no_token()

    def test_list_models_follows_pagination_cursor(self):
        server = self._inline_server(page="two")
        server.start()
        try:
            models = server.list_models()
        finally:
            server.stop()
        self.assertEqual([model.id for model in models], ["gpt-5.1-codex", "gpt-legacy"])
        requests = [message for message in self._received() if message.get("method") == "model/list"]
        self.assertEqual([request["params"].get("cursor") for request in requests], [None, "page-2"])

    def test_list_models_can_include_hidden(self):
        server = self._inline_server()
        server.start()
        try:
            models = server.list_models(include_hidden=True)
        finally:
            server.stop()
        self.assertEqual(
            [model.id for model in models], ["gpt-5.1-codex", "gpt-5-codex", "gpt-legacy"]
        )
        requests = [message for message in self._received() if message.get("method") == "model/list"]
        self.assertEqual(requests[0]["params"]["includeHidden"], True)


class ImageInputTest(_ServerTestCase):
    """Text + ``localImage`` UserInput transport for turn/start."""

    def test_user_input_builds_text_and_local_image_items(self):
        inputs = CodexAppServer._user_input("look", ["/tmp/a.png", "/tmp/b.png"])
        self.assertEqual(inputs[0], {"type": "text", "text": "look"})
        self.assertEqual(
            inputs[1:],
            [
                {"type": "localImage", "path": "/tmp/a.png"},
                {"type": "localImage", "path": "/tmp/b.png"},
            ],
        )
        self.assertEqual(CodexAppServer._user_input("plain", None), [{"type": "text", "text": "plain"}])
        self.assertEqual(CodexAppServer._user_input("plain", []), [{"type": "text", "text": "plain"}])

    def test_run_turn_transmits_local_image_paths(self):
        server = self._server()
        server.start()
        try:
            thread_id = server.start_thread()
            events = list(server.run_turn(thread_id, "please look", ["/abs/one.png"]))
            self.assertEqual(events[-1].kind, "turn_completed")
            starts = [message for message in self._received() if message.get("method") == "turn/start"]
            self.assertEqual(len(starts), 1)
            self.assertEqual(
                starts[0]["params"]["input"],
                [
                    {"type": "text", "text": "please look"},
                    {"type": "localImage", "path": "/abs/one.png"},
                ],
            )
            self._assert_no_token()
        finally:
            server.stop()

    def test_run_turn_without_images_keeps_legacy_text_input(self):
        server = self._server()
        server.start()
        try:
            thread_id = server.start_thread()
            list(server.run_turn(thread_id, "plain"))
            starts = [message for message in self._received() if message.get("method") == "turn/start"]
            self.assertEqual(starts[0]["params"]["input"], [{"type": "text", "text": "plain"}])
        finally:
            server.stop()


class CodexModelTest(unittest.TestCase):
    def test_defaults(self):
        model = CodexModel(id="gpt-5.1-codex", display_name="GPT-5.1 Codex")
        self.assertEqual(model.id, "gpt-5.1-codex")
        self.assertEqual(model.display_name, "GPT-5.1 Codex")
        self.assertFalse(model.supports_image)
        self.assertFalse(model.is_default)
        self.assertFalse(model.hidden)
        self.assertEqual(model.supported_reasoning_efforts, ())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
