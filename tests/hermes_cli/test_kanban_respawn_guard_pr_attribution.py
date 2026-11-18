"""The ``active_pr`` respawn guard attributes a PR comment to a run of its card.

Regression for LS-4168: the guard used to read every PR-URL comment in the last
24 h as "a prior worker of THIS card already opened a PR", so a PR left on the
card by another profile's run held it ready indefinitely (t_43e699f5: 13 h).
Only a comment whose stamp falls inside one of the card's own ``task_runs``
windows is its duplicate signal now. A card with no run keeps the previous
fail-closed hold, and unreadable stamps/windows hold too; a same-second tie
between the comment and a handoff event keeps the guard (fail closed).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


PR_COMMENT = "Opened https://github.com/example/repo/pull/44 for review."


def _comment_stamp(conn, comment_id: int) -> int:
    return conn.execute(
        "SELECT created_at FROM task_comments WHERE id = ?", (comment_id,)
    ).fetchone()[0]


def _set_comment_stamp(conn, comment_id: int, stamp: int) -> None:
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_comments SET created_at = ? WHERE id = ?", (stamp, comment_id)
        )


def _backdate_comment(conn, comment_id: int, seconds: int = 1) -> None:
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_comments SET created_at = created_at - ? WHERE id = ?",
            (seconds, comment_id),
        )


def _add_event(conn, task_id: str, kind: str, stamp: int) -> None:
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, ?, NULL, ?)",
            (task_id, kind, stamp),
        )


def _run_end(conn, task_id: str, outcome: str = "blocked") -> int:
    return conn.execute(
        "SELECT ended_at FROM task_runs WHERE task_id = ? AND outcome = ? "
        "ORDER BY ended_at DESC LIMIT 1",
        (task_id, outcome),
    ).fetchone()[0]


def test_pr_comment_outside_every_run_does_not_guard(kanban_home: Path) -> None:
    """A PR comment stamped after the card's only run ended is another run's
    work: it must not hold this card (LS-4168)."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="foreign pr", assignee="dev")
        assert kb.claim_task(conn, tid) is not None
        assert kb.block_task(conn, tid)

        comment_id = kb.add_comment(conn, tid, author="other-profile", body=PR_COMMENT)
        _set_comment_stamp(conn, comment_id, _run_end(conn, tid) + 10800)

        assert kbd.check_respawn_guard(conn, tid) is None


def test_no_run_still_guards(kanban_home: Path) -> None:
    """Nothing attributes the comment to a run, so the old hold survives."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="never claimed", assignee="dev")
        kb.add_comment(conn, tid, author="dev", body=PR_COMMENT)
        assert kbd.check_respawn_guard(conn, tid) == "active_pr"


def test_pr_comment_inside_own_run_still_guards(kanban_home: Path) -> None:
    """The open run's window is open-ended: its own comment is a duplicate signal."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="own pr", assignee="dev")
        assert kb.claim_task(conn, tid) is not None
        kb.add_comment(conn, tid, author="dev", body=PR_COMMENT)
        assert kbd.check_respawn_guard(conn, tid) == "active_pr"


def test_promoted_after_own_pr_still_guards(kanban_home: Path) -> None:
    """A ``promoted`` event is not a handoff: the PR's own worker stays held."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="promoted after pr", assignee="dev")
        assert kb.claim_task(conn, tid) is not None
        comment_id = kb.add_comment(conn, tid, author="dev", body=PR_COMMENT)
        _add_event(conn, tid, "promoted", _comment_stamp(conn, comment_id) + 5)
        assert kbd.check_respawn_guard(conn, tid) == "active_pr"


def test_reclaimed_after_own_pr_still_guards(kanban_home: Path) -> None:
    """A crash/reclaim is not a handoff: the worker that opened the PR is not respawned."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="reclaimed after pr", assignee="dev")
        assert kb.claim_task(conn, tid) is not None
        kb.add_comment(conn, tid, author="dev", body=PR_COMMENT)
        assert kb.reclaim_task(conn, tid) is True
        assert kbd.check_respawn_guard(conn, tid) == "active_pr"


def test_operator_handoff_after_own_pr_lifts_guard(kanban_home: Path) -> None:
    """A real reassignment AFTER the attributable PR comment names the closer (#111910)."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="handoff after pr", assignee="dev")
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
        # Widen the window so the second-granularity backdated comment stays inside it.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_runs SET started_at = started_at - 3600 WHERE id = ?",
                (claimed.current_run_id,),
            )
        comment_id = kb.add_comment(conn, tid, author="dev", body=PR_COMMENT)
        _backdate_comment(conn, comment_id)
        assert kb.block_task(conn, tid)
        assert kb.assign_task(conn, tid, "closer") is True
        assert kbd.check_respawn_guard(conn, tid) is None


def test_same_second_still_guards(kanban_home: Path) -> None:
    """A handoff stamped at the comment's exact second is a tie: fail closed."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="same second", assignee="dev")
        assert kb.claim_task(conn, tid) is not None
        comment_id = kb.add_comment(conn, tid, author="dev", body=PR_COMMENT)
        stamp = _comment_stamp(conn, comment_id)
        assert kb.block_task(conn, tid)
        assert kb.assign_task(conn, tid, "closer") is True
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_events SET created_at = ? "
                "WHERE task_id = ? AND kind = 'assigned'",
                (stamp, tid),
            )
        assert kbd.check_respawn_guard(conn, tid) == "active_pr"


def test_recent_success_lift_unchanged(kanban_home: Path) -> None:
    """Block 3 is untouched: a status event after a completed run lifts it."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="recent success", assignee="dev")
        assert kb.claim_task(conn, tid) is not None
        assert kb.complete_task(conn, tid, result="done", force=True)
        _add_event(conn, tid, "status", _run_end(conn, tid, "completed") + 1)
        assert kbd.check_respawn_guard(conn, tid) is None
