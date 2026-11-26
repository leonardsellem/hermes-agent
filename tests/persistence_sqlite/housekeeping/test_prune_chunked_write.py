"""R1: the session prune commits per chunk, and a slow chunk never holds the writer mutex.

The 2026-11-24 census (journal, 7 days) attributed 103 of 235 in-process writer-mutex refusals
to ``prune_sessions``: one ``_execute_write`` over the whole selected set (p50 hold 848 s on
today's 14.8 GB store). Each chunk must commit alone, run its own post-commit tail, and a
failure at chunk N must leave every committed chunk with its tail already done.
"""
import json
import threading
import time

import pytest

from persistence.sqlite.conversation._store.messages import append_message
from persistence.sqlite.conversation._store.sessions import create_session, end_session, get_session

OLD = time.time() - 100 * 86400


@pytest.fixture
def store(backend, scope_a):
    return backend.open(scope_a)


@pytest.fixture
def db(store):
    """The store's StoreHolder, shared with its repositories."""
    return store.holder()


@pytest.fixture
def sessions_dir(tmp_path):
    return tmp_path / "sessions"


@pytest.fixture
def media_root(tmp_path):
    return str(tmp_path / "media")


def _seed(db, count=None, *, ids=None, sessions_dir):
    ids = [f"s{i:03d}" for i in range(count)] if ids is None else list(ids)
    sessions_dir.mkdir(parents=True, exist_ok=True)
    for sid in ids:
        create_session(db, sid, "cli")
        append_message(db, sid, "user", "old", timestamp=OLD)
        end_session(db, sid, "done")
        db._conn.execute("UPDATE sessions SET started_at = ?, ended_at = ? WHERE id = ?", (OLD, OLD, sid))
        (sessions_dir / f"{sid}.json").write_text("{}", encoding="utf-8")
    db._conn.commit()
    return ids


def _seed_conversation(db, members):
    """Register a local conversation (whole-lineage unit) over *members* in one policy receipt."""
    from persistence.sqlite.conversation._store.local import POLICY_PREFIX
    db._execute_write(lambda conn: conn.execute(
        "INSERT INTO state_meta (key, value) VALUES (?, ?)",
        (POLICY_PREFIX + members[0], json.dumps({"session_id": members[0], "lineage": list(members),
                                                 "entry": {"session_id": members[-1]}}))))


def _seed_receipts(db, prefix, count):
    """*count* policy receipts outside the prune: a real single-segment lineage each."""
    from persistence.sqlite.conversation._store.local import POLICY_PREFIX
    values = [(POLICY_PREFIX + f"{prefix}-{i:05d}",
               json.dumps({"session_id": f"{prefix}-{i:05d}", "lineage": [f"{prefix}-{i:05d}"],
                           "entry": {"session_id": f"{prefix}-{i:05d}"}, "request_id": f"req-{prefix}-{i}"}))
              for i in range(count)]
    db._execute_write(lambda conn: conn.executemany(
        "INSERT INTO state_meta (key, value) VALUES (?, ?)", values))


def _tail(sessions_dir, deleted_ids):
    """The caller's per-committed-chunk tail: record the ids and remove their transcript files."""
    def tail(ids):
        deleted_ids.extend(ids)
        for sid in ids:
            (sessions_dir / f"{sid}.json").unlink(missing_ok=True)
    return tail


def _spy(db, monkeypatch, *, fail_on_write_chunk=None):
    """Record every write callback by name; optionally raise on the Nth chunk write."""
    calls = []
    real = db._execute_write
    seen_chunks = []

    def spy(fn, patience_s=None):
        calls.append(fn.__name__)
        if fn.__name__ == "_write_chunk":
            seen_chunks.append(fn.__name__)
            if fail_on_write_chunk is not None and len(seen_chunks) == fail_on_write_chunk:
                raise RuntimeError("chunk write failed")
        return real(fn, patience_s)

    monkeypatch.setattr(db, "_execute_write", spy)
    return calls


def test_prune_commits_once_per_chunk(store, db, monkeypatch, sessions_dir, media_root):
    from persistence.sqlite.housekeeping.maintenance import _PRUNE_WRITE_CHUNK
    ids = _seed(db, _PRUNE_WRITE_CHUNK + 2, sessions_dir=sessions_dir)
    calls = _spy(db, monkeypatch)
    deleted_ids = []
    pruned = store.maintenance.prune_sessions(
        older_than_days=10, media_root=media_root, on_committed=_tail(sessions_dir, deleted_ids))
    assert pruned.count == len(ids)
    assert calls[0] == "_retire" and calls[-1] == "_cleanup_prompts"
    assert calls.count("_write_chunk") == 2, calls
    assert len(calls) == 2 + 2, calls
    assert sorted(deleted_ids) == sorted(ids)
    print(f"chunk_size={_PRUNE_WRITE_CHUNK} write_calls={len(calls)} chunks={calls.count('_write_chunk')}")


def test_a_blocked_chunk_does_not_hold_the_writer_mutex(store, db, sessions_dir, media_root):
    from persistence.sqlite.housekeeping.maintenance import _PRUNE_WRITE_CHUNK
    entered, release = threading.Event(), threading.Event()

    def blocking_tail(*args, **kwargs):
        entered.set()
        assert release.wait(15), "the test never released the blocked tail"

    ids = _seed(db, _PRUNE_WRITE_CHUNK, sessions_dir=sessions_dir)
    result = {}

    def prune():
        result["pruned"] = store.maintenance.prune_sessions(
            older_than_days=10, media_root=media_root, on_committed=blocking_tail)

    pruner = threading.Thread(target=prune, daemon=True)
    pruner.start()
    assert entered.wait(15), "chunk 1's tail never ran"

    wrote = threading.Event()

    def other_writer():
        db._execute_write(lambda conn: conn.execute(
            "INSERT INTO state_meta (key, value) VALUES ('probe', '1')"))
        wrote.set()

    writer = threading.Thread(target=other_writer, daemon=True)
    writer.start()
    assert wrote.wait(5), "a second writer was held while chunk 1's tail was blocked"
    release.set()
    pruner.join(15)
    assert not pruner.is_alive()
    assert result["pruned"].count == len(ids)


def test_a_failed_chunk_leaves_no_committed_chunk_without_its_tail(store, db, monkeypatch, sessions_dir, media_root):
    from persistence.sqlite.conversation._store.retirement import RETIRED_PREFIX
    from persistence.sqlite.housekeeping.maintenance import _PRUNE_WRITE_CHUNK
    ids = _seed(db, _PRUNE_WRITE_CHUNK + 1, sessions_dir=sessions_dir)
    _spy(db, monkeypatch, fail_on_write_chunk=2)
    deleted_ids = []
    with pytest.raises(RuntimeError, match="chunk write failed"):
        store.maintenance.prune_sessions(
            older_than_days=10, media_root=media_root, on_committed=_tail(sessions_dir, deleted_ids))
    first, second = ids[:_PRUNE_WRITE_CHUNK], ids[_PRUNE_WRITE_CHUNK:]
    assert sorted(deleted_ids) == sorted(first)
    for sid in first:
        assert get_session(db, sid) is None
        assert not (sessions_dir / f"{sid}.json").exists()
        assert db._conn.execute("SELECT 1 FROM state_meta WHERE key = ?",
                                (RETIRED_PREFIX + sid,)).fetchone() is not None
    for sid in second:
        assert get_session(db, sid) is not None
        assert (sessions_dir / f"{sid}.json").exists()
        # The fence and the delete share one transaction: the uncommitted chunk is NOT fenced.
        assert db._conn.execute("SELECT 1 FROM state_meta WHERE key = ?",
                                (RETIRED_PREFIX + sid,)).fetchone() is None


def test_a_resume_after_the_retirement_write_keeps_its_session(store, db, monkeypatch, sessions_dir, media_root):
    """A message written between the retirement write and a chunk's delete must survive.

    The chunk re-validates its selection inside the transaction that fences and deletes, so a
    session that became active after the snapshot is skipped (and never fenced).
    """
    from persistence.sqlite.conversation._store.retirement import RETIRED_PREFIX
    from persistence.sqlite.housekeeping.maintenance import _PRUNE_WRITE_CHUNK
    ids = _seed(db, _PRUNE_WRITE_CHUNK + 1, sessions_dir=sessions_dir)
    revived = ids[-1]
    real = db._execute_write

    def spy(fn, patience_s=None):
        result = real(fn, patience_s)
        if fn.__name__ == "_retire":
            append_message(db, revived, "user", "new turn after the retirement write")
        return result

    monkeypatch.setattr(db, "_execute_write", spy)
    deleted_ids = []
    pruned = store.maintenance.prune_sessions(
        older_than_days=10, media_root=media_root, on_committed=_tail(sessions_dir, deleted_ids))
    assert pruned.count == len(ids) - 1
    assert revived not in deleted_ids
    assert get_session(db, revived) is not None
    assert any("new turn after" in m.content for m in store.messages.read_conversation(revived))
    assert db._conn.execute("SELECT 1 FROM state_meta WHERE key = ?",
                            (RETIRED_PREFIX + revived,)).fetchone() is None


def test_the_slowest_prune_write_stays_under_the_budget(store, db, monkeypatch, sessions_dir, media_root):
    from persistence.sqlite.housekeeping.maintenance import _PRUNE_WRITE_CHUNK
    ids = _seed(db, _PRUNE_WRITE_CHUNK * 2, sessions_dir=sessions_dir)
    _seed_receipts(db, "budget", 2_000)
    real = db._execute_write
    slowest = [0.0]

    def timed(fn, patience_s=None):
        started = time.monotonic()
        try:
            return real(fn, patience_s)
        finally:
            slowest[0] = max(slowest[0], time.monotonic() - started)

    monkeypatch.setattr(db, "_execute_write", timed)
    pruned = store.maintenance.prune_sessions(older_than_days=10, media_root=media_root)
    assert pruned.count == len(ids)
    print(f"chunk_size={_PRUNE_WRITE_CHUNK} receipts=2000 slowest_write={slowest[0]:.3f}s")
    assert slowest[0] < 10.0


def _chunk_census(store, monkeypatch, root, foreign_receipts):
    """One two-chunk prune beside *foreign_receipts* unrelated receipts: per chunk, the
    ``state_meta`` SELECT statements it issued and the rows those reads returned."""
    from persistence.sqlite.housekeeping.maintenance import _PRUNE_WRITE_CHUNK

    db = store.holder()
    sessions_dir, media_root = root / "sessions", str(root / "media")
    members = ["conv-000", "conv-001"]
    ids = _seed(db, _PRUNE_WRITE_CHUNK + 2, sessions_dir=sessions_dir)
    _seed(db, ids=members, sessions_dir=sessions_dir)
    _seed_conversation(db, members)
    _seed_receipts(db, "foreign", foreign_receipts)

    samples = []
    state = {"armed": False, "reading": False, "statements": [], "rows": 0}
    real, real_factory = db._execute_write, db._conn.row_factory

    def trace(sql):
        state["reading"] = sql.lstrip().upper().startswith("SELECT") and "FROM state_meta" in sql
        if state["armed"] and state["reading"]:
            state["statements"].append(sql)

    def row_factory(cursor, row):
        # Count a state_meta SELECT's own rows: keyed on its shape, not only on the last trace.
        if state["armed"] and state["reading"] and tuple(d[0] for d in cursor.description) in {("key", "value"), ("value",)}:
            state["rows"] += 1
        return real_factory(cursor, row)

    def spy(fn, patience_s=None):
        if fn.__name__ != "_write_chunk":
            return real(fn, patience_s)
        state.update(armed=True, reading=False, statements=[], rows=0)
        try:
            return real(fn, patience_s)
        finally:
            state["armed"] = False
            samples.append((tuple(state["statements"]), state["rows"]))

    monkeypatch.setattr(db, "_execute_write", spy)
    db._conn.set_trace_callback(trace)
    db._conn.row_factory = row_factory
    try:
        pruned = store.maintenance.prune_sessions(older_than_days=10, media_root=media_root)
    finally:
        db._conn.set_trace_callback(None)
        db._conn.row_factory = real_factory
    return samples, pruned, len(ids) + len(members)


def test_the_chunk_read_cost_is_independent_of_the_receipt_set(backend, scope_a, scope_b, monkeypatch, tmp_path):
    """R1 residual: a chunk re-validates its own batch, not the store's whole receipt set.

    The same prune beside 2 and beside 2 000 unrelated receipts must issue the same ``state_meta``
    reads and scan the same rows; a full policy-receipt scan here is the 5.8–22.8 s hold class.
    """
    small, small_pruned, count = _chunk_census(backend.open(scope_a), monkeypatch, tmp_path / "two", 2)
    large, large_pruned, _ = _chunk_census(backend.open(scope_b), monkeypatch, tmp_path / "many", 2_000)
    assert small_pruned.count == large_pruned.count == count
    assert small == large
    assert sum(rows for _statements, rows in large) == 2  # only the pruned conversation's own receipt
    reads = [sql for statements, _rows in large for sql in statements]
    assert not [sql for sql in reads if "gateway.local_policy.v1:*" in sql], reads


def test_a_whole_conversation_is_deleted_in_one_chunk(store, db, monkeypatch, sessions_dir, media_root):
    """A local conversation is one unit: it never spans two transactions, even below the chunk size."""
    from persistence.sqlite.conversation._store.retirement import RETIRED_PREFIX
    from persistence.sqlite.housekeeping import maintenance
    sessions_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(maintenance, "_PRUNE_WRITE_CHUNK", 1)
    members = ["conv-000", "conv-001"]
    _seed(db, ids=members, sessions_dir=sessions_dir)
    _seed_conversation(db, members)
    calls = _spy(db, monkeypatch)
    deleted_ids = []
    pruned = store.maintenance.prune_sessions(
        older_than_days=10, media_root=media_root, on_committed=_tail(sessions_dir, deleted_ids))
    assert pruned.count == 2
    assert calls.count("_write_chunk") == 1, calls
    assert sorted(deleted_ids) == members
    for sid in members:
        assert get_session(db, sid) is None
        assert db._conn.execute("SELECT 1 FROM state_meta WHERE key = ?",
                                (RETIRED_PREFIX + sid,)).fetchone() is not None


def test_a_member_becoming_live_skips_the_whole_conversation(store, db, monkeypatch, sessions_dir, media_root):
    """A queued admission on ONE member after the classification keeps the whole conversation."""
    from persistence.sqlite.conversation._store.retirement import RETIRED_PREFIX
    members = ["conv-000", "conv-001"]
    _seed(db, ids=members, sessions_dir=sessions_dir)
    _seed_conversation(db, members)
    real = db._execute_write

    def spy(fn, patience_s=None):
        result = real(fn, patience_s)
        if fn.__name__ == "_retire":
            db._execute_write(lambda conn: conn.execute(
                "INSERT INTO session_admissions (admission_id, request_id, principal_id, "
                "target_session_id, lineage_json, payload_json, payload_digest, intent, status) "
                "VALUES ('adm-1', 'req-1', 'human', 'conv-001', '[]', '{}', 'd', 'queue', 'queued')"))
        return result

    monkeypatch.setattr(db, "_execute_write", spy)
    deleted_ids = []
    pruned = store.maintenance.prune_sessions(
        older_than_days=10, media_root=media_root, on_committed=_tail(sessions_dir, deleted_ids))
    assert pruned.count == 0
    assert deleted_ids == []
    for sid in members:
        assert get_session(db, sid) is not None
        assert db._conn.execute("SELECT 1 FROM state_meta WHERE key = ?",
                                (RETIRED_PREFIX + sid,)).fetchone() is None


def test_a_conversation_that_grows_after_classification_is_deferred(store, db, monkeypatch, sessions_dir, media_root):
    """A lineage member added after the retirement write defers the whole conversation."""
    from persistence.sqlite.conversation._store.local import POLICY_PREFIX
    from persistence.sqlite.conversation._store.retirement import RETIRED_PREFIX
    members = ["conv-000", "conv-001"]
    _seed(db, ids=members, sessions_dir=sessions_dir)
    _seed_conversation(db, members)
    real = db._execute_write

    def spy(fn, patience_s=None):
        result = real(fn, patience_s)
        if fn.__name__ == "_retire":
            db._execute_write(lambda conn: conn.execute(
                "UPDATE state_meta SET value = ? WHERE key = ?",
                (json.dumps({"session_id": "conv-000", "lineage": ["conv-000", "conv-001", "conv-002"]}),
                 POLICY_PREFIX + "conv-000")))
        return result

    monkeypatch.setattr(db, "_execute_write", spy)
    deleted_ids = []
    pruned = store.maintenance.prune_sessions(
        older_than_days=10, media_root=media_root, on_committed=_tail(sessions_dir, deleted_ids))
    assert pruned.count == 0
    assert deleted_ids == []
    for sid in members:
        assert get_session(db, sid) is not None
        assert db._conn.execute("SELECT 1 FROM state_meta WHERE key = ?",
                                (RETIRED_PREFIX + sid,)).fetchone() is None


def test_a_conversation_wider_than_the_chunk_rides_alone_and_is_measured(store, db, monkeypatch, sessions_dir,
                                                                        media_root):
    """A unit wider than the chunk is one transaction, and its width is printed with its cost."""
    from persistence.sqlite.conversation._store.retirement import RETIRED_PREFIX
    from persistence.sqlite.housekeeping import maintenance
    members = [f"wide-{i:03d}" for i in range(maintenance._PRUNE_WRITE_CHUNK * 2)]
    _seed(db, ids=members, sessions_dir=sessions_dir)
    _seed_conversation(db, members)
    calls = _spy(db, monkeypatch)
    real = db._execute_write
    slowest = [0.0]

    def timed(fn, patience_s=None):
        started = time.monotonic()
        try:
            return real(fn, patience_s)
        finally:
            slowest[0] = max(slowest[0], time.monotonic() - started)

    monkeypatch.setattr(db, "_execute_write", timed)
    pruned = store.maintenance.prune_sessions(older_than_days=10, media_root=media_root)
    assert pruned.count == len(members)
    assert calls.count("_write_chunk") == 1, calls
    for sid in members:
        assert get_session(db, sid) is None
        assert db._conn.execute("SELECT 1 FROM state_meta WHERE key = ?",
                                (RETIRED_PREFIX + sid,)).fetchone() is not None
    print(f"widest_unit={len(members)} chunk={maintenance._PRUNE_WRITE_CHUNK} "
          f"slowest_write={slowest[0]:.3f}s")
