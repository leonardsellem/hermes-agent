"""U1-5: a prune that raises mid-pass still retires the live sessions and routes it deleted.

``prune_sessions`` reports each committed chunk through ``on_committed`` (LS-4187 R1). The
owner-run ``session.prune`` used to call ``retire_live_sessions`` only on a clean return, so a
failure at chunk N leaked the in-memory routes of chunks 1..N-1.
"""
import asyncio
from types import SimpleNamespace

import pytest

from gateway import session_prune


class _Store:
    def __init__(self, profile_dir):
        self.scope = SimpleNamespace(profile_dir=profile_dir)
        self.maintenance = SimpleNamespace(prune_sessions=self._prune)
        self.routing = SimpleNamespace()

    @staticmethod
    def _prune(**kwargs):
        kwargs["on_committed"](("s1", "s2"))
        raise RuntimeError("chunk write failed")


def test_a_failed_prune_still_retires_the_committed_chunks(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    calls = []
    monkeypatch.setattr("gateway.session_mutations.retire_live_sessions",
                        lambda authority, ids: calls.append(list(ids)))
    store = _Store(tmp_path)
    monkeypatch.setattr("gateway.session_persistence.authority_persistence", lambda authority: store)
    authority = SimpleNamespace()
    with pytest.raises(RuntimeError, match="chunk write failed"):
        asyncio.run(session_prune._apply(authority, {"older_than_days": 90}, None))
    assert calls == [["s1", "s2"]]
