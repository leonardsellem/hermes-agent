"""A script payload's ``refused`` is an outcome, not a failure (LS-3886).

Contract: a no_agent script that exits non-zero declaring ``{"outcome": "refused"}`` is a
deliberate no-op — the run ends silent (``last_status = "ok"``, ``last_outcome = "refused"``)
and ``failure_streak`` / ``last_failure`` are left exactly as they were. A real failure still
increments the streak, the refusal is read from the payload only (an auth/timeout-looking note
cannot reclassify it), and a payload ``"should_retry": false`` forbids the unreachable-model
re-run ladder from re-running the failure.
"""

from __future__ import annotations

from datetime import datetime, timedelta, UTC

import pytest


@pytest.fixture
def hermes_env(tmp_path, monkeypatch):
    """Isolate HERMES_HOME and reload the modules that cache it at import time."""
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "scripts").mkdir()
    (home / "cron").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    import importlib

    import hermes_constants

    importlib.reload(hermes_constants)
    import cron.jobs

    importlib.reload(cron.jobs)
    import cron.scheduler

    importlib.reload(cron.scheduler)
    return home


def _script(hermes_env, name: str, body: str) -> None:
    (hermes_env / "scripts" / name).write_text(f"#!/usr/bin/env bash\n{body}", encoding="utf-8")


def _run(hermes_env, name: str):
    """Create a no_agent job over *name*, run it for real, return (job, run_job result)."""
    from cron.jobs import create_job
    from cron.scheduler import run_job

    job = create_job(
        prompt=None, schedule="every 5m", script=name, no_agent=True, deliver="local")
    return job, run_job(job)


# ---------------------------------------------------------------------------
# The script seam: a refusing script is a silent success carrying its outcome
# ---------------------------------------------------------------------------


def test_refusing_script_is_silent_success_and_keeps_its_payload(hermes_env):
    from cron.scheduler import SILENT_MARKER

    _script(
        hermes_env, "refuse.sh",
        "echo '{\"outcome\":\"refused\",\"reason_code\":\"pin_hold\",\"should_retry\":false}'\n"
        "exit 1\n",
    )
    job, (success, doc, final_response, error) = _run(hermes_env, "refuse.sh")

    assert success is True, "a refusal is not a failure"
    assert error is None
    assert final_response == SILENT_MARKER, "a deliberate no-op must not alert"
    assert "refused" in doc
    assert job["_run_outcome"] == "refused"
    assert job["_should_retry"] is False


def test_real_script_failure_still_alerts_and_records_no_outcome(hermes_env):
    from cron.scheduler import SILENT_MARKER

    _script(hermes_env, "broken.sh", "echo 'boom' >&2\nexit 1\n")
    job, (success, _doc, final_response, error) = _run(hermes_env, "broken.sh")

    assert success is False
    assert final_response != SILENT_MARKER
    assert "boom" in (error or "")
    assert "_run_outcome" not in job


def test_retryable_payload_failure_still_alerts_but_carries_should_retry(hermes_env):
    _script(
        hermes_env, "retry-later.sh",
        "echo '{\"outcome\":\"failed_retryable\",\"should_retry\":false}'\nexit 3\n",
    )
    job, (success, _doc, final_response, _error) = _run(hermes_env, "retry-later.sh")

    assert success is False, "only 'refused' is exempt from failure"
    assert final_response, "a real failure still delivers its alert"
    assert "_run_outcome" not in job, "only a refusal is an outcome Hermes records"
    assert job["_should_retry"] is False


def test_exit_zero_refusal_is_also_a_silent_outcome(hermes_env):
    """AC1 names no exit code: a script that declines without a failure must stay silent."""
    from cron.jobs import get_job, mark_job_run
    from cron.scheduler import SILENT_MARKER, _RunDelivery
    from cron.scheduler_bookkeeping import finish_completed_run

    _script(
        hermes_env, "refuse-zero.sh",
        "echo '{\"outcome\":\"refused\",\"reason_code\":\"pin_hold\"}'\nexit 0\n",
    )
    job, (success, _doc, final_response, error) = _run(hermes_env, "refuse-zero.sh")

    assert success is True
    assert error is None
    assert final_response == SILENT_MARKER, "the refusal JSON must not be delivered as a message"
    assert job["_run_outcome"] == "refused"

    assert mark_job_run(job["id"], False, "Script exited with code 1\nstdout:\nboom") is True
    finish_completed_run(_RunDelivery(job=job, success=success, error=error), None, "exec-zero")
    record = get_job(job["id"])
    assert record["last_status"] == "ok"
    assert record["last_outcome"] == "refused"
    assert record["failure_streak"] == 1, "a refusal that exits 0 must not fake a recovery"


def test_refusal_json_on_stderr_is_not_an_outcome(hermes_env):
    """The contract is the last STDOUT line: stderr is the failure channel, never an outcome."""
    from cron.scheduler import SILENT_MARKER

    _script(hermes_env, "stderr-refusal.sh", "echo '{\"outcome\":\"refused\"}' >&2\nexit 1\n")
    job, (success, _doc, final_response, error) = _run(hermes_env, "stderr-refusal.sh")

    assert success is False
    assert final_response != SILENT_MARKER, "a real failure cannot be silenced by stderr"
    assert "refused" in (error or ""), "the stderr text is still reported as the failure"
    assert "_run_outcome" not in job


def test_error_looking_note_cannot_reclassify_a_payload_refusal(hermes_env):
    from cron.incidents import _classify_failure_type
    from cron.scheduler import SILENT_MARKER

    _script(
        hermes_env, "noisy-refusal.sh",
        "echo 'authentication failed: HTTP 401 timeout' >&2\n"
        "echo '{\"outcome\":\"refused\",\"reason\":\"pin_hold\"}'\n"
        "exit 3\n",
    )
    job, (success, _doc, final_response, _error) = _run(hermes_env, "noisy-refusal.sh")

    assert success is True
    assert final_response == SILENT_MARKER
    assert job["_run_outcome"] == "refused"
    # The payload's word outranks every error-text bucket; without it the text still classifies.
    assert _classify_failure_type("authentication failed: HTTP 401 timeout", "refused") == "refused"
    assert _classify_failure_type("authentication failed: HTTP 401", None) == "auth"


# ---------------------------------------------------------------------------
# Job bookkeeping: streak untouched on refusal, incremented on failure
# ---------------------------------------------------------------------------


def test_refusal_leaves_failure_streak_and_last_failure_untouched(hermes_env):
    from cron.jobs import create_job, get_job, mark_job_run

    job = create_job(prompt="watch", schedule="every 1h")
    assert mark_job_run(job["id"], False, "Script exited with code 1\nstdout:\nboom") is True
    before = get_job(job["id"])
    assert before["failure_streak"] == 1

    assert mark_job_run(job["id"], True, run_outcome="refused") is True
    after = get_job(job["id"])

    assert after["last_status"] == "ok", "a refusal is not an error"
    assert after["last_outcome"] == "refused"
    assert after["last_error"] is None
    assert after["failure_streak"] == 1, "a refusal neither increments nor resets the streak"
    assert after["last_failure"] == before["last_failure"], "the failure history is untouched"


def test_real_failure_still_increments_the_streak(hermes_env):
    from cron.jobs import create_job, get_job, mark_job_run

    job = create_job(prompt="watch", schedule="every 1h")
    assert mark_job_run(job["id"], False, "Script exited with code 1\nstdout:\nboom") is True
    assert mark_job_run(job["id"], False, "Script exited with code 1\nstdout:\nboom") is True

    failed = get_job(job["id"])
    assert failed["last_status"] == "error"
    assert failed["failure_streak"] == 2
    assert "last_outcome" not in failed


def test_a_later_non_refusal_run_clears_the_refused_outcome(hermes_env):
    from cron.jobs import create_job, get_job, mark_job_run

    job = create_job(prompt="watch", schedule="every 1h")
    assert mark_job_run(job["id"], True, run_outcome="refused") is True
    assert get_job(job["id"])["last_outcome"] == "refused"

    assert mark_job_run(job["id"], True) is True
    assert "last_outcome" not in get_job(job["id"])


def test_delivery_failed_still_behaves_as_today(hermes_env):
    from cron.jobs import create_job, get_job, mark_job_run

    job = create_job(prompt="watch", schedule="every 1h")
    assert mark_job_run(job["id"], True, delivery_error="webhook unreachable") is True

    record = get_job(job["id"])
    assert record["last_status"] == "delivery_failed"
    assert record["failure_streak"] == 0
    assert record.get("last_failure") is None
    assert "last_outcome" not in record


# ---------------------------------------------------------------------------
# The retry ladder honours the payload's should_retry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("should_retry", [None, True])
def test_should_retry_absent_or_true_keeps_the_ladder(hermes_env, should_retry):
    from cron import unreachable_retry as ur
    from cron.jobs import create_job, get_job, mark_job_run

    job = create_job(prompt="nightly", schedule="every 24h")
    assert mark_job_run(
        job["id"], False, "ConnectError: dns", model_unreachable=True,
        should_retry=should_retry,
    ) is True

    record = get_job(job["id"])
    assert record[ur.STATE_KEY]["attempt"] == 1
    assert datetime.fromisoformat(record["next_run_at"]) - datetime.now(UTC) <= timedelta(
        seconds=ur.RETRY_DELAYS_SECONDS[0] + 120)


def test_payload_should_retry_false_is_not_retried(hermes_env):
    from cron import unreachable_retry as ur
    from cron.jobs import create_job, get_job, mark_job_run

    job = create_job(prompt="nightly", schedule="every 24h")
    now = datetime.now(UTC)
    assert mark_job_run(
        job["id"], False, "ConnectError: dns", model_unreachable=True, should_retry=False,
    ) is True

    record = get_job(job["id"])
    assert record.get(ur.STATE_KEY) is None, "the ladder must not re-run a payload that said no"
    assert datetime.fromisoformat(record["next_run_at"]) - now > timedelta(hours=1), (
        "next fire is the natural occurrence, not a ladder rung")


def test_stderr_line_named_stdout_cannot_forge_a_refusal(hermes_env):
    """The verdict comes from the stdout STREAM, not from a block boundary guessed in text:
    a stderr line literally reading ``stdout:`` must not promote the line after it."""
    from cron.scheduler import SILENT_MARKER

    _script(
        hermes_env, "forged-marker.sh",
        "echo 'diag:' >&2\n"
        "echo 'stdout:' >&2\n"
        "echo '{\"outcome\":\"refused\"}' >&2\n"
        "exit 1\n",
    )
    job, (success, _doc, final_response, error) = _run(hermes_env, "forged-marker.sh")

    assert success is False, "the marker in stderr must not move the payload boundary"
    assert final_response != SILENT_MARKER
    assert "_run_outcome" not in job
    assert "refused" in (error or ""), "the stderr text is still reported as the failure"


def test_should_retry_veto_rides_the_owner_boundary(hermes_env, cron_owner, monkeypatch):
    """A scheduled agent run executes in the gateway, not the firing process: the pre-run
    script's should_retry verdict must cross that boundary with the run's other flags."""
    import cron.scheduler as sched
    from cron.scheduler_authority import run_canonical_job

    def fake_run_job(job, *, extra_prompt=None, cancel_event=None, execution_id=None):
        job["_should_retry"] = False
        job["_model_unreachable"] = True
        return (False, "", "", "ConnectError: dns")

    monkeypatch.setattr(sched, "run_job", fake_run_job)
    job = {"id": "boundary-flags", "name": "boundary-flags", "prompt": "p",
           "schedule": "every 1h"}

    run_canonical_job(job, execution_id="exec-boundary")

    assert job.get("_model_unreachable") is True, "control: a known run-side flag rides back"
    assert job.get("_should_retry") is False, "the retry veto was dropped at the boundary"
