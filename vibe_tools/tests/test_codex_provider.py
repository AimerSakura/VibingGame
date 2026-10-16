from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path

from vibe_tools import codex_auth
from vibe_tools.codex_app_server import CodexAppServerError, TurnEvent
from vibe_tools.codex_provider import (
    ERR_ACCOUNT_LOAD_FAILED,
    ERR_INTERRUPTED,
    ERR_NO_ACCOUNT,
    ERR_REFRESH_FAILED,
    ERR_START_FAILED,
    ERR_THREAD_FAILED,
    ERR_TOOLS_UNSUPPORTED,
    ERR_TURN_FAILED,
    CodexProvider,
    CodexProviderError,
)

# Obviously fake placeholders.  They are never a real credential and must never
# be persisted outside this test source file.
FAKE_TOKEN = "FAKE-ACCESS-TOKEN-for-unit-tests-only"
FAKE_REFRESH_TOKEN = "FAKE-REFRESH-TOKEN-for-unit-tests-only"
FAKE_ROTATED_TOKEN = "FAKE-ROTATED-TOKEN-for-unit-tests-only"
FAKE_ROTATED_REFRESH = "FAKE-ROTATED-REFRESH-for-unit-tests-only"

CODEX_EXE = "/opt/fake-codex"
MODEL = "gpt-5-codex"


def make_account(
    *,
    access_token: str = FAKE_TOKEN,
    refresh_token: str = FAKE_REFRESH_TOKEN,
    earliest_refresh_at: int | None = None,
    account_id: str = "acct-fake-0001",
) -> codex_auth.CodexAccount:
    return codex_auth.CodexAccount(
        account_id=account_id,
        label="Fake Account",
        email="fake@example.invalid",
        issuer="https://auth.openai.com",
        subject="fake-subject",
        client_id="fake-client",
        ext_agent_host_id="urn:uuid:fake-host",
        id_token="FAKE-ID-TOKEN",
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="Bearer",
        expires_in=3600,
        earliest_refresh_at=earliest_refresh_at,
        scopes=("openid", "profile"),
        saved_at="2020-01-01T00:00:00+00:00",
        plan_enabled=True,
    )


class FakeAppServer:
    """In-memory stand-in for :class:`CodexAppServer`; never spawns codex."""

    def __init__(self, codex_exe: str, cwd: str, access_token: str, model: str) -> None:
        self.codex_exe = codex_exe
        self.cwd = str(cwd)
        self.access_token = access_token
        self.model = model
        self.started = False
        self.stopped = False
        self.start_count = 0
        self.resume_calls: list[str] = []
        self.turns: list[tuple[str, str]] = []
        self.interrupt_calls: list[tuple[str, str | None]] = []
        self.new_thread_id = "thread-fake-0001"
        self.turn_id = "turn-fake-0001"
        self.start_raises = False
        self.thread_raises = False
        self.resume_raises = False
        self.events: list = []

    def start(self) -> None:
        if self.start_raises:
            raise CodexAppServerError("raw start failure " + FAKE_TOKEN)
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def start_thread(self) -> str:
        if self.thread_raises:
            raise CodexAppServerError("raw thread failure " + FAKE_TOKEN)
        self.start_count += 1
        return self.new_thread_id

    def resume_thread(self, thread_id: str) -> str:
        self.resume_calls.append(thread_id)
        if self.resume_raises:
            raise CodexAppServerError("raw resume failure " + FAKE_TOKEN)
        return thread_id

    def run_turn(self, thread_id: str, text: str):
        self.turns.append((thread_id, text))
        for event in self.events:
            if event == "WAIT_FOR_INTERRUPT":
                while not self.interrupt_calls:
                    if not self._sleep():
                        break
                yield TurnEvent("turn_completed", status="interrupted")
            else:
                yield event

    def _sleep(self) -> bool:
        threading.Event().wait(0.01)
        return not self.stopped

    def current_turn_id(self, thread_id: str):
        return self.turn_id

    def interrupt(self, thread_id: str, turn_id: str | None = None) -> None:
        self.interrupt_calls.append((thread_id, turn_id))


class FakeServerFactory:
    """Configurable factory producing :class:`FakeAppServer` instances."""

    def __init__(
        self,
        *,
        events=None,
        new_thread_id: str = "thread-fake-0001",
        turn_id: str = "turn-fake-0001",
        start_raises: bool = False,
        thread_raises: bool = False,
        resume_raises: bool = False,
    ) -> None:
        self.instances: list[FakeAppServer] = []
        self.events = events
        self.new_thread_id = new_thread_id
        self.turn_id = turn_id
        self.start_raises = start_raises
        self.thread_raises = thread_raises
        self.resume_raises = resume_raises

    def __call__(self, codex_exe: str, cwd: str, access_token: str, model: str) -> FakeAppServer:
        server = FakeAppServer(codex_exe, cwd, access_token, model)
        if self.events is not None:
            server.events = list(self.events)
        server.new_thread_id = self.new_thread_id
        server.turn_id = self.turn_id
        server.start_raises = self.start_raises
        server.thread_raises = self.thread_raises
        server.resume_raises = self.resume_raises
        self.instances.append(server)
        return server


COMPLETED_EVENTS = [TurnEvent("agent_delta", text="ok"), TurnEvent("turn_completed", status="completed")]


class CodexProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name) / "project"
        self.project.mkdir()
        (self.project / "project.godot").write_text("config_version=5\n", encoding="utf-8")
        (self.project / "game.gd").write_text("extends Node\n", encoding="utf-8")
        self.stage = Path(self.temp.name) / "stage"
        self.stage.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def build_provider(
        self,
        *,
        accounts=None,
        loader=None,
        saver=None,
        active=None,
        refresh=None,
        clock=None,
        logs=None,
        server_factory=None,
        codex_exe: str = CODEX_EXE,
        model: str = MODEL,
        account_id: str | None = None,
    ) -> CodexProvider:
        accounts = accounts if accounts is not None else {}

        def default_loader(value):
            return accounts.get(value)

        return CodexProvider(
            codex_exe=codex_exe,
            model=model,
            account_id=account_id,
            server_factory=server_factory or FakeServerFactory(events=COMPLETED_EVENTS),
            account_loader=loader or default_loader,
            account_saver=saver or (lambda account: None),
            active_account_id=active or (lambda: next(iter(accounts), None)),
            refresh=refresh or (lambda account: account),
            clock=clock or (lambda: 0.0),
            log_fn=(logs.append if logs is not None else (lambda message: None)),
        )

    def thread_file(self) -> Path:
        return self.project / ".godot" / "agent" / "codex" / "thread.json"

    # -- happy path ------------------------------------------------------- #

    def test_run_agent_uses_stage_cwd_and_maps_safe_events(self):
        account = make_account()
        factory = FakeServerFactory(
            events=[
                TurnEvent("agent_delta", text="Hel"),
                TurnEvent("agent_delta", text="lo"),
                TurnEvent("command_output", text="ls -la"),
                TurnEvent("file_change", text="res://game.gd"),
                TurnEvent("turn_completed", status="completed"),
            ]
        )
        provider = self.build_provider(accounts={account.account_id: account}, server_factory=factory)
        events: list[tuple[str, dict]] = []
        original = (self.project / "game.gd").read_text(encoding="utf-8")

        reply = provider.run_agent(
            "make it jump", str(self.stage), self.project, threading.Event(),
            lambda name, payload: events.append((name, payload)),
        )
        server = factory.instances[0]

        self.assertEqual(reply, "Hello")
        self.assertEqual(
            [name for name, _ in events],
            ["agent_delta", "agent_delta", "command_output", "file_change", "turn_completed"],
        )
        self.assertEqual(events[0][1]["text"], "Hel")
        self.assertEqual(events[3][1]["path"], "res://game.gd")
        self.assertEqual(events[4][1]["status"], "completed")
        self.assertEqual(server.cwd, str(self.stage))
        self.assertEqual(server.codex_exe, CODEX_EXE)
        self.assertEqual(server.model, MODEL)
        self.assertEqual(server.access_token, FAKE_TOKEN)
        self.assertTrue(server.started)
        self.assertTrue(server.stopped)
        # The live project is never the app-server working directory and no
        # source file changed just because a turn ran.
        self.assertNotEqual(server.cwd, str(self.project))
        self.assertEqual((self.project / "game.gd").read_text(encoding="utf-8"), original)

    def test_thread_file_is_atomic_private_and_contains_only_safe_thread_id(self):
        account = make_account()
        factory = FakeServerFactory(events=COMPLETED_EVENTS, new_thread_id="thread-safe-123")
        provider = self.build_provider(accounts={account.account_id: account}, server_factory=factory)
        provider.run_agent("hi", str(self.stage), self.project, threading.Event(), lambda n, p: None)

        path = self.thread_file()
        self.assertTrue(path.is_file())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        self.assertEqual(set(data), {"thread_id"})
        self.assertEqual(data["thread_id"], "thread-safe-123")
        self.assertNotIn(FAKE_TOKEN, raw)
        self.assertNotIn(FAKE_REFRESH_TOKEN, raw)
        # Atomic replace leaves no temporary files behind.
        self.assertEqual(list(path.parent.glob("*tmp*")), [])

    def test_thread_resume_recovery_and_tampered_file(self):
        account = make_account()
        provider = self.build_provider(accounts={account.account_id: account})

        # First run starts and records a thread.
        first = FakeServerFactory(events=COMPLETED_EVENTS, new_thread_id="thread-first")
        provider._server_factory = first
        provider.run_agent("one", str(self.stage), self.project, threading.Event(), lambda n, p: None)
        self.assertEqual(json.loads(self.thread_file().read_text(encoding="utf-8"))["thread_id"], "thread-first")

        # Second run resumes the stored thread instead of starting a new one.
        second = FakeServerFactory(events=COMPLETED_EVENTS, new_thread_id="thread-ignored")
        provider._server_factory = second
        provider.run_agent("two", str(self.stage), self.project, threading.Event(), lambda n, p: None)
        self.assertEqual(second.instances[0].resume_calls, ["thread-first"])
        self.assertEqual(second.instances[0].start_count, 0)

        # A failed resume transparently starts a new thread and rewrites the file.
        recovering = FakeServerFactory(
            events=COMPLETED_EVENTS, new_thread_id="thread-second", resume_raises=True
        )
        provider._server_factory = recovering
        provider.run_agent("three", str(self.stage), self.project, threading.Event(), lambda n, p: None)
        self.assertEqual(recovering.instances[0].resume_calls, ["thread-first"])
        self.assertEqual(recovering.instances[0].start_count, 1)
        self.assertEqual(json.loads(self.thread_file().read_text(encoding="utf-8"))["thread_id"], "thread-second")

        # A tampered/unsafe thread file is ignored and replaced.
        self.thread_file().write_text(json.dumps({"thread_id": "../../etc/passwd"}), encoding="utf-8")
        tampered = FakeServerFactory(events=COMPLETED_EVENTS, new_thread_id="thread-safe")
        provider._server_factory = tampered
        provider.run_agent("four", str(self.stage), self.project, threading.Event(), lambda n, p: None)
        self.assertEqual(tampered.instances[0].resume_calls, [])
        self.assertEqual(tampered.instances[0].start_count, 1)
        self.assertEqual(json.loads(self.thread_file().read_text(encoding="utf-8"))["thread_id"], "thread-safe")

    def test_refresh_saves_rotated_account_without_leaking_token(self):
        account = make_account(earliest_refresh_at=0)
        rotated = make_account(
            access_token=FAKE_ROTATED_TOKEN,
            refresh_token=FAKE_ROTATED_REFRESH,
            earliest_refresh_at=10_000,
        )
        saved: list = []
        refreshed: list = []
        logs: list[str] = []
        factory = FakeServerFactory(events=COMPLETED_EVENTS)
        provider = self.build_provider(
            accounts={account.account_id: account},
            saver=saved.append,
            refresh=lambda value: (refreshed.append(value) or rotated),
            clock=lambda: 100.0,
            logs=logs,
            server_factory=factory,
        )
        reply = provider.run_agent("go", str(self.stage), self.project, threading.Event(), lambda n, p: None)

        self.assertEqual(reply, "ok")
        self.assertEqual(refreshed, [account])
        self.assertEqual(saved, [rotated])
        self.assertEqual(factory.instances[0].access_token, FAKE_ROTATED_TOKEN)
        joined = "\n".join(logs)
        self.assertNotIn(FAKE_TOKEN, joined)
        self.assertNotIn(FAKE_ROTATED_TOKEN, joined)
        self.assertNotIn(FAKE_TOKEN, self.thread_file().read_text(encoding="utf-8"))

    def test_refresh_not_triggered_before_earliest_refresh_at(self):
        account = make_account(earliest_refresh_at=10_000)
        refreshed: list = []
        factory = FakeServerFactory(events=COMPLETED_EVENTS)
        provider = self.build_provider(
            accounts={account.account_id: account},
            refresh=lambda value: (refreshed.append(value) or value),
            clock=lambda: 100.0,
            server_factory=factory,
        )
        provider.run_agent("go", str(self.stage), self.project, threading.Event(), lambda n, p: None)
        self.assertEqual(refreshed, [])
        self.assertEqual(factory.instances[0].access_token, FAKE_TOKEN)

    def test_refresh_failure_is_safe(self):
        account = make_account(earliest_refresh_at=0)

        def boom(value):
            raise codex_auth.CodexAuthError("refresh_request_failed " + FAKE_TOKEN)

        provider = self.build_provider(accounts={account.account_id: account}, refresh=boom, clock=lambda: 100.0)
        with self.assertRaises(CodexProviderError) as ctx:
            provider.run_agent("go", str(self.stage), self.project, threading.Event(), lambda n, p: None)
        self.assertEqual(str(ctx.exception), ERR_REFRESH_FAILED)
        self.assertNotIn(FAKE_TOKEN, str(ctx.exception))

    def test_missing_or_unreadable_account_is_safe(self):
        provider = self.build_provider(accounts={})
        with self.assertRaises(CodexProviderError) as ctx:
            provider.run_agent("go", str(self.stage), self.project, threading.Event(), lambda n, p: None)
        self.assertEqual(str(ctx.exception), ERR_NO_ACCOUNT)

        def exploding_loader(value):
            raise RuntimeError("database boom " + FAKE_TOKEN)

        provider = self.build_provider(accounts={"acct-fake-0001": None}, loader=exploding_loader)
        with self.assertRaises(CodexProviderError) as ctx:
            provider.run_agent("go", str(self.stage), self.project, threading.Event(), lambda n, p: None)
        self.assertEqual(str(ctx.exception), ERR_ACCOUNT_LOAD_FAILED)
        self.assertNotIn(FAKE_TOKEN, str(ctx.exception))

    def test_start_failure_maps_to_safe_code(self):
        account = make_account()
        factory = FakeServerFactory(start_raises=True)
        provider = self.build_provider(accounts={account.account_id: account}, server_factory=factory)
        with self.assertRaises(CodexProviderError) as ctx:
            provider.run_agent("go", str(self.stage), self.project, threading.Event(), lambda n, p: None)
        self.assertEqual(str(ctx.exception), ERR_START_FAILED)
        self.assertNotIn(FAKE_TOKEN, str(ctx.exception))

    def test_thread_failure_maps_to_safe_code(self):
        account = make_account()
        factory = FakeServerFactory(thread_raises=True)
        provider = self.build_provider(accounts={account.account_id: account}, server_factory=factory)
        with self.assertRaises(CodexProviderError) as ctx:
            provider.run_agent("go", str(self.stage), self.project, threading.Event(), lambda n, p: None)
        self.assertEqual(str(ctx.exception), ERR_THREAD_FAILED)
        self.assertNotIn(FAKE_TOKEN, str(ctx.exception))

    def test_error_turn_and_missing_terminal_are_safe(self):
        account = make_account()
        factory = FakeServerFactory(events=[TurnEvent("error", status="failed", message="RAW " + FAKE_TOKEN)])
        provider = self.build_provider(accounts={account.account_id: account}, server_factory=factory)
        events: list[tuple[str, dict]] = []
        with self.assertRaises(CodexProviderError) as ctx:
            provider.run_agent(
                "go", str(self.stage), self.project, threading.Event(),
                lambda n, p: events.append((n, p)),
            )
        self.assertEqual(str(ctx.exception), ERR_TURN_FAILED)
        self.assertNotIn(FAKE_TOKEN, str(ctx.exception))
        self.assertEqual([name for name, _ in events], ["error"])
        self.assertEqual(events[0][1]["message"], ERR_TURN_FAILED)
        self.assertNotIn(FAKE_TOKEN, json.dumps(events))
        self.assertTrue(factory.instances[0].stopped)

        class NoTerminalFactory(FakeServerFactory):
            def __call__(self, codex_exe, cwd, access_token, model):
                instance = super().__call__(codex_exe, cwd, access_token, model)
                instance.events = [TurnEvent("agent_delta", text="partial")]
                return instance

        provider = self.build_provider(accounts={account.account_id: account}, server_factory=NoTerminalFactory())
        with self.assertRaises(CodexProviderError) as ctx:
            provider.run_agent("go", str(self.stage), self.project, threading.Event(), lambda n, p: None)
        self.assertEqual(str(ctx.exception), ERR_TURN_FAILED)

    def test_interrupted_terminal_without_cancel_is_safe(self):
        account = make_account()
        factory = FakeServerFactory(events=[TurnEvent("turn_completed", status="interrupted")])
        provider = self.build_provider(accounts={account.account_id: account}, server_factory=factory)
        with self.assertRaises(CodexProviderError) as ctx:
            provider.run_agent("go", str(self.stage), self.project, threading.Event(), lambda n, p: None)
        self.assertEqual(str(ctx.exception), ERR_TURN_FAILED)
        self.assertTrue(factory.instances[0].stopped)

    def test_cancel_interrupts_with_current_turn_id_and_stops(self):
        account = make_account()
        factory = FakeServerFactory(events=["WAIT_FOR_INTERRUPT"], new_thread_id="thread-cancel", turn_id="turn-42")
        provider = self.build_provider(accounts={account.account_id: account}, server_factory=factory)
        cancel_event = threading.Event()
        captured: dict = {}

        def run():
            try:
                provider.run_agent("go", str(self.stage), self.project, cancel_event, lambda n, p: None)
            except CodexProviderError as error:
                captured["error"] = error

        worker = threading.Thread(target=run)
        worker.start()
        server = factory.instances[0]
        for _ in range(500):
            if server.turns:
                break
            threading.Event().wait(0.01)
        cancel_event.set()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertIsInstance(captured.get("error"), CodexProviderError)
        self.assertEqual(str(captured["error"]), ERR_INTERRUPTED)
        self.assertEqual(server.interrupt_calls, [("thread-cancel", "turn-42")])
        self.assertTrue(server.stopped)

    # -- disposable invoke ------------------------------------------------- #

    def test_invoke_uses_disposable_isolated_cwd(self):
        account = make_account()
        factory = FakeServerFactory(events=[TurnEvent("agent_delta", text="plan"), TurnEvent("turn_completed", status="completed")])
        provider = self.build_provider(accounts={account.account_id: account}, server_factory=factory)
        reply = provider.invoke([{"role": "system", "content": "be terse"}, {"role": "user", "content": "hi"}])
        self.assertEqual(reply, "plan")
        server = factory.instances[0]
        self.assertNotEqual(server.cwd, str(self.project))
        self.assertNotEqual(server.cwd, str(self.stage))
        self.assertFalse(server.cwd.startswith(str(self.project) + os.sep))
        self.assertFalse(server.cwd.startswith(str(self.stage) + os.sep))
        self.assertIn("be terse", server.turns[0][1])
        self.assertIn("hi", server.turns[0][1])
        self.assertTrue(server.stopped)
        self.assertFalse(Path(server.cwd).exists())

    def test_invoke_with_tools_is_explicitly_unsupported(self):
        account = make_account()
        provider = self.build_provider(accounts={account.account_id: account})
        with self.assertRaises(CodexProviderError) as ctx:
            provider.invoke_with_tools([{"role": "user", "content": "hi"}], [{"type": "function"}])
        self.assertEqual(str(ctx.exception), ERR_TOOLS_UNSUPPORTED)

    def test_logs_never_contain_tokens(self):
        account = make_account(earliest_refresh_at=0)
        rotated = make_account(access_token=FAKE_ROTATED_TOKEN, earliest_refresh_at=10_000)
        logs: list[str] = []
        provider = self.build_provider(
            accounts={account.account_id: account},
            refresh=lambda value: rotated,
            clock=lambda: 100.0,
            logs=logs,
            server_factory=FakeServerFactory(events=COMPLETED_EVENTS),
        )
        provider.run_agent("go", str(self.stage), self.project, threading.Event(), lambda n, p: None)
        joined = "\n".join(logs)
        self.assertNotIn(FAKE_TOKEN, joined)
        self.assertNotIn(FAKE_REFRESH_TOKEN, joined)
        self.assertNotIn(FAKE_ROTATED_TOKEN, joined)
        self.assertIn(account.account_id, joined)

    def test_stop_is_idempotent_without_an_active_server(self):
        account = make_account()
        provider = self.build_provider(accounts={account.account_id: account})
        provider.stop()
        provider.stop()


if __name__ == "__main__":
    unittest.main()
