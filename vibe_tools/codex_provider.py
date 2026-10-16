"""ChatGPT/Codex app-server provider bridge for the editor sidecar.

This module owns the ChatGPT-plan lifecycle that the sidecar needs:

* resolve a stored :class:`~vibe_tools.codex_auth.CodexAccount` (refreshing it
  through ``codex_auth`` when its window has elapsed),
* run one agent turn through :class:`~vibe_tools.codex_app_server.CodexAppServer`
  with a disposable staging directory as ``cwd`` and a protected on-disk
  ``thread.json`` so a project can resume its Codex thread, and
* expose a text-only ``invoke`` for the test-plan/review flows.

Security invariants (mirroring ``codex_app_server``):

* The access token is handed to the app-server **only** through the child
  environment (the app-server injects ``ACCESS_TOKEN`` itself).  It is never
  placed in ``argv``, project files, events, logs or exception messages.
* Every failure becomes a finite error code; raw exception text (which may
  embed a token or a prompt) is discarded.
* ``thread.json`` lives under the project's ignored ``.godot`` tree, is written
  atomically with mode ``0600`` inside a ``0700`` directory, and contains only a
  validated, bounded ``thread_id``.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

from vibe_tools import codex_accounts, codex_auth
from vibe_tools.codex_app_server import CodexAppServer

log = logging.getLogger("godotvibe.codex_provider")

# Fixed, non-sensitive error codes.  They are safe to surface to the editor.
ERR_NO_ACCOUNT = "chatgpt_account_missing"
ERR_ACCOUNT_LOAD_FAILED = "chatgpt_account_unavailable"
ERR_REFRESH_FAILED = "chatgpt_refresh_failed"
ERR_START_FAILED = "codex_app_server_start_failed"
ERR_THREAD_FAILED = "codex_thread_failed"
ERR_TURN_FAILED = "codex_turn_failed"
ERR_INTERRUPTED = "codex_turn_interrupted"
ERR_TOOLS_UNSUPPORTED = "codex_tools_unsupported"

#: A thread id is only ever a bounded, single-line identifier.
_SAFE_THREAD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@+-]{0,199}$")
_THREAD_FILENAME = "thread.json"
_THREAD_DIR_PARTS = (".godot", "agent", "codex")
_MAX_THREAD_FILE_BYTES = 4096
_INVOKE_PREFIX = "vibe-codex-invoke-"
_CANCEL_POLL_S = 0.05


class CodexProviderError(RuntimeError):
    """A safe provider failure carrying only one of the fixed error codes."""


def _safe_thread_id(value: Any) -> str:
    if isinstance(value, str) and _SAFE_THREAD_ID_RE.fullmatch(value):
        return value
    return ""


def _messages_to_text(messages: Any) -> str:
    """Serialize chat messages to bounded plain text without tool schemas."""
    if not isinstance(messages, list):
        return ""
    parts: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user")[:64]
        content = message.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            chunks: list[str] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text":
                    chunks.append(str(item.get("text") or ""))
                elif item.get("type") == "image_url":
                    chunks.append("[image omitted]")
            text = "\n".join(chunks)
        elif content is None:
            text = ""
        else:
            text = str(content)
        parts.append("## %s\n%s" % (role, text))
    return "\n\n".join(parts)


class CodexProvider:
    """Bridge between the sidecar and the ChatGPT Codex app-server.

    All collaborators are injectable so the flow can be tested without a real
    ``codex`` binary, account store or keychain.
    """

    def __init__(
        self,
        *,
        codex_exe: str,
        model: str,
        account_id: str | None = None,
        server_factory: Callable[..., Any] | None = None,
        account_loader: Callable[[str], codex_auth.CodexAccount | None] | None = None,
        account_saver: Callable[[codex_auth.CodexAccount], None] | None = None,
        active_account_id: Callable[[], str | None] | None = None,
        refresh: Callable[[codex_auth.CodexAccount], codex_auth.CodexAccount] | None = None,
        clock: Callable[[], float] | None = None,
        log_fn: Callable[[str], Any] | None = None,
    ) -> None:
        if not isinstance(codex_exe, str) or not codex_exe:
            raise CodexProviderError(ERR_START_FAILED)
        self._codex_exe = codex_exe
        self._model = model or ""
        self._account_id = account_id or None
        self._server_factory = server_factory or CodexAppServer
        self._account_loader = account_loader or codex_accounts.load_account
        self._account_saver = account_saver or codex_accounts.save_account
        self._active_account_id = active_account_id or codex_accounts.active_account_id
        self._refresh = refresh or codex_auth.refresh
        self._clock = clock or time.time
        self._log_fn = log_fn or log.info
        self._server_lock = threading.Lock()
        self._server: Any = None
        self.last_account_id: str | None = None

    # ------------------------------------------------------------------
    # lifecycle / logging
    # ------------------------------------------------------------------
    def stop(self) -> None:
        """Stop any app-server currently owned by this provider."""
        with self._server_lock:
            server, self._server = self._server, None
        if server is None:
            return
        try:
            server.stop()
        except Exception:
            pass
        self._log("codex provider server stopped")

    def _log(self, message: str, *args: Any) -> None:
        try:
            self._log_fn(message % args if args else message)
        except Exception:
            pass

    def _make_server(self, cwd: str, access_token: str) -> Any:
        try:
            return self._server_factory(
                codex_exe=self._codex_exe,
                cwd=str(cwd),
                access_token=access_token,
                model=self._model,
            )
        except CodexProviderError:
            raise
        except Exception:
            raise CodexProviderError(ERR_START_FAILED) from None

    # ------------------------------------------------------------------
    # account resolution
    # ------------------------------------------------------------------
    def _resolve_account(self) -> codex_auth.CodexAccount:
        account_id = self._account_id or self._active_account_id()
        if not account_id:
            raise CodexProviderError(ERR_NO_ACCOUNT)
        try:
            account = self._account_loader(account_id)
        except Exception:
            raise CodexProviderError(ERR_ACCOUNT_LOAD_FAILED) from None
        if not isinstance(account, codex_auth.CodexAccount):
            raise CodexProviderError(ERR_NO_ACCOUNT)
        self.last_account_id = account.account_id
        due = account.earliest_refresh_at is not None and self._clock() >= account.earliest_refresh_at
        if not account.access_token or due:
            try:
                updated = self._refresh(account)
            except Exception:
                raise CodexProviderError(ERR_REFRESH_FAILED) from None
            if not isinstance(updated, codex_auth.CodexAccount) or not updated.access_token:
                raise CodexProviderError(ERR_REFRESH_FAILED)
            try:
                self._account_saver(updated)
            except Exception:
                raise CodexProviderError(ERR_REFRESH_FAILED) from None
            account = updated
            self.last_account_id = account.account_id
            self._log("codex account refreshed account_id=%s", account.account_id)
        return account

    # ------------------------------------------------------------------
    # protected thread.json
    # ------------------------------------------------------------------
    def _thread_file(self, project_root: Any) -> Path:
        return Path(project_root).joinpath(*_THREAD_DIR_PARTS, _THREAD_FILENAME)

    def _read_thread_id(self, project_root: Any) -> str:
        path = self._thread_file(project_root)
        try:
            if path.stat().st_size > _MAX_THREAD_FILE_BYTES:
                return ""
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        if not isinstance(data, dict):
            return ""
        return _safe_thread_id(data.get("thread_id"))

    def _write_thread_id(self, project_root: Any, thread_id: str) -> None:
        safe = _safe_thread_id(thread_id)
        if not safe:
            raise CodexProviderError(ERR_THREAD_FAILED)
        path = self._thread_file(project_root)
        directory = path.parent
        directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
        payload = json.dumps({"thread_id": safe}, separators=(",", ":"), sort_keys=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".thread-", suffix=".tmp", dir=str(directory))
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def _resume_or_start(self, server: Any, project_root: Any) -> str:
        stored = self._read_thread_id(project_root)
        if stored:
            try:
                resumed = server.resume_thread(stored)
            except Exception:
                resumed = ""
            resumed = _safe_thread_id(resumed)
            if resumed:
                if resumed != stored:
                    try:
                        self._write_thread_id(project_root, resumed)
                    except Exception:
                        pass
                self._log("codex thread resumed")
                return resumed
            self._log("codex thread resume failed; starting a new thread")
        try:
            thread_id = _safe_thread_id(server.start_thread())
        except Exception:
            raise CodexProviderError(ERR_THREAD_FAILED) from None
        if not thread_id:
            raise CodexProviderError(ERR_THREAD_FAILED)
        try:
            self._write_thread_id(project_root, thread_id)
        except CodexProviderError:
            raise
        except Exception:
            raise CodexProviderError(ERR_THREAD_FAILED) from None
        self._log("codex thread started")
        return thread_id

    # ------------------------------------------------------------------
    # cancellation
    # ------------------------------------------------------------------
    def _watch_cancel(self, server: Any, thread_id: str, cancel_event: threading.Event,
                      finished: threading.Event) -> None:
        while not finished.is_set():
            if not cancel_event.wait(_CANCEL_POLL_S):
                continue
            if finished.is_set():
                return
            try:
                turn_id = server.current_turn_id(thread_id)
                server.interrupt(thread_id, turn_id)
            except Exception:
                pass
            return

    # ------------------------------------------------------------------
    # public: one agent turn
    # ------------------------------------------------------------------
    def run_agent(
        self,
        prompt: str,
        workspace_stage: Any,
        project_root: Any,
        cancel_event: threading.Event | None = None,
        on_event: Callable[[str, dict], Any] | None = None,
    ) -> str:
        """Run one Codex turn in ``workspace_stage`` and return the text reply."""
        if not isinstance(prompt, str) or not prompt.strip():
            raise CodexProviderError(ERR_TURN_FAILED)
        account = self._resolve_account()
        emit = self._emitter(on_event)
        server: Any = None
        finished = threading.Event()
        watcher: threading.Thread | None = None
        reply: list[str] = []
        completed = False
        interrupted = False
        try:
            server = self._make_server(str(workspace_stage), account.access_token)
            with self._server_lock:
                self._server = server
            self._log("codex turn starting account_id=%s", account.account_id)
            try:
                server.start()
            except Exception:
                raise CodexProviderError(ERR_START_FAILED) from None
            thread_id = self._resume_or_start(server, project_root)
            if cancel_event is not None and cancel_event.is_set():
                raise CodexProviderError(ERR_INTERRUPTED)
            if cancel_event is not None:
                watcher = threading.Thread(
                    target=self._watch_cancel,
                    args=(server, thread_id, cancel_event, finished),
                    name="godotvibe-codex-cancel",
                    daemon=True,
                )
                watcher.start()
            try:
                for event in server.run_turn(thread_id, prompt):
                    if cancel_event is not None and cancel_event.is_set():
                        interrupted = True
                    kind = getattr(event, "kind", "")
                    if kind == "agent_delta":
                        text = getattr(event, "text", "") or ""
                        reply.append(text)
                        emit("agent_delta", {"text": text, "item_id": getattr(event, "item_id", None)})
                    elif kind == "command_output":
                        emit("command_output", {
                            "text": getattr(event, "text", "") or "",
                            "item_id": getattr(event, "item_id", None),
                        })
                    elif kind == "file_change":
                        emit("file_change", {
                            "path": getattr(event, "text", "") or "",
                            "item_id": getattr(event, "item_id", None),
                        })
                    elif kind == "turn_completed":
                        status = getattr(event, "status", None)
                        if status in (None, "completed"):
                            completed = True
                        emit("turn_completed", {"status": status or "completed"})
                    elif kind == "error":
                        # Never forward the raw message: it may embed prompts or tokens.
                        emit("error", {"message": ERR_TURN_FAILED, "status": getattr(event, "status", None)})
            except Exception:
                raise CodexProviderError(ERR_TURN_FAILED) from None
            if interrupted or (cancel_event is not None and cancel_event.is_set()):
                raise CodexProviderError(ERR_INTERRUPTED)
            if not completed:
                raise CodexProviderError(ERR_TURN_FAILED)
            self._log("codex turn completed account_id=%s", account.account_id)
            return "".join(reply)
        finally:
            finished.set()
            if watcher is not None and watcher is not threading.current_thread():
                watcher.join(timeout=2.0)
            with self._server_lock:
                if self._server is server:
                    self._server = None
            if server is not None:
                try:
                    server.stop()
                except Exception:
                    pass
            self._log("codex turn stopped account_id=%s", account.account_id)

    # ------------------------------------------------------------------
    # public: disposable text-only invoke
    # ------------------------------------------------------------------
    def invoke(self, messages: Any, temperature: float = 0.5, max_tokens: int | None = None,
               **kwargs: Any) -> str | None:
        """Run one disposable turn in an isolated temp cwd and return its text.

        The temporary directory is never the sidecar project or a workspace
        stage, so plan/review prompts cannot rewrite live or staged files.
        """
        text = _messages_to_text(messages)
        if not text.strip():
            return None
        account = self._resolve_account()
        temporary = tempfile.mkdtemp(prefix=_INVOKE_PREFIX)
        server: Any = None
        reply: list[str] = []
        completed = False
        try:
            server = self._make_server(temporary, account.access_token)
            with self._server_lock:
                self._server = server
            try:
                server.start()
            except Exception:
                raise CodexProviderError(ERR_START_FAILED) from None
            try:
                thread_id = server.start_thread()
            except Exception:
                raise CodexProviderError(ERR_THREAD_FAILED) from None
            thread_id = _safe_thread_id(thread_id)
            if not thread_id:
                raise CodexProviderError(ERR_THREAD_FAILED)
            try:
                for event in server.run_turn(thread_id, text):
                    kind = getattr(event, "kind", "")
                    if kind == "agent_delta":
                        reply.append(getattr(event, "text", "") or "")
                    elif kind == "turn_completed" and getattr(event, "status", None) in (None, "completed"):
                        completed = True
            except Exception:
                raise CodexProviderError(ERR_TURN_FAILED) from None
            if not completed:
                raise CodexProviderError(ERR_TURN_FAILED)
            return "".join(reply)
        finally:
            with self._server_lock:
                if self._server is server:
                    self._server = None
            if server is not None:
                try:
                    server.stop()
                except Exception:
                    pass
            shutil.rmtree(temporary, ignore_errors=True)

    def invoke_with_tools(self, messages: Any, tools: Any = None, **kwargs: Any) -> Any:
        """Explicitly unsupported: tool schemas are never forwarded to Codex."""
        raise CodexProviderError(ERR_TOOLS_UNSUPPORTED)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _emitter(on_event: Callable[[str, dict], Any] | None) -> Callable[[str, dict], None]:
        if on_event is None:
            return lambda name, payload: None

        def emit(name: str, payload: dict) -> None:
            try:
                on_event(name, payload)
            except Exception:
                pass

        return emit


__all__ = [
    "CodexProvider",
    "CodexProviderError",
    "ERR_NO_ACCOUNT",
    "ERR_ACCOUNT_LOAD_FAILED",
    "ERR_REFRESH_FAILED",
    "ERR_START_FAILED",
    "ERR_THREAD_FAILED",
    "ERR_TURN_FAILED",
    "ERR_INTERRUPTED",
    "ERR_TOOLS_UNSUPPORTED",
]
