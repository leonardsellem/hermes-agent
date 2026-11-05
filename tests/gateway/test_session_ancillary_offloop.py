"""LS-3828 U5: an ancillary projection's store and config reads run off the event loop and before
``event_stream.lock``. That lock is a ``threading.RLock`` that ``/steer`` takes on the loop
(``gateway/session_busy_controls.py``), so a read blocked under it stalls the whole connection.

The store reads (``local_receipt``, ``_compression_lineage``) are stubbed to block synchronously,
as the real SQLite read did, and a loop ticker measures the stall.
"""
import asyncio
import sys
import threading
import time
import types
from types import SimpleNamespace

import pytest

from gateway import session_ancillary
from gateway import session_managed_worker
from gateway.config import Platform

SID, ROUTE = 'sid-1', 'route-1'
BLOCK_S = 1.0
MAX_LOOP_GAP_S = 0.2  # a ticker on the loop; the stub blocks for ~1 s when run inline


class Live:
    def __init__(self):
        self.route = ROUTE
        self.source = SimpleNamespace(platform=Platform.LOCAL)
        self.event_stream = SimpleNamespace(lock=threading.RLock())


class Authority:
    def __init__(self, agent):
        self.sessions = {SID: Live()}
        self.runner = SimpleNamespace(
            session_store=SimpleNamespace(_lock=threading.RLock(),
                                          _entries={ROUTE: SimpleNamespace(session_id=SID)}))
        self.db = SimpleNamespace(db_path='/tmp/session-ancillary-offloop.db')
        self._agent = agent

    def agent(self, _ref):
        return self._agent


def _blocked(barrier, value, delay=BLOCK_S):
    """A store read that blocks its thread, as the SQLite/file read does (never yields)."""
    def read(*_args, **_kwargs):
        if barrier is not None:
            barrier.set()
        time.sleep(delay)
        return value
    return read


@pytest.fixture
def rig(monkeypatch):
    monkeypatch.setattr(session_ancillary, 'authorize', lambda *_a, **_k: None)
    monkeypatch.setattr(session_managed_worker, 'managed_policy', lambda *_a, **_k: None)
    registry = types.ModuleType('tools.delegate_tool_registry')
    registry._active_subagents = {}
    registry._active_subagents_lock = threading.Lock()
    monkeypatch.setitem(sys.modules, 'tools.delegate_tool_registry', registry)
    agent = SimpleNamespace(session_id=SID, _session_db=SimpleNamespace(db_path='/tmp/x.db'),
                            gateway_session_key=ROUTE)
    authority = Authority(agent)
    return SimpleNamespace(authority=authority), SimpleNamespace(session_id=SID), {'session_id': SID}


def _stub_local_receipt(monkeypatch, **kwargs):
    module = types.ModuleType('hermes_state_local')
    module.local_receipt = _blocked(kwargs.get('barrier'), {'entry': {'session_id': SID}},
                                    kwargs.get('delay', BLOCK_S))
    monkeypatch.setitem(sys.modules, 'hermes_state_local', module)


async def _max_gap(during):
    gaps, running = [], True

    async def tick():
        last = time.monotonic()
        while running:
            await asyncio.sleep(0.01)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    task = asyncio.create_task(tick())
    await asyncio.sleep(0)
    try:
        await during()
    finally:
        running = False
        await task
    return max(gaps)


@pytest.mark.asyncio
async def test_process_list_does_not_hold_the_loop(rig, monkeypatch):
    """process-list-does-not-hold-the-loop"""
    connection, ref, params = rig
    _stub_local_receipt(monkeypatch)

    gap = await _max_gap(lambda: session_ancillary.read(connection, ref, params, kind='processes'))

    assert gap < MAX_LOOP_GAP_S, f"the loop stalled {gap:.2f}s on the process.list store read"


@pytest.mark.asyncio
async def test_subagent_list_does_not_hold_the_loop(rig, monkeypatch):
    """subagent-list-does-not-hold-the-loop"""
    connection, ref, params = rig
    _stub_local_receipt(monkeypatch, delay=0)
    monkeypatch.setattr(session_ancillary, '_compression_lineage', _blocked(None, frozenset({SID})))

    gap = await _max_gap(lambda: session_ancillary.read(connection, ref, params, kind='subagents'))

    assert gap < MAX_LOOP_GAP_S, f"the loop stalled {gap:.2f}s on the subagent.list lineage read"


@pytest.mark.asyncio
async def test_steer_lock_free_while_ancillary_store_read_runs(rig, monkeypatch):
    """steer-lock-free-while-ancillary-store-read-runs"""
    connection, ref, params = rig
    barrier = threading.Event()
    _stub_local_receipt(monkeypatch, barrier=barrier)
    lock = connection.authority.sessions[SID].event_stream.lock
    held = []

    def probe():  # what /steer does on the loop: take event_stream.lock
        assert barrier.wait(5), "the store read never started"
        started = time.monotonic()
        acquired = lock.acquire(timeout=BLOCK_S + 1)
        held.append((acquired, time.monotonic() - started))
        if acquired:
            lock.release()

    prober = threading.Thread(target=probe)
    prober.start()
    try:
        await session_ancillary.read(connection, ref, params, kind='processes')
    finally:
        prober.join(5)
    acquired, waited = held[0]
    assert acquired
    assert waited < MAX_LOOP_GAP_S, f"the steer lock was held {waited:.2f}s by the store read"


@pytest.mark.asyncio
async def test_ancillary_payloads_unchanged(rig, monkeypatch):
    """ancillary-payloads-unchanged: the prefetch moves the reads, never the projection."""
    connection, ref, params = rig
    _stub_local_receipt(monkeypatch, delay=0)
    monkeypatch.setattr(session_ancillary, '_compression_lineage', lambda *_a, **_k: frozenset({SID}))
    process = SimpleNamespace(id='p-1', command='echo hi', exited=False, exit_code=None,
                              started_at=1.0, exited_at=None, completion_reason=None,
                              output_buffer='hi\n', session_key=ROUTE, owner_task_id=SID,
                              task_id='p-1')
    registry_module = types.ModuleType('tools.process_registry')
    registry_module.process_registry = SimpleNamespace(
        _lock=threading.RLock(), _running={'p-1': process}, _finished={})
    monkeypatch.setitem(sys.modules, 'tools.process_registry', registry_module)
    registry = sys.modules['tools.delegate_tool_registry']
    registry._is_descendant_of = lambda _agent, _parent: True
    registry._active_subagents = {'sub-1': {
        'subagent_id': 'sub-1', 'parent_id': SID, 'depth': 1, 'goal': 'g', 'delegation_id': 'd',
        'model': 'm', 'started_at': 2.0, 'status': 'running', 'tool_count': 1, 'last_tool': 'read'}}

    processes = await session_ancillary.read(connection, ref, params, kind='processes')
    subagents = await session_ancillary.read(connection, ref, params, kind='subagents')

    assert processes == {'processes': [{
        'session_id': 'p-1', 'command': 'echo hi', 'status': 'running', 'exit_code': None,
        'started_epoch': 1.0, 'exited_at': None, 'completion_reason': None, 'output_tail': 'hi\n'}]}
    assert subagents == {'subagents': [{
        'subagent_id': 'sub-1', 'parent_id': SID, 'depth': 1, 'goal': 'g', 'delegation_id': 'd',
        'model': 'm', 'started_at': 2.0, 'status': 'running', 'tool_count': 1, 'last_tool': 'read',
        'accepting_steer': None}],
        'delegations': []}
