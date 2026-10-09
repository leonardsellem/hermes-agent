"""Tool children, not their restart-safe worker, are preferred OOM victims."""

import pytest

from tools.environments import local


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("in_worker, expected", [(True, "500"), (False, "0")])
def test_foreground_child_oom_score_adj(tmp_path, monkeypatch, in_worker, expected):
    monkeypatch.setattr(local, "in_hermes_worker_scope", lambda: in_worker)
    env = local.LocalEnvironment(cwd=str(tmp_path), timeout=10)
    try:
        result = env.execute("cat /proc/self/oom_score_adj", timeout=10)
        assert result["returncode"] == 0, result
        assert result["output"].strip() == expected
    finally:
        env.cleanup()
