"""Cron admissions retain exact job identity and stored-job model policy."""
import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def test_cron_admission_uses_job_model_and_exact_retry(tmp_path, monkeypatch):
    from cron import jobs
    from gateway import run, session_cron
    from gateway.session import SessionStore
    from gateway.session_authority import SessionAuthority
    from gateway.session_contract import Principal
    from hermes_state import SessionDB
    from hermes_state_runtime import begin_runtime_epoch, RuntimeStoreError

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    (tmp_path / 'config.yaml').write_text(
        'model:\n  default: test-model\ncron:\n  preflight: false\n', encoding='utf-8')
    monkeypatch.setattr(jobs, 'get_job', lambda jid: {'id': jid, 'prompt': 'original', 'model': 'per-job-model'})
    monkeypatch.setattr(run, '_load_gateway_config', lambda: {'model': {}, 'platform_toolsets': {'cli': []}})
    db = SessionDB(tmp_path / 'state.db')
    from gateway.config import GatewayConfig
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    runner = SimpleNamespace(_draining=False, session_store=store, adapters={})
    authority = SessionAuthority(runner, profile_id=str(tmp_path), instance_id='test', db=db,
                                 epoch=begin_runtime_epoch(db, instance_id='test'))
    runner.session_authority = authority
    monkeypatch.setattr(authority, '_schedule', lambda ref: None)
    actor = Principal('owner', str(tmp_path), frozenset({'session:create', 'session:submit', 'session:control'}), 'viewer')

    async def probe():
        params = {'job_id': 'job', 'request_id': 'fire', 'extra_prompt': 'extra'}
        receipt = await session_cron.operation(authority, 'submit', params, actor)
        assert await session_cron.operation(authority, 'submit', params, actor) == receipt
        assert await session_cron.operation(authority, 'submit', params) == receipt
        with pytest.raises(RuntimeStoreError):
            await session_cron.operation(authority, 'submit', dict(params, extra_prompt='changed'), actor)
        policy = runner.adapters[next(iter(runner.adapters))].policies[receipt['session_id']]
        assert policy.model == 'per-job-model'
        assert policy.source == 'cron'

    try:
        asyncio.run(probe())
    finally:
        db.close()


@pytest.mark.asyncio
async def test_cron_cancel_between_claim_and_execution_registration_stops_the_job(tmp_path, monkeypatch):
    """A started admission whose execute() has not registered its event yet still honours cancel."""
    from contextlib import nullcontext
    import json
    from cron import scheduler
    from gateway import run, session_cron
    from gateway.session_authority import LiveSession, SessionAuthority
    from gateway.session_contract import Principal, SessionRef
    from hermes_state import SessionDB
    from hermes_state_runtime import admit_session_input, begin_runtime_epoch, claim_session_input

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    db = SessionDB(tmp_path / 'state.db')
    db.create_session('s', source='cron')
    authority = SessionAuthority(SimpleNamespace(_draining=False), profile_id='p', instance_id='test', db=db,
                                 epoch=begin_runtime_epoch(db, instance_id='test'))
    authority.sessions['s'] = LiveSession(SimpleNamespace(platform=None, user_id='cron-owner'), 'route')
    actor = Principal('cron-owner', 'p', frozenset({'session:create', 'session:submit', 'session:control'}), 'ticker')
    ref = SessionRef('p', 's')
    monkeypatch.setattr(run, '_profile_runtime_scope', lambda home: nullcontext())
    seen = {}

    def run_job(job, *, extra_prompt, execution_id, cancel_event):
        seen['cancelled_at_start'] = cancel_event.is_set()
        return (False, '', '', 'cancelled')
    monkeypatch.setattr(scheduler, 'run_job', run_job)

    with db:
        admit_session_input(db, epoch=authority.epoch, principal_id='cron-owner', session_id='s',
                            request_id='cron:job:fire', payload={'text': ''})
        row = claim_session_input(db, epoch=authority.epoch, session_id='s')
        # The claim is committed and visible as `started`; execute() has not run yet.
        params = {'session_id': 's', 'admission_id': row['admission_id']}
        assert await session_cron.operation(authority, 'cancel', params, actor) == {'ok': True}
        policy = SimpleNamespace(request_json=json.dumps({
            'extra_prompt': None, 'request_id': 'cron:job:fire', 'cron_job': {'id': 'job'}}))
        with pytest.raises(RuntimeError):
            await session_cron.execute(authority, ref, row, policy)
    assert seen == {'cancelled_at_start': True}
    assert authority._cron_cancellations == {}


@pytest.mark.asyncio
async def test_owner_execution_binds_the_scheduler_cron_identity_inside_run_job(tmp_path, monkeypatch):
    """The owner runs the job on its own loop thread, which never saw the firing scheduler's
    ``enter_cron_execution``: ``ctx.current_cron_execution()`` must still name this fire, keyed on
    the scheduler's execution id (the admission's request id), never on the admission id."""
    from contextlib import nullcontext
    import json
    from cron import scheduler
    from cron.execution_identity import current_cron_execution
    from cron.executions import create_execution, mark_execution_running
    from gateway import run, session_cron
    from gateway.session_authority import LiveSession, SessionAuthority
    from gateway.session_contract import SessionRef
    from hermes_state import SessionDB
    from hermes_state_runtime import admit_session_input, begin_runtime_epoch, claim_session_input

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    execution = create_execution('job', source='builtin', scheduled_instant='2026-10-08T09:00:00')
    record = mark_execution_running(execution['id'])
    db = SessionDB(tmp_path / 'state.db')
    db.create_session('s', source='cron')
    authority = SessionAuthority(SimpleNamespace(_draining=False), profile_id='p', instance_id='test', db=db,
                                 epoch=begin_runtime_epoch(db, instance_id='test'))
    authority.sessions['s'] = LiveSession(SimpleNamespace(platform=None, user_id='cron-owner'), 'route')
    monkeypatch.setattr(run, '_profile_runtime_scope', lambda home: nullcontext())
    seen = {}

    def run_job(job, *, extra_prompt, execution_id, cancel_event):
        seen['execution_id'] = execution_id
        seen['identity'] = current_cron_execution()
        return (True, 'doc', 'answer', None)
    monkeypatch.setattr(scheduler, 'run_job', run_job)
    request_id = 'cron:job:' + execution['id']
    with db:
        admit_session_input(db, epoch=authority.epoch, principal_id='cron-owner', session_id='s',
                            request_id=request_id, payload={'text': ''})
        row = claim_session_input(db, epoch=authority.epoch, session_id='s')
        policy = SimpleNamespace(request_json=json.dumps({
            'extra_prompt': None, 'request_id': request_id, 'cron_job': {'id': 'job', 'name': 'Job'}}))
        assert await session_cron.execute(authority, SessionRef('p', 's'), row, policy) == 'answer'
    ident = seen['identity']
    assert seen['execution_id'] == execution['id'] != row['admission_id']
    assert ident is not None and (ident.job_id, ident.job_name, ident.execution_id) == ('job', 'Job', execution['id'])
    assert (ident.source, ident.scheduled_instant, ident.started_at) == (
        'builtin', record['scheduled_instant'], record['started_at'])
    assert current_cron_execution() is None


def test_draining_owner_still_answers_status_and_cancel_of_admitted_cron_work(tmp_path, monkeypatch):
    """A stop/restart sets ``_draining`` while it waits for in-flight cron work. The in-gateway
    firer polls ``status`` until the run settles: refusing it made the firer raise
    CronExecutionUnknown and pause the job although the run completed. New fires stay fenced."""
    import threading
    from cron import jobs
    from cron.scheduler_authority import run_canonical_job
    from gateway import run, session_cron
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway.session_authority import SessionAuthority
    from hermes_state import SessionDB
    from hermes_state_runtime import (RuntimeStoreError, begin_runtime_epoch, claim_session_input,
                                      settle_session_input)

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(jobs, 'get_job', lambda jid: {'id': jid, 'prompt': 'p', 'model': 'm'})
    monkeypatch.setattr(run, '_load_gateway_config', lambda: {'model': {}, 'platform_toolsets': {'cli': []}})
    db = SessionDB(tmp_path / 'state.db')
    runner = SimpleNamespace(_draining=False, session_store=SessionStore(tmp_path / 'sessions', GatewayConfig()),
                             adapters={})
    authority = SessionAuthority(runner, profile_id=str(tmp_path), instance_id='test', db=db,
                                 epoch=begin_runtime_epoch(db, instance_id='test'))
    runner.session_authority = authority
    admitted = threading.Event()
    monkeypatch.setattr(authority, '_schedule', lambda ref: admitted.set())
    polled_while_draining = threading.Event()
    real_operation = session_cron.operation

    async def observed_operation(owner, name, params, actor=None):
        draining = runner._draining
        result = await real_operation(owner, name, params, actor)
        if name == 'status' and draining:
            polled_while_draining.set()  # answered, not refused, during the drain
        return result
    monkeypatch.setattr(session_cron, 'operation', observed_operation)

    async def probe():
        session_cron.bind_owner(authority)
        try:
            firer = asyncio.create_task(asyncio.to_thread(run_canonical_job, {'id': 'job'}, execution_id='fire'))
            assert await asyncio.to_thread(admitted.wait, 10), firer
            runner._draining = True  # shutdown begins while the admitted run executes
            sid = next(iter(authority.sessions))
            row = claim_session_input(db, epoch=authority.epoch, session_id=sid)
            assert await session_cron.operation(
                authority, 'cancel', {'session_id': sid, 'admission_id': row['admission_id']}) == {'ok': True}
            # the firer polls status at least once during the drain, and is answered
            assert await asyncio.to_thread(polled_while_draining.wait, 10), 'firer never polled status during the drain'
            settle_session_input(db, epoch=authority.epoch, admission_id=row['admission_id'],
                                 generation=row['generation'], outcome='completed',
                                 result={'result': {'cron_result': [True, 'doc', 'answer', None]}, 'usage': {}})
            assert await asyncio.wait_for(firer, 10) == (True, 'doc', 'answer', None)
            with pytest.raises(RuntimeStoreError, match='runtime_draining'):
                await session_cron.operation(authority, 'submit',
                                             {'job_id': 'job', 'request_id': 'next', 'extra_prompt': None})
        finally:
            session_cron.unbind_owner(authority)

    try:
        asyncio.run(probe())
    finally:
        db.close()


# --- Owner fires that stop before the agent runs must end the committed row -------------
#
# A gateway-owned fire commits its session row at admission (``session_cron._create``),
# before ``run_job`` reaches any gate. Every gate exit then used to return without ending
# it: untitled, unended, empty — 960 such rows/day from four 5m monitor watchers. These
# tests drive the REAL producer chain (real SessionDB, real ``operation('submit')``, real
# ``execute``, real ``run_job``) and stub only monitor/script I/O and the agent.

_EARLY_EXIT_CASES = ('config_unparseable', 'payload_empty', 'monitor_source_failed',
                     'injection_blocked', 'empty_prompt', 'setup_blocked', 'exception_in_try')
_NO_STORE_CASES = ('monitor_no_change', 'monitor_source_failed', 'wake_gate_false',
                   'empty_prompt', 'injection_blocked', 'payload_empty',
                   'config_unparseable', 'setup_blocked', 'exception_in_try')
_FINAL_REASONS = ('cron_complete', 'cron_incomplete_no_output')


def _gate_job(case):
    job = {'id': 'watch-job', 'name': 'Watch Job', 'prompt': 'check the thing', 'model': 'test-model',
           'schedule': 'every 5m'}
    if case in ('monitor_no_change', 'monitor_source_failed'):
        job['monitor_script'] = 'echo watched'
    elif case == 'wake_gate_false':
        job['script'] = 'scripts/watch.sh'
    elif case == 'payload_empty':
        job['prompt'] = ''
    return job


def _owner_fire(tmp_path, monkeypatch, job, *, request_id='fire'):
    """Real owner fire: submit the admission, claim it, execute ``run_job`` through it."""
    from contextlib import nullcontext
    from cron import jobs
    from gateway import run, session_cron
    from gateway.config import GatewayConfig, Platform
    from gateway.session import SessionStore
    from gateway.session_authority import LiveSession, SessionAuthority
    from gateway.session_contract import SessionRef
    from gateway.session_local_recovery import local_adapter_map
    from hermes_state import SessionDB
    from hermes_state_runtime import begin_runtime_epoch, claim_session_input

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(jobs, 'get_job', lambda jid: job)
    monkeypatch.setattr(run, '_load_gateway_config', lambda: {'model': {}, 'platform_toolsets': {'cli': []}})
    monkeypatch.setattr(run, '_profile_runtime_scope', lambda home: nullcontext())
    db = SessionDB(tmp_path / 'state.db')
    runner = SimpleNamespace(_draining=False, session_store=SessionStore(tmp_path / 'sessions', GatewayConfig()),
                             adapters={})
    authority = SessionAuthority(runner, profile_id=str(tmp_path), instance_id='test', db=db,
                                 epoch=begin_runtime_epoch(db, instance_id='test'))
    runner.session_authority = authority
    monkeypatch.setattr(authority, '_schedule', lambda ref: None)

    async def fire(before_execute=None):
        receipt = await session_cron.operation(
            authority, 'submit', {'job_id': job['id'], 'request_id': request_id, 'extra_prompt': None})
        sid = receipt['session_id']
        # In the gateway the admission schedules the live session; here we place it directly.
        authority.sessions[sid] = LiveSession(SimpleNamespace(platform=None, user_id='cron-owner'), 'route')
        policy = local_adapter_map(authority)[Platform.LOCAL].policies[sid]
        row = claim_session_input(db, epoch=authority.epoch, session_id=sid)
        if before_execute is not None:
            before_execute()  # the admission is committed; only the run is still to come
        try:
            return await session_cron.execute(authority, SessionRef(authority.profile_id, sid), row, policy), sid
        except RuntimeError:
            # A failing run (the error gates) raises out of execute; the row is what we assert on.
            return None, sid

    return db, fire


def _row(db, sid):
    return dict(db._read_one(
        'SELECT title, title_source, ended_at, end_reason, message_count FROM sessions WHERE id = ?', (sid,)))


def _stub_gate(monkeypatch, case):
    """Stub only the I/O and the single gate each early-exit family needs."""
    from cron import monitor as cron_monitor, scheduler
    from cron.scheduler_prompt import _block_and_pause_job

    if case in ('monitor_no_change', 'monitor_source_failed'):
        failed = case == 'monitor_source_failed'
        monkeypatch.setattr(cron_monitor, 'check_monitor', lambda job: SimpleNamespace(
            ok=not failed, changed=False, first_run=False,
            context_block=None, error='source down' if failed else None))
    if case == 'wake_gate_false':
        monkeypatch.setattr(scheduler, '_run_job_script_with_claim_heartbeat',
                            lambda *a, **k: (True, '{"wakeAgent": false}'))
    if case == 'config_unparseable':
        def _refuse(*_a, **_k):
            from hermes_cli.config import InvalidUserConfigError
            raise InvalidUserConfigError('config.yaml is not valid YAML')
        monkeypatch.setattr('hermes_cli.config.require_parseable_user_config', _refuse)
    if case == 'payload_empty':
        # Jobs-store I/O of the legacy-empty-payload pause, not part of this contract.
        monkeypatch.setattr(scheduler, '_block_and_pause_job',
                            lambda *a, **k: (False, 'empty payload\n', '', 'empty payload'))
    if case == 'injection_blocked':
        def _blocked(*_a, **_k):
            raise scheduler.CronPromptInjectionBlocked('invisible unicode in skill content')
        monkeypatch.setattr(scheduler, '_build_job_prompt', _blocked)
    if case == 'empty_prompt':
        monkeypatch.setattr(scheduler, '_build_job_prompt', lambda *a, **k: None)
    if case == 'setup_blocked':
        monkeypatch.setattr(scheduler, '_resolve_cron_agent_setup', lambda *a, **k: SimpleNamespace(
            blocked=(False, '# Cron Job: Watch Job\n\n**Status:** BLOCKED\n', '', 'preflight blocked'),
            model='m', runtime=None, prefill_messages=None, max_iterations=None, reasoning_config=None,
            fallback_model=None, credential_pool=None, fallback_notice=None))
    if case == 'exception_in_try':
        # ``_create`` loads the job config too, so raise only inside the owner run.
        from gateway import session_cron
        _real_load = scheduler._load_cron_job_config

        def _boom(*a, **k):
            if session_cron.current_execution() is not None:
                raise RuntimeError('config exploded before the store opened')
            return _real_load(*a, **k)
        monkeypatch.setattr(scheduler, '_load_cron_job_config', _boom)


def test_unchanged_monitor_fire_ends_a_titled_row(tmp_path, monkeypatch):
    """A no-change monitor tick ends the row the admission already committed."""
    job = _gate_job('monitor_no_change')
    db, fire = _owner_fire(tmp_path, monkeypatch, job)
    _stub_gate(monkeypatch, 'monitor_no_change')
    try:
        returned, sid = asyncio.run(fire())
        row = _row(db, sid)
        assert returned == '[SILENT]'
        assert row['end_reason'] == 'cron_suppressed'
        assert row['ended_at'] is not None
        assert row['message_count'] == 0
        assert row['title'] and 'Watch Job' in row['title']
    finally:
        db.close()


def test_wake_gate_false_fire_ends_a_titled_row(tmp_path, monkeypatch):
    """A script gate that answers wakeAgent=false ends the committed row too."""
    job = _gate_job('wake_gate_false')
    db, fire = _owner_fire(tmp_path, monkeypatch, job)
    _stub_gate(monkeypatch, 'wake_gate_false')
    try:
        returned, sid = asyncio.run(fire())
        row = _row(db, sid)
        assert returned == '[SILENT]'
        assert row['end_reason'] == 'cron_suppressed'
        assert row['ended_at'] is not None
        assert row['title'] and 'Watch Job' in row['title']
    finally:
        db.close()


def test_changed_monitor_fire_is_finalized_as_complete_not_suppressed(tmp_path, monkeypatch):
    """A tick that reaches the agent is still finalized by ``_finalize_cron_session``."""
    from cron import scheduler
    from hermes_state import SessionDB

    job = _gate_job('monitor_no_change')
    db, fire = _owner_fire(tmp_path, monkeypatch, job)
    _stub_gate(monkeypatch, 'monitor_no_change')
    monkeypatch.setattr('cron.monitor.check_monitor', lambda job: SimpleNamespace(
        ok=True, changed=True, first_run=True, context_block='new output', error=None))
    monkeypatch.setattr('hermes_state_registry.acquire', lambda *a, **k: db)
    monkeypatch.setattr('hermes_cli.env_loader.load_hermes_dotenv', lambda *a, **k: [])
    monkeypatch.setattr('hermes_cli.env_loader.reset_secret_source_cache', lambda *a, **k: None)
    monkeypatch.setattr('hermes_cli.runtime_provider.resolve_runtime_provider',
                        lambda *a, **k: {'api_key': 'test-key', 'base_url': 'https://example.invalid/v1',
                                         'provider': 'openrouter', 'api_mode': 'chat_completions'})
    agent = MagicMock()
    agent.run_conversation.return_value = {'final_response': 'all good'}
    monkeypatch.setattr('run_agent.AIAgent', lambda *a, **k: agent)
    try:
        returned, sid = asyncio.run(fire())
        row = _row(db, sid)
        assert returned == 'all good'
        assert row['end_reason'] in _FINAL_REASONS, row
        assert row['ended_at'] is not None
        assert row['title']
    finally:
        db.close()


@pytest.mark.parametrize('case', _EARLY_EXIT_CASES)
def test_early_exit_ends_owner_row(tmp_path, monkeypatch, case):
    """Every early-exit family leaves the committed row titled and ended."""
    job = _gate_job(case)
    db, fire = _owner_fire(tmp_path, monkeypatch, job)
    _stub_gate(monkeypatch, case)
    try:
        _result, sid = asyncio.run(fire())
        row = _row(db, sid)
        assert row['end_reason'] == 'cron_suppressed', row
        assert row['ended_at'] is not None, row
        assert row['title'] and 'Watch Job' in row['title'], row
        assert row['message_count'] == 0
    finally:
        db.close()


@pytest.mark.parametrize('case', _NO_STORE_CASES)
def test_early_exit_acquires_no_store(tmp_path, monkeypatch, case):
    """#96290 holds: an early exit never acquires the registry and never builds a store."""
    import hermes_state_registry
    from hermes_state import SessionDB

    job = _gate_job(case)
    db, fire = _owner_fire(tmp_path, monkeypatch, job)  # owner's store exists BEFORE the spies
    _stub_gate(monkeypatch, case)
    acquired, constructed = [], []

    def _acquire_spy(*a, **k):
        acquired.append(a)
        raise AssertionError('store acquired on an early exit')

    def _init_spy(self, *a, **k):
        constructed.append(a)
        raise AssertionError('SessionDB constructed on an early exit')

    def _install_spies():
        monkeypatch.setattr(hermes_state_registry, 'acquire', _acquire_spy)
        monkeypatch.setattr(SessionDB, '__init__', _init_spy)

    try:
        asyncio.run(fire(before_execute=_install_spies))
        assert acquired == [] and constructed == []
    finally:
        monkeypatch.undo()
        db.close()


def test_acquire_spy_detects_a_store_open(tmp_path, monkeypatch):
    """Control: the same spy DOES record an acquisition, so a zero above means something."""
    import hermes_state_registry
    from cron import scheduler
    from gateway import session_cron

    job = _gate_job('monitor_no_change')
    db, _fire = _owner_fire(tmp_path, monkeypatch, job)
    calls = []
    monkeypatch.setattr(hermes_state_registry, 'acquire', lambda *a, **k: (calls.append(a), db)[1])
    token = session_cron._execution.set((SimpleNamespace(db=db), 'control-session', job['id'], 'fire'))
    try:
        assert scheduler._open_cron_session_db(job) is db
        assert len(calls) == 1
    finally:
        session_cron._execution.reset(token)
        db.close()
