#!/usr/bin/env python3
"""Offline fake ``codex app-server`` for ``vibe_tools`` tests.

This fixture speaks just enough of the stdio/JSONL protocol for the
``CodexAppServer`` client.  It is pure stdlib and is launched either through
its shebang or through the current Python interpreter (see the test helper),
never through a shell.

The behaviour is selected with ``FAKE_CODEX_SCENARIO``.  If
``FAKE_CODEX_TRANSCRIPT`` points at a file, every received message and a few
lifecycle events are appended there as JSON lines so tests can assert ordering.

Security: the fake server only checks whether ``ACCESS_TOKEN`` is present in
the environment; it never reads, stores or echoes the value.
"""

from __future__ import annotations

import json
import os
import sys

THREAD_ID = "thread-fake-1"
FLAT_THREAD_ID = "thread-flat"
TURN_ID = "turn-fake-1"
APPROVAL_ID = 9001


def _emit(message: dict) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _bad_line() -> None:
    sys.stdout.write("{this is not valid json\n")
    sys.stdout.flush()


def _record(path: str | None, entry: dict) -> None:
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")
        handle.flush()


def _turn_events(scenario: str, thread_id: str, turn_id: str) -> None:
    _emit({"method": "turn/started", "params": {"threadId": thread_id, "turnId": turn_id}})
    if scenario == "bad_json":
        _bad_line()
    _emit(
        {
            "method": "item/agentMessage/delta",
            "params": {
                "threadId": thread_id,
                "turnId": turn_id,
                "itemId": "msg-1",
                "delta": "Hello ",
            },
        }
    )
    if scenario == "bad_json":
        _bad_line()
    # Alternate common shape: delta is an object carrying the text.
    _emit(
        {
            "method": "item/agentMessage/delta",
            "params": {
                "threadId": thread_id,
                "turnId": turn_id,
                "itemId": "msg-1",
                "delta": {"text": "world"},
            },
        }
    )
    _emit(
        {
            "method": "item/commandExecution/outputDelta",
            "params": {
                "threadId": thread_id,
                "turnId": turn_id,
                "itemId": "cmd-1",
                "delta": "out\n",
            },
        }
    )
    # File changes may carry full file contents; the client must not surface
    # the diff into logs/events.
    _emit(
        {
            "method": "item/fileChange/completed",
            "params": {
                "threadId": thread_id,
                "turnId": turn_id,
                "itemId": "fc-1",
                "path": "res://main.gd",
                "diff": "SECRET-FILE-CONTENT",
            },
        }
    )
    _emit({"method": "codex/someUnknownNotification", "params": {"threadId": thread_id}})
    if scenario == "failed_turn":
        _emit(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "turn": {"id": turn_id, "status": "failed"},
                    "error": {"message": "model exploded"},
                },
            }
        )
    elif scenario == "error_notification":
        _emit({"method": "error", "params": {"threadId": thread_id, "message": "stream blew up"}})
    else:
        _emit(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "turn": {"id": turn_id, "status": "completed"},
                },
            }
        )


def main() -> int:
    scenario = os.environ.get("FAKE_CODEX_SCENARIO", "happy")
    transcript = os.environ.get("FAKE_CODEX_TRANSCRIPT")
    _record(
        transcript,
        {
            "event": "start",
            "argv": sys.argv[1:],
            "access_token_present": "ACCESS_TOKEN" in os.environ,
        },
    )

    if scenario == "exit_immediately":
        return 0

    thread_id = THREAD_ID
    turn_id = TURN_ID
    awaiting_interrupt = False
    approval_pending = False

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            message = json.loads(raw)
        except ValueError:
            _record(transcript, {"event": "bad_json_from_client"})
            continue
        _record(transcript, {"event": "recv", "msg": message})

        method = message.get("method")
        req_id = message.get("id")
        params = message.get("params") or {}

        if method is None:
            # Client response (for example an approval decision).
            if approval_pending and req_id == APPROVAL_ID:
                decision = (message.get("result") or {}).get("decision")
                _record(transcript, {"event": "approval_decision", "decision": decision})
                approval_pending = False
                _emit(
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": thread_id,
                            "turnId": turn_id,
                            "turn": {"id": turn_id, "status": "completed"},
                        },
                    }
                )
            continue

        if method == "initialize":
            if scenario == "no_initialize_response":
                continue
            if scenario == "initialize_error":
                _emit({"id": req_id, "error": {"code": -32000, "message": "init failed"}})
                continue
            if scenario == "initialize_empty":
                _emit({"id": req_id, "unexpected": True})
                continue
            _emit(
                {
                    "id": req_id,
                    "result": {
                        "userAgent": "fake-codex/0.0",
                        "clientInfo": params.get("clientInfo"),
                    },
                }
            )
        elif method == "initialized":
            pass
        elif method == "thread/start":
            if scenario == "flat_thread_id":
                _emit({"id": req_id, "result": {"threadId": FLAT_THREAD_ID}})
            else:
                _emit({"id": req_id, "result": {"thread": {"id": thread_id}}})
        elif method == "thread/resume":
            _emit(
                {
                    "id": req_id,
                    "result": {"thread": {"id": params.get("threadId") or thread_id}},
                }
            )
        elif method == "turn/start":
            _emit({"id": req_id, "result": {"turn": {"id": turn_id}}})
            if scenario == "approval":
                approval_pending = True
                _emit(
                    {
                        "id": APPROVAL_ID,
                        "method": "item/commandExecution/requestApproval",
                        "params": {"threadId": thread_id, "turnId": turn_id, "itemId": "cmd-1"},
                    }
                )
            elif scenario == "interrupt_wait":
                awaiting_interrupt = True
                _emit(
                    {
                        "method": "item/agentMessage/delta",
                        "params": {
                            "threadId": thread_id,
                            "turnId": turn_id,
                            "itemId": "msg-1",
                            "delta": "partial",
                        },
                    }
                )
            elif scenario == "exit_during_turn":
                return 0
            elif scenario == "missing_thread_id":
                _emit(
                    {
                        "method": "item/agentMessage/delta",
                        "params": {"itemId": "msg-1", "delta": "loose"},
                    }
                )
                _emit(
                    {
                        "method": "turn/completed",
                        "params": {"turn": {"id": turn_id, "status": "completed"}},
                    }
                )
            else:
                _turn_events(scenario, thread_id, turn_id)
        elif method == "turn/interrupt":
            _emit({"id": req_id, "result": {}})
            if awaiting_interrupt:
                awaiting_interrupt = False
                _emit(
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": thread_id,
                            "turnId": turn_id,
                            "turn": {"id": turn_id, "status": "interrupted"},
                        },
                    }
                )
        else:
            if req_id is not None:
                _emit({"id": req_id, "result": {}})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
