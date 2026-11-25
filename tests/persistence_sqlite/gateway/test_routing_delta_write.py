"""R2: a routing-index save touches only its delta and keeps the retired-fence return contract.

Today ``replace_routing_entries`` deletes and re-inserts the WHOLE scope (29 945 rows in two
scopes on the live 2026-11-24 store, 20 847 in the sessions scope) and re-selects every key: the
second-longest holder of the in-process writer mutex (88 of 235 refusals, p90 29.8 s).
"""
import json

import pytest

SCOPE = "/home/ubuntu/.hermes/sessions"
SEED = 20_000


@pytest.fixture
def store(backend, scope_a):
    return backend.open(scope_a)


@pytest.fixture
def media_root(tmp_path):
    return str(tmp_path / "media")


def _entry(i, value=0):
    return json.dumps({"session_id": f"s{i}", "v": value})


def _seed(store, count):
    store.holder()._write_sql(
        "INSERT INTO gateway_routing (scope, session_key, entry_json, updated_at) VALUES (?, ?, ?, ?)",
        [(SCOPE, f"k{i}", _entry(i), 1.0) for i in range(count)], many=True)


def test_one_changed_entry_touches_only_that_row(store):
    _seed(store, SEED)
    entries = {f"k{i}": _entry(i) for i in range(SEED)}
    entries["k0"] = _entry(0, value=1)
    before = store.holder()._conn.total_changes
    skipped = store.routing.replace_routing_entries(entries, routing_scope=SCOPE)
    changed = store.holder()._conn.total_changes - before
    assert skipped == set()
    assert changed <= 3, changed
    stored = store.routing.load_routing_entries(routing_scope=SCOPE)
    assert stored["k0"] == entries["k0"]
    print(f"total_changes={changed} rows={len(stored)}")


def test_a_removed_entry_is_deleted(store):
    _seed(store, 3)
    entries = {f"k{i}": _entry(i) for i in range(2)}
    assert store.routing.replace_routing_entries(entries, routing_scope=SCOPE) == set()
    assert set(store.routing.load_routing_entries(routing_scope=SCOPE)) == set(entries)


def test_a_large_delta_is_chunked(store):
    from persistence.sqlite.conversation._store.sql_common import _SQL_IN_CHUNK
    _seed(store, SEED)
    entries = {f"k{i}": _entry(i) for i in range(SEED)}
    for i in range(2_500):
        entries[f"k{i}"] = _entry(i, value=1)
    statements = []
    store.holder()._conn.set_trace_callback(statements.append)
    try:
        assert store.routing.replace_routing_entries(entries, routing_scope=SCOPE) == set()
    finally:
        store.holder()._conn.set_trace_callback(None)
    upserts = [sql for sql in statements if sql.startswith("INSERT OR REPLACE INTO gateway_routing")]
    # The trace callback expands bound values, so count each statement's row tuples (the
    # column list is the one extra "(").
    batches = [sql.count("(") - 1 for sql in upserts]
    changed = [f"k{i}" for i in range(2_500)]
    assert batches == [len(chunk) for chunk in _chunks(changed, _SQL_IN_CHUNK)], batches[:3]
    assert sum(batches) == 2_500 and max(batches) <= _SQL_IN_CHUNK, batches
    stored = store.routing.load_routing_entries(routing_scope=SCOPE)
    assert all(stored[f"k{i}"] == entries[f"k{i}"] for i in range(2_500))
    print(f"batches={len(batches)} sizes={batches}")


def test_a_retired_entry_is_still_skipped(store, media_root):
    from persistence.sqlite.conversation._store.retirement import retire_sessions
    # The fence exists BEFORE the row is seeded: an unchanged submitted key must still be
    # rejected (the whole-scope rewrite dropped the row and refused the re-insert).
    store.holder()._execute_write(lambda conn: retire_sessions(conn, ["s0"], media_root=media_root))
    _seed(store, 1)
    entries = {"k0": _entry(0)}
    skipped = store.routing.replace_routing_entries(entries, routing_scope=SCOPE)
    assert skipped == {"k0"}, skipped
    assert "k0" not in store.routing.load_routing_entries(routing_scope=SCOPE)


def _chunks(items, size):
    for start in range(0, len(items), size):
        yield items[start:start + size]
