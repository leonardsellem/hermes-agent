"""A managed Kanban run's lost-ACK recovery honours the exit its authority recorded.

``tasks.worker_pid`` is the dispatcher's spawned submitter, but the managed interpreter that ran
the turns lives under the profile authority and writes ``worker_result`` with ITS pid (the one
``worker_bound`` recorded). The dead-worker sweep must accept that result under the same claim,
so a quota wall (exit 75) requeues as rate-limited instead of booking a crash.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from gateway import session_kanban
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kbd._recent_worker_exits.clear()
    kb.init_db()
    with kbc.connect() as c:
        yield c


def _dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


@pytest.mark.parametrize("bound_claim, expected", [("own", "rate_limited"), ("foreign", "crashed")])
def test_managed_exit_75_requeues_rate_limited_not_crashed(conn, bound_claim, expected):
    host = kb._claimer_id().split(":", 1)[0]
    tid = kb.create_task(conn, title="managed", assignee="w")
    task = kb.claim_task(conn, tid, claimer=f"{host}:owner")
    assert task is not None
    submitter, interpreter = _dead_pid(), _dead_pid()
    assert submitter != interpreter
    kbd._set_worker_pid(conn, tid, submitter)
    claim = task.claim_lock if bound_claim == "own" else "someone-else"
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "worker_bound", {"pid": interpreter, "claim_lock": claim, "started_at": "x"},
                         run_id=task.current_run_id)
        kb._append_event(conn, tid, "worker_result", {"pid": interpreter, "claim_lock": task.claim_lock,
                         "exit_code": kb.KANBAN_RATE_LIMIT_EXIT_CODE, "last_output": ""}, run_id=task.current_run_id)

    crashed = kbd.detect_crashed_workers(conn)

    outcome = conn.execute("SELECT outcome FROM task_runs WHERE id=?", (task.current_run_id,)).fetchone()[0]
    assert outcome == expected
    assert (tid in kbd.detect_crashed_workers._last_rate_limited) is (expected == "rate_limited")
    assert (tid in crashed) is (expected == "crashed")


START_FAILURE = {"phase": "construction", "class": "provider_pool_exhausted",
                 "provider": "openai-codex", "requested_model": "gpt-test"}


def test_managed_pool_exhausted_start_failure_reaches_the_board_reason(conn):
    """A 75 carrying ``start_failure`` requeues rate-limited AND names the cause on the board."""
    host = kb._claimer_id().split(":", 1)[0]
    tid = kb.create_task(conn, title="managed pool", assignee="w")
    task = kb.claim_task(conn, tid, claimer=f"{host}:owner")
    assert task is not None
    submitter, interpreter = _dead_pid(), os.getpid()
    kbd._set_worker_pid(conn, tid, submitter)
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "worker_bound", {"pid": interpreter, "claim_lock": task.claim_lock, "started_at": "x"},
                         run_id=task.current_run_id)
        kb._append_event(conn, tid, "worker_result", {"pid": interpreter, "claim_lock": task.claim_lock,
                         "exit_code": kb.KANBAN_RATE_LIMIT_EXIT_CODE, "last_output": "",
                         "start_failure": START_FAILURE}, run_id=task.current_run_id)

    kbd.detect_crashed_workers(conn)

    outcome = conn.execute("SELECT outcome FROM task_runs WHERE id=?", (task.current_run_id,)).fetchone()[0]
    assert outcome == "rate_limited"
    last_error, failures = conn.execute(
        "SELECT last_failure_error, consecutive_failures FROM tasks WHERE id=?", (tid,)).fetchone()
    assert "provider_pool_exhausted" in last_error
    assert "openai-codex" in last_error and "gpt-test" in last_error
    assert failures == 0
    event = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='rate_limited' "
                         "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
    assert json.loads(event[0])["start_failure"] == START_FAILURE
    assert kbd.check_respawn_guard(conn, tid) == "rate_limit_cooldown"


def test_pool_exhausted_construction_never_spends_the_budget(conn, monkeypatch):
    """Three real construction failures through the production writer stay rate-limited without ever
    touching ``consecutive_failures``; a generic construction crash right after still counts."""
    from agent.auxiliary_unavailable import ProviderCredentialsExhaustedError
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "0")
    host = kb._claimer_id().split(":", 1)[0]
    tid = kb.create_task(conn, title="pool budget", assignee="w", max_retries=5)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET consecutive_failures=2 WHERE id=?", (tid,))

    def attempt(error):
        task = kb.claim_task(conn, tid, claimer=f"{host}:owner")
        assert task is not None
        kbd._set_worker_pid(conn, tid, _dead_pid())
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "worker_bound", {"pid": os.getpid(), "claim_lock": task.claim_lock},
                             run_id=task.current_run_id)
        frame = {"policy": {"kanban_json": json.dumps({"db": str(kb.kanban_db_path()), "task_id": tid,
                  "run_id": task.current_run_id, "claim_lock": task.claim_lock}), "model": "gpt-test"}}
        session_kanban.record_start_failure(frame, error)
        kbd.detect_crashed_workers(conn)
        return conn.execute("SELECT outcome FROM task_runs WHERE id=?", (task.current_run_id,)).fetchone()[0]

    outcomes = [attempt(ProviderCredentialsExhaustedError("pool exhausted", provider="openai-codex"))
                for _ in range(3)]
    assert outcomes == ["rate_limited"] * 3
    assert conn.execute("SELECT consecutive_failures FROM tasks WHERE id=?", (tid,)).fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='gave_up'",
                        (tid,)).fetchone()[0] == 0

    assert attempt(RuntimeError("unknown startup failure")) == "crashed"
    assert conn.execute("SELECT consecutive_failures FROM tasks WHERE id=?", (tid,)).fetchone()[0] == 3
    assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='gave_up'",
                        (tid,)).fetchone()[0] == 0
