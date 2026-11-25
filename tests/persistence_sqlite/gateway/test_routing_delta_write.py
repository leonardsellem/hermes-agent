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


def test_delta_write_uses_bounded_sql_statements_for_large_index(store):
    """A 20k-row routing save with 1 change must not issue O(N) per-row SELECTs inside the write lock."""
    _seed(store, SEED)
    entries = {f"k{i}": _entry(i) for i in range(SEED)}
    entries["k0"] = _entry(0, value=1)
    statements = []
    store.holder()._conn.set_trace_callback(statements.append)
    try:
        skipped = store.routing.replace_routing_entries(entries, routing_scope=SCOPE)
    finally:
        store.holder()._conn.set_trace_callback(None)
    assert skipped == set()
    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    assert len(selects) <= 5, f"expected O(1) SELECTs for {SEED} rows, got {len(selects)}"


def test_range_scan_fence_matches_all_namespace_and_expiry_rules(store, media_root):
    import time
    from persistence.authority.vocabulary import RETRY_HORIZON_S
    from persistence.sqlite.conversation._store.retirement import RETIRED_PREFIX
    from persistence.sqlite.gateway._routes import ROUTE_FENCE_PREFIX

    now = time.time()
    def _setup(conn):
        # 1. own-scoped retirement marker: fences main, does NOT fence secondary 'jude'
        conn.execute("INSERT INTO state_meta(key,value) VALUES(?,?)",
                     (RETIRED_PREFIX + "s_own", json.dumps({"retired_at": now, "own": True})))
        # 2. unscoped legacy retirement marker: fences both main and secondary
        conn.execute("INSERT INTO state_meta(key,value) VALUES(?,?)",
                     (RETIRED_PREFIX + "s_unscoped", json.dumps({"retired_at": now})))
        # 3. active secondary fence: fences jude
        conn.execute("INSERT INTO state_meta(key,value) VALUES(?,?)",
                     (ROUTE_FENCE_PREFIX + "jude:s_fenced", json.dumps({"retired_at": now})))
        # 4. expired sweep fence: must NOT fence
        conn.execute("INSERT INTO state_meta(key,value) VALUES(?,?)",
                     (ROUTE_FENCE_PREFIX + "jude:s_expired",
                      json.dumps({"retired_at": now - RETRY_HORIZON_S - 60, "expires": True})))
        # 5. main fence where session DOES exist in sessions table: must NOT fence
        conn.execute("INSERT INTO sessions(id, source, started_at) VALUES('s_live_main', 'local', ?)", (now,))
        conn.execute("INSERT INTO state_meta(key,value) VALUES(?,?)",
                     (ROUTE_FENCE_PREFIX + "main:s_live_main", json.dumps({"retired_at": now})))
        # 6. main fence where session does NOT exist in sessions table: MUST fence
        conn.execute("INSERT INTO state_meta(key,value) VALUES(?,?)",
                     (ROUTE_FENCE_PREFIX + "main:s_gone_main", json.dumps({"retired_at": now})))

    store.holder()._execute_write(_setup)
    entries = {
        "agent:main:local:dm:1": json.dumps({"session_id": "s_own"}),
        "agent:jude:local:dm:1": json.dumps({"session_id": "s_own"}),
        "agent:jude:local:dm:2": json.dumps({"session_id": "s_unscoped"}),
        "agent:jude:local:dm:3": json.dumps({"session_id": "s_fenced"}),
        "agent:jude:local:dm:4": json.dumps({"session_id": "s_expired"}),
        "agent:main:local:dm:5": json.dumps({"session_id": "s_live_main"}),
        "agent:main:local:dm:6": json.dumps({"session_id": "s_gone_main"}),
    }
    skipped = store.routing.replace_routing_entries(entries, routing_scope=SCOPE)
    assert skipped == {
        "agent:main:local:dm:1",
        "agent:jude:local:dm:2",
        "agent:jude:local:dm:3",
        "agent:main:local:dm:6",
    }, skipped
    stored = set(store.routing.load_routing_entries(routing_scope=SCOPE))
    assert stored == {
        "agent:jude:local:dm:1",
        "agent:jude:local:dm:4",
        "agent:main:local:dm:5",
    }, stored


def _chunks(items, size):
    for start in range(0, len(items), size):
        yield items[start:start + size]
