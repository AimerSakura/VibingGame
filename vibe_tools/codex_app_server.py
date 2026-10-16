"""Codex ``app-server`` JSON-RPC-over-stdio (JSONL) client.

This module is deliberately only a protocol client: it never decides a working
directory.  Callers (for example the editor sidecar) must pass a disposable
staging directory as ``cwd`` and commit the staged files back to the real
project themselves.

Security invariants:

* The access token is passed to the child **only** through its environment
  (``ACCESS_TOKEN``); it is never placed in ``argv``, project files, logs or
  exception messages.
* The parent ``os.environ`` is never mutated.
* The seven launch ``-c`` configuration values are fixed constants and must not
  be extended with secrets, the model or the cwd.

The subprocess is expected to accept one JSON object per line on stdin and
produce one JSON object per line on stdout, with an optional ``jsonrpc`` header
omitted (the Codex app-server convention).
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from dataclasses import dataclass
from itertools import count
from typing import Any, Callable, Iterator

try:  # pragma: no cover - the package is always importable in practice
    from vibe_tools import __version__ as _APP_VERSION
except Exception:  # pragma: no cover - defensive only
    _APP_VERSION = "0.0.0"

AGENT_NAME = "VibeGodot"

#: Fixed Codex app-server launch configuration.  Do not add token/model/cwd.
APP_SERVER_CONFIG: tuple[str, ...] = (
    'model_provider="openai_chatgpt_plan"',
    'model_providers.openai_chatgpt_plan.name="ChatGPT plan"',
    'model_providers.openai_chatgpt_plan.base_url="https://api.openai.com/v1"',
    'model_providers.openai_chatgpt_plan.env_key="ACCESS_TOKEN"',
    'model_providers.openai_chatgpt_plan.wire_api="responses"',
    'model_providers.openai_chatgpt_plan.requires_openai_auth=false',
    'model_providers.openai_chatgpt_plan.supports_websockets=false',
)

DEFAULT_REQUEST_TIMEOUT = 30.0
DEFAULT_TERMINATE_TIMEOUT = 3.0

#: Hard cap on ``model/list`` pagination so a misbehaving server cannot loop.
_MAX_MODEL_PAGES = 10

_TERMINAL_NOTIFICATIONS = frozenset({"turn/completed", "error", "turn/failed"})
_CLOSED = object()


class CodexAppServerError(RuntimeError):
    """Raised for every app-server transport or protocol failure."""


@dataclass
class TurnEvent:
    """One mapped app-server notification consumed from ``run_turn``."""

    kind: str
    text: str = ""
    item_id: str | None = None
    status: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class CodexModel:
    """One entry from the app-server ``model/list`` catalog.

    Only non-sensitive metadata is retained: the model id used for
    ``thread/start``, a display name, the advertised input modalities and the
    reasoning efforts.  Nothing here is a credential.
    """

    id: str
    display_name: str
    supports_image: bool = False
    is_default: bool = False
    hidden: bool = False
    supported_reasoning_efforts: tuple[str, ...] = ()


class _Pending:
    __slots__ = ("event", "response", "error")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.response: dict | None = None
        self.error: str | None = None


class CodexAppServer:
    """JSON-RPC/JSONL client for ``codex app-server --listen stdio://``."""

    def __init__(
        self,
        *,
        codex_exe: str,
        cwd: str,
        access_token: str,
        model: str,
        env: dict | None = None,
        log: Callable[[str], Any] = print,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
        terminate_timeout: float = DEFAULT_TERMINATE_TIMEOUT,
    ) -> None:
        if not codex_exe:
            raise ValueError("codex_exe is required")
        if not access_token:
            raise ValueError("access_token is required")
        self._codex_exe = codex_exe
        self._cwd = str(cwd)
        self._access_token = access_token
        self._model = model
        # Safe copy: mutating the child env never touches os.environ.
        self._base_env: dict[str, str] = dict(env) if env is not None else dict(os.environ)
        self._log_fn = log
        self._request_timeout = float(request_timeout)
        self._terminate_timeout = float(terminate_timeout)

        self._argv: list[str] = []
        self._child_env: dict[str, str] = {}
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None

        self._stdin_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[Any, _Pending] = {}
        self._turn_lock = threading.Lock()
        self._turn_queues: dict[str, "queue.Queue"] = {}
        self._turn_id_lock = threading.Lock()
        self._turn_ids: dict[str, str] = {}
        self._id_source = count(1)

        self._closed = threading.Event()
        self._stopped = threading.Event()
        self._on_closed_lock = threading.Lock()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def _build_argv(self) -> list[str]:
        argv = [self._codex_exe, "app-server", "--listen", "stdio://"]
        for config in APP_SERVER_CONFIG:
            argv.extend(["-c", config])
        return argv

    def start(self) -> None:
        if self._proc is not None:
            raise CodexAppServerError("app-server already started")
        self._stopped.clear()
        self._closed.clear()
        self._argv = self._build_argv()
        child_env = dict(self._base_env)
        child_env["ACCESS_TOKEN"] = self._access_token  # sole injection point
        self._child_env = child_env
        self._log("starting codex app-server")
        try:
            self._proc = subprocess.Popen(
                self._argv,
                cwd=self._cwd,
                env=child_env,
                text=True,
                bufsize=1,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:  # pragma: no cover - exercised by missing exe
            self._proc = None
            raise CodexAppServerError(f"failed to start codex app-server: {exc}") from exc

        self._reader = threading.Thread(
            target=self._read_stdout, name="codex-app-server-stdout", daemon=True
        )
        self._reader.start()
        self._stderr_reader = threading.Thread(
            target=self._drain_stderr, name="codex-app-server-stderr", daemon=True
        )
        self._stderr_reader.start()

        try:
            self._request(
                "initialize",
                {
                    "clientInfo": {
                        "name": AGENT_NAME,
                        "title": AGENT_NAME,
                        "version": _APP_VERSION,
                    }
                },
            )
        except Exception:
            # A missing/failed initialize response must never be silently ignored.
            self.stop()
            raise
        self._send({"method": "initialized"})
        self._log("codex app-server initialized")

    def stop(self) -> None:
        self._stopped.set()
        proc = self._proc
        self._proc = None
        if proc is not None:
            try:
                if proc.stdin is not None and not proc.stdin.closed:
                    proc.stdin.close()
            except Exception:
                pass
            if proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass
            try:
                proc.wait(timeout=self._terminate_timeout)
            except Exception:
                if proc.poll() is None:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    try:
                        proc.wait(timeout=2.0)
                    except Exception:
                        pass
        for thread in (self._reader, self._stderr_reader):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=2.0)
        self._reader = None
        self._stderr_reader = None
        if proc is not None:
            for stream in (proc.stdout, proc.stderr):
                if stream is not None and not stream.closed:
                    try:
                        stream.close()
                    except Exception:
                        pass
        self._mark_closed()
        self._log("codex app-server stopped")

    def alive(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    # ------------------------------------------------------------------
    # requests / notifications
    # ------------------------------------------------------------------
    def _ensure_alive(self) -> None:
        proc = self._proc
        if (
            self._stopped.is_set()
            or self._closed.is_set()
            or proc is None
            or proc.poll() is not None
        ):
            raise CodexAppServerError("codex app-server is not running")

    def _send(self, message: dict) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise CodexAppServerError("codex app-server is not running")
        data = json.dumps(message, separators=(",", ":"))
        with self._stdin_lock:
            if self._stopped.is_set():
                raise CodexAppServerError("codex app-server is stopping")
            try:
                proc.stdin.write(data + "\n")
                proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                raise CodexAppServerError("failed to write to codex app-server stdin") from exc

    def _request(self, method: str, params: dict | None = None) -> Any:
        self._ensure_alive()
        pending = _Pending()
        request_id = next(self._id_source)
        with self._pending_lock:
            self._pending[request_id] = pending
        message: dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        try:
            self._send(message)
        except Exception:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise

        if not pending.event.wait(self._request_timeout):
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise CodexAppServerError(f"timed out waiting for '{method}' response")
        with self._pending_lock:
            self._pending.pop(request_id, None)

        if pending.error:
            raise CodexAppServerError(self._redact(pending.error))
        response = pending.response or {}
        if "error" in response:
            raise CodexAppServerError(
                self._redact(self._error_message(response["error"], method))
            )
        if "result" not in response:
            raise CodexAppServerError(
                f"malformed '{method}' response from codex app-server"
            )
        return response["result"]

    # ------------------------------------------------------------------
    # public protocol surface
    # ------------------------------------------------------------------
    def start_thread(self) -> str:
        result = self._request("thread/start", self._thread_params())
        thread_id = self._thread_id_from(result)
        if not thread_id:
            raise CodexAppServerError("codex app-server did not return a thread id")
        return thread_id

    def resume_thread(self, thread_id: str) -> str:
        params = self._thread_params()
        params["threadId"] = thread_id
        result = self._request("thread/resume", params)
        return self._thread_id_from(result) or thread_id

    def run_turn(self, thread_id: str, text: str, images: list[str] | None = None) -> Iterator[TurnEvent]:
        self._ensure_alive()
        turn_queue: "queue.Queue" = queue.Queue()
        with self._turn_lock:
            if thread_id in self._turn_queues:
                raise CodexAppServerError(
                    f"a turn is already running for thread {thread_id}"
                )
            self._turn_queues[thread_id] = turn_queue
        try:
            result = self._request(
                "turn/start",
                {"threadId": thread_id, "input": self._user_input(text, images)},
            )
            # Prefer the synchronous turn/start response; a turn/started
            # notification handled by the reader thread may already have
            # recorded the same id.
            turn_id = self._turn_id_from(result)
            if turn_id:
                self._record_turn_id(thread_id, turn_id)
            while True:
                item = turn_queue.get()
                if item is _CLOSED:
                    raise CodexAppServerError("codex app-server exited during turn")
                method, params = item
                event = self._map_notification(method, params)
                if event is not None:
                    yield event
                if method in _TERMINAL_NOTIFICATIONS:
                    return
        finally:
            with self._turn_lock:
                if self._turn_queues.get(thread_id) is turn_queue:
                    del self._turn_queues[thread_id]
            # Any terminal outcome (completed/failed/interrupted) ends the turn.
            self._clear_turn_id(thread_id)

    def current_turn_id(self, thread_id: str) -> str | None:
        """Return the last turn id observed for ``thread_id``, if any.

        This is read-only and non-sensitive: it never exposes prompts or
        credentials, and it returns ``None`` once the turn has reached a
        terminal state or the process exited.
        """
        if not isinstance(thread_id, str) or not thread_id:
            return None
        with self._turn_id_lock:
            return self._turn_ids.get(thread_id)

    def interrupt(self, thread_id: str, turn_id: str | None = None) -> None:
        """Interrupt the turn; ``turn_id=None`` uses the recorded current turn."""
        if turn_id is None:
            turn_id = self.current_turn_id(thread_id)
        if not turn_id:
            raise CodexAppServerError(f"no active turn recorded for thread {thread_id}")
        self._request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})

    def list_models(self, *, include_hidden: bool = False) -> list[CodexModel]:
        """Return the ``model/list`` catalog.

        This is a read-only request: it never starts a thread, never starts a
        turn and never persists a ``thread.json``.  Pagination is followed for a
        bounded number of pages.
        """
        self._ensure_alive()
        models: list[CodexModel] = []
        cursor: str | None = None
        for _ in range(_MAX_MODEL_PAGES):
            params: dict[str, Any] = {"includeHidden": bool(include_hidden)}
            if cursor:
                params["cursor"] = cursor
            result = self._request("model/list", params)
            page, cursor = self._models_page(result, include_hidden=include_hidden)
            models.extend(page)
            if not cursor:
                break
        return models

    # ------------------------------------------------------------------
    # UserInput helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _user_input(text: str, images: list[str] | None = None) -> list[dict[str, Any]]:
        """Build the ``turn/start`` UserInput list (text + local images).

        Image paths are only serialized here; boundary validation is the
        caller's responsibility (``CodexProvider`` confines them to the staging
        directory before they reach this method).
        """
        inputs: list[dict[str, Any]] = [{"type": "text", "text": text}]
        for path in images or ():
            if isinstance(path, str) and path:
                inputs.append({"type": "localImage", "path": path})
        return inputs

    @classmethod
    def _models_page(
        cls, payload: Any, *, include_hidden: bool = False
    ) -> tuple[list[CodexModel], str | None]:
        """Parse one ``model/list`` result into models plus a next cursor."""
        items: Any
        cursor: Any
        if isinstance(payload, list):
            items, cursor = payload, None
        elif isinstance(payload, dict):
            items = payload.get("data")
            cursor = payload.get("nextCursor")
        else:
            return [], None
        if not isinstance(items, list):
            items = []
        if not isinstance(cursor, str) or not cursor:
            cursor = None
        models: list[CodexModel] = []
        for item in items:
            model = cls._model_from(item)
            if model is None:
                continue
            if model.hidden and not include_hidden:
                continue
            models.append(model)
        return models, cursor

    @classmethod
    def _model_from(cls, item: Any) -> CodexModel | None:
        if not isinstance(item, dict):
            return None
        model_id = item.get("id") or item.get("model")
        if not isinstance(model_id, str) or not model_id.strip():
            return None
        model_id = model_id.strip()[:200]
        display = item.get("displayName") or item.get("display_name")
        if not isinstance(display, str) or not display.strip():
            display = model_id
        modalities = cls._input_modalities(item)
        return CodexModel(
            id=model_id,
            display_name=display.strip()[:200],
            supports_image="image" in modalities,
            is_default=bool(item.get("isDefault") or item.get("is_default")),
            hidden=bool(item.get("hidden")),
            supported_reasoning_efforts=cls._reasoning_efforts(item),
        )

    @staticmethod
    def _input_modalities(item: dict) -> tuple[str, ...]:
        raw = item.get("inputModalities")
        if raw is None:
            raw = item.get("input_modalities")
        if not isinstance(raw, (list, tuple)):
            return ()
        return tuple(
            value.strip().lower()
            for value in raw
            if isinstance(value, str) and value.strip()
        )

    @staticmethod
    def _reasoning_efforts(item: dict) -> tuple[str, ...]:
        raw = item.get("supportedReasoningEfforts")
        if raw is None:
            raw = item.get("supported_reasoning_efforts")
        if not isinstance(raw, (list, tuple)):
            return ()
        efforts: list[str] = []
        for entry in raw:
            if isinstance(entry, str):
                value = entry
            elif isinstance(entry, dict):
                value = entry.get("effort")
            else:
                value = None
            if isinstance(value, str) and value.strip():
                efforts.append(value.strip()[:64])
        return tuple(efforts)

    # ------------------------------------------------------------------
    # reader threads
    # ------------------------------------------------------------------
    def _read_stdout(self) -> None:
        proc = self._proc
        try:
            if proc is None or proc.stdout is None:
                return
            for raw in proc.stdout:
                line = raw.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    # A malformed line must not break the stream.
                    self._log("skipping malformed JSON line from codex app-server")
                    continue
                if not isinstance(message, dict):
                    continue
                try:
                    self._handle_message(message)
                except Exception:
                    self._log("unexpected error while handling codex app-server message")
        except Exception:
            pass
        finally:
            self._mark_closed()

    def _drain_stderr(self) -> None:
        proc = self._proc
        try:
            if proc is None or proc.stderr is None:
                return
            for _ in proc.stderr:
                # Never log child stderr: it may echo prompts or credentials.
                pass
        except Exception:
            pass

    def _handle_message(self, message: dict) -> None:
        method = message.get("method")
        if method is not None:
            if "id" in message:
                self._handle_server_request(message)
            else:
                params = message.get("params")
                self._dispatch_notification(method, params if isinstance(params, dict) else {})
            return
        if "id" in message:
            self._resolve(message["id"], message)

    def _handle_server_request(self, message: dict) -> None:
        request_id = message.get("id")
        method = message.get("method")
        self._log(f"declining codex app-server request '{method}'")
        try:
            self._send({"id": request_id, "result": {"decision": "decline"}})
        except CodexAppServerError:
            pass

    def _resolve(self, request_id: Any, message: dict) -> None:
        with self._pending_lock:
            pending = self._pending.get(request_id)
            if pending is None and isinstance(request_id, str) and request_id.isdigit():
                pending = self._pending.get(int(request_id))
        if pending is None:
            return
        pending.response = message
        pending.event.set()

    def _dispatch_notification(self, method: str, params: dict) -> None:
        thread_id = self._notification_thread_id(params)
        if method == "turn/started":
            self._record_started_turn(thread_id, params)
        with self._turn_lock:
            target = self._turn_queues.get(thread_id) if thread_id is not None else None
            if target is None and len(self._turn_queues) == 1:
                # Some notifications omit the thread id; never let an unknown
                # shape leave the sole active turn waiting forever.
                target = next(iter(self._turn_queues.values()))
        if target is not None:
            target.put((method, params))

    def _record_started_turn(self, thread_id: str | None, params: dict) -> None:
        turn_id = self._turn_id_from(params)
        if not turn_id:
            return
        if thread_id is None:
            with self._turn_lock:
                if len(self._turn_queues) == 1:
                    thread_id = next(iter(self._turn_queues))
        if thread_id is not None:
            self._record_turn_id(thread_id, turn_id)

    def _record_turn_id(self, thread_id: str, turn_id: str) -> None:
        if not isinstance(thread_id, str) or not thread_id:
            return
        if not isinstance(turn_id, str) or not turn_id:
            return
        with self._turn_id_lock:
            self._turn_ids[thread_id] = turn_id

    def _clear_turn_id(self, thread_id: str) -> None:
        if not isinstance(thread_id, str) or not thread_id:
            return
        with self._turn_id_lock:
            self._turn_ids.pop(thread_id, None)

    def _mark_closed(self) -> None:
        with self._on_closed_lock:
            if self._closed.is_set():
                return
            self._closed.set()
        with self._pending_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for item in pending:
            item.error = "codex app-server process exited"
            item.event.set()
        with self._turn_lock:
            turn_queues = list(self._turn_queues.values())
        for turn_queue in turn_queues:
            turn_queue.put(_CLOSED)
        with self._turn_id_lock:
            self._turn_ids.clear()

    # ------------------------------------------------------------------
    # notification mapping helpers
    # ------------------------------------------------------------------
    def _thread_params(self) -> dict:
        return {
            "cwd": self._cwd,
            "model": self._model,
            "approvalPolicy": "never",
            "sandboxPolicy": {
                "type": "workspaceWrite",
                "writableRoots": [self._cwd],
                "networkAccess": False,
            },
        }

    @staticmethod
    def _thread_id_from(payload: Any) -> str | None:
        if not isinstance(payload, dict):
            return None
        thread = payload.get("thread")
        if isinstance(thread, dict):
            for key in ("id", "threadId", "thread_id"):
                value = thread.get(key)
                if isinstance(value, str) and value:
                    return value
        for key in ("threadId", "thread_id", "id"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    @staticmethod
    def _turn_id_from(payload: Any) -> str | None:
        """Extract a turn id from a turn/start result or turn/started params.

        Tries the nested ``turn`` object first, then the flat ``turnId`` /
        ``turn_id`` / ``id`` keys.  Returns ``None`` for unknown shapes so
        callers can safely ignore them.
        """
        if not isinstance(payload, dict):
            return None
        turn = payload.get("turn")
        if isinstance(turn, dict):
            for key in ("id", "turnId", "turn_id"):
                value = turn.get(key)
                if isinstance(value, str) and value:
                    return value
        for key in ("turnId", "turn_id", "id"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    @staticmethod
    def _notification_thread_id(params: dict) -> str | None:
        for key in ("threadId", "thread_id"):
            value = params.get(key)
            if isinstance(value, str) and value:
                return value
        turn = params.get("turn")
        if isinstance(turn, dict):
            for key in ("threadId", "thread_id"):
                value = turn.get(key)
                if isinstance(value, str) and value:
                    return value
        return None

    @staticmethod
    def _delta_text(params: dict) -> str:
        delta = params.get("delta")
        if isinstance(delta, str):
            return delta
        if isinstance(delta, dict):
            for key in ("text", "content", "output", "value"):
                value = delta.get(key)
                if isinstance(value, str):
                    return value
        for key in ("text", "output", "chunk", "content"):
            value = params.get(key)
            if isinstance(value, str):
                return value
        return ""

    @staticmethod
    def _item_id(params: dict) -> str | None:
        for key in ("itemId", "item_id"):
            value = params.get(key)
            if isinstance(value, str) and value:
                return value
        delta = params.get("delta")
        if isinstance(delta, dict):
            for key in ("itemId", "item_id"):
                value = delta.get(key)
                if isinstance(value, str) and value:
                    return value
        return None

    @staticmethod
    def _file_change_text(params: dict) -> str:
        # Only the path is surfaced; never the file contents.
        for key in ("path", "filePath", "file_path", "file", "uri"):
            value = params.get(key)
            if isinstance(value, str) and value:
                return value
        delta = params.get("delta")
        if isinstance(delta, dict):
            for key in ("path", "filePath", "file_path"):
                value = delta.get(key)
                if isinstance(value, str) and value:
                    return value
        return ""

    @staticmethod
    def _turn_status(params: dict) -> str | None:
        status = params.get("status")
        if isinstance(status, str):
            return status
        turn = params.get("turn")
        if isinstance(turn, dict):
            status = turn.get("status")
            if isinstance(status, str):
                return status
        return None

    @classmethod
    def _error_text(cls, params: dict) -> str:
        sources = [params, params.get("turn"), params.get("error")]
        for source in sources:
            if not isinstance(source, dict):
                continue
            for key in ("message", "codexErrorInfo", "code"):
                value = source.get(key)
                if isinstance(value, str) and value:
                    return value
                if isinstance(value, dict):
                    nested = value.get("message")
                    if isinstance(nested, str) and nested:
                        return nested
        return "codex app-server reported an error"

    def _map_notification(self, method: str, params: dict) -> TurnEvent | None:
        if method in ("item/agentMessage/delta", "item/agent_message/delta"):
            return TurnEvent(
                "agent_delta", text=self._delta_text(params), item_id=self._item_id(params)
            )
        if method in (
            "item/commandExecution/outputDelta",
            "item/command_execution/output_delta",
        ):
            return TurnEvent(
                "command_output", text=self._delta_text(params), item_id=self._item_id(params)
            )
        if method.startswith("item/fileChange") or method.startswith("item/file_change"):
            return TurnEvent(
                "file_change", text=self._file_change_text(params), item_id=self._item_id(params)
            )
        if method == "turn/completed":
            status = self._turn_status(params)
            if status == "failed":
                return TurnEvent(
                    "error", status=status, message=self._redact(self._error_text(params))
                )
            return TurnEvent("turn_completed", status=status)
        if method in ("error", "turn/failed"):
            status = params.get("status")
            return TurnEvent(
                "error",
                status=status if isinstance(status, str) else None,
                message=self._redact(self._error_text(params)),
            )
        return None

    @staticmethod
    def _error_message(error: Any, method: str) -> str:
        if isinstance(error, dict):
            text = error.get("message") or error.get("code") or json.dumps(error)
        else:
            text = str(error)
        return f"'{method}' failed: {text}"

    # ------------------------------------------------------------------
    # redaction
    # ------------------------------------------------------------------
    def _redact(self, value: Any) -> str:
        text = "" if value is None else str(value)
        if self._access_token:
            text = text.replace(self._access_token, "***")
        return text

    def _log(self, message: str) -> None:
        try:
            self._log_fn(self._redact(message))
        except Exception:
            pass
