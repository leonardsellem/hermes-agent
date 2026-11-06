"""Canonical Kanban receipts retain the shared one-shot outcome semantics."""
import json
import os
from types import SimpleNamespace

import pytest

# This checkout is a worktree under the real home: the worker's entry-point bootstrap must be
# imported before the real-home I/O guard installs, or its own `<checkout>/../manifest.json` probe
# is refused and every run_worker_turns test fails for the envelope, not the code.
from gateway import session_managed_worker  # noqa: F401
from gateway import session_kanban
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect_closing


def _bound_claim(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    kb.init_db()
    path = kb.kanban_db_path()
    with connect_closing(path) as conn:
        task_id = kb.create_task(conn, title="private fixture", assignee="default")
        task = kb.claim_task(conn, task_id, claimer="private-host:owner")
        assert task is not None
        context = {"db": str(path), "task_id": task_id,
                   "run_id": task.current_run_id, "claim_lock": task.claim_lock}
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, "worker_bound",
                             {"pid": os.getpid(), "claim_lock": task.claim_lock},
                             run_id=task.current_run_id)
    return path, context


@pytest.mark.parametrize(
    "result, expected",
    [
        ({"completed": True}, 0),
        ({"failed": True}, 1),
        ({"partial": True}, 1),
        ({"completed": False}, 1),
        ({"interrupted": True}, 130),
        (None, 1),
        *[({"failed": True, "failure_reason": reason}, kb.KANBAN_RATE_LIMIT_EXIT_CODE)
          for reason in ("rate_limit", "upstream_rate_limit", "billing", "overloaded", "server_error", "timeout")],
        *[({"failed": True, "failure_reason": reason}, kb.KANBAN_TERMINAL_PROVIDER_EXIT_CODE)
          for reason in ("auth", "auth_permanent", "model_not_found", "ssl_cert_verification", "upstream_blocked")],
    ],
)
def test_managed_result_persists_exact_worker_exit(tmp_path, monkeypatch, result, expected):
    path, context = _bound_claim(tmp_path, monkeypatch)
    monkeypatch.setattr(session_kanban, "_run_task_turns", lambda *_args: result)
    frame = {"policy": {"kanban_json": json.dumps(context)}}

    assert session_kanban.run_worker_turns(SimpleNamespace(), frame, []) is result
    assert session_kanban.worker_exit_code(path, context) == expected
    assert session_kanban.worker_exit_code(path, context | {"claim_lock": "foreign"}) == 1


def _credential_failures():
    from hermes_cli.auth import AuthError
    from hermes_cli.auth_constants import CODEX_RATE_LIMITED_CODE
    from agent.auxiliary_unavailable import ProviderCredentialsExhaustedError
    wrapped = RuntimeError("agent init failed")
    wrapped.__cause__ = AuthError("refresh revoked", provider="nous", relogin_required=True)
    relogin_over_pool = AuthError("relogin", provider="nous", relogin_required=True)
    relogin_over_pool.__cause__ = ProviderCredentialsExhaustedError("pool exhausted", provider="openai-codex")
    return [
        (AuthError("quota", provider="openai-codex", code=CODEX_RATE_LIMITED_CODE), kb.KANBAN_RATE_LIMIT_EXIT_CODE),
        (AuthError("relogin", provider="nous", relogin_required=True), kb.KANBAN_TERMINAL_PROVIDER_EXIT_CODE),
        (wrapped, kb.KANBAN_TERMINAL_PROVIDER_EXIT_CODE),
        (relogin_over_pool, kb.KANBAN_TERMINAL_PROVIDER_EXIT_CODE),
        (RuntimeError("unknown startup failure"), 1),
    ]


@pytest.mark.parametrize("error, expected", _credential_failures())
def test_managed_start_failure_books_the_one_shot_credential_exit(tmp_path, monkeypatch, error, expected):
    """Credentials that fail before any turn (agent construction) keep the one-shot CLI's mapping:
    a quota wall is EX_TEMPFAIL, an explicit re-login is EX_CONFIG, anything else stays 1."""
    path, context = _bound_claim(tmp_path, monkeypatch)
    frame = {"policy": {"kanban_json": json.dumps(context)}}

    session_kanban.record_start_failure(frame, error)
    result = session_kanban.worker_result(path, context)
    assert result.get("exit_code") == expected
    assert "start_failure" not in result


def _pool_exhausted(message="pool exhausted", provider="openai-codex"):
    from agent.auxiliary_unavailable import ProviderCredentialsExhaustedError
    return ProviderCredentialsExhaustedError(message, provider=provider)


def _pool_frame(context, model="gpt-test"):
    return {"policy": {"kanban_json": json.dumps(context), "model": model}}


@pytest.mark.parametrize("wrapped", [False, True])
def test_pool_exhausted_start_failure_books_75_with_redacted_diagnostic(tmp_path, monkeypatch, wrapped):
    """A provider pool exhausted during construction is the existing 75 (quota wall), and the
    diagnostic names the class/provider/requested model without ever echoing the raw exception."""
    path, context = _bound_claim(tmp_path, monkeypatch)
    error = _pool_exhausted("RAW-SECRET-ish text")
    if wrapped:
        outer = RuntimeError("agent init failed")
        outer.__cause__ = error
        error = outer

    session_kanban.record_start_failure(_pool_frame(context), error)

    result = session_kanban.worker_result(path, context)
    assert result["exit_code"] == 75
    assert result["start_failure"] == {"phase": "construction", "class": "provider_pool_exhausted",
                                       "provider": "openai-codex", "requested_model": "gpt-test"}
    assert "RAW-SECRET-ish" not in json.dumps(result)


def test_pool_exhausted_402_and_429_messages_book_the_same_class(tmp_path, monkeypatch):
    """402 and 429 both say ``provider_pool_exhausted``: the class never claims which one happened."""
    path, context = _bound_claim(tmp_path, monkeypatch)
    frame = _pool_frame(context)
    results = []
    for message in ("Provider 'openai-codex' is out of usable credentials (402)",
                    "Provider 'openai-codex' is cooling down after a rate limit (429)"):
        session_kanban.record_start_failure(frame, _pool_exhausted(message))
        results.append(session_kanban.worker_result(path, context))

    expected = {"phase": "construction", "class": "provider_pool_exhausted",
                "provider": "openai-codex", "requested_model": "gpt-test"}
    assert [result["start_failure"] for result in results] == [expected, expected]
    for result in results:
        blob = json.dumps(result)
        assert "402" not in blob and "429" not in blob and "quota" not in blob


def test_pool_exhausted_after_conversation_is_not_neutralised(tmp_path, monkeypatch):
    """The exact case is a CONSTRUCTION failure: the same error inside the turns stays exit 1."""
    from agent.auxiliary_unavailable import ProviderCredentialsExhaustedError
    path, context = _bound_claim(tmp_path, monkeypatch)

    def run_turns(*_args):
        raise ProviderCredentialsExhaustedError("pool exhausted", provider="openai-codex")
    monkeypatch.setattr(session_kanban, "_run_task_turns", run_turns)

    with pytest.raises(ProviderCredentialsExhaustedError):
        session_kanban.run_worker_turns(SimpleNamespace(session_id="s"), _pool_frame(context), [])
    result = session_kanban.worker_result(path, context)
    assert result["exit_code"] == 1
    assert "start_failure" not in result


def test_missing_kanban_skills_keep_exit_1(tmp_path, monkeypatch):
    """A declared-but-absent skill raises after construction and is not a provider wall."""
    path, context = _bound_claim(tmp_path, monkeypatch)
    context["skills"] = ["absent"]
    monkeypatch.setattr("agent.skill_commands.build_preloaded_skills_prompt",
                        lambda *args, **kwargs: ("", [], ["absent"]))

    with pytest.raises(ValueError, match="kanban_skills_unavailable"):
        session_kanban.run_worker_turns(SimpleNamespace(session_id="s"), _pool_frame(context), [])
    result = session_kanban.worker_result(path, context)
    assert result["exit_code"] == 1
    assert "start_failure" not in result
