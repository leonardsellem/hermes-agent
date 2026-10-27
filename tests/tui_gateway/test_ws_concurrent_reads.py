"""The WS concurrent-read lane (#134889 GW-18 review): waiting context reads stay bounded, a server
shutdown or client hang-up never waits for reads that have not started, and a reply the read lane
cannot send never cancels the control handler the serial lane is running.

The real ``handle_ws`` dispatcher; only ``_dispatch_request`` is a gated async stand-in.
"""
import asyncio
import json
import logging
import re

import pytest

from tui_gateway import server
from tui_gateway import ws as ws_mod


class Socket:
    def __init__(self):
        self.inbound = asyncio.Queue()
        self.received = 0
        self.sent = []
        self.fail_ids = set()

    async def accept(self):
        pass

    async def send_text(self, line):
        frame = json.loads(line)
        if frame.get("id") in self.fail_ids:
            raise RuntimeError("peer gone")
        self.sent.append(frame)

    async def receive_text(self):
        item = await self.inbound.get()
        if item is None:
            raise ws_mod._WebSocketDisconnect()
        self.received += 1
        return json.dumps(item)

    async def close(self):
        pass


@pytest.fixture
def rig(monkeypatch):
    gates, log = {}, []

    async def dispatch(authority, req, method, transport):
        log.append(f"start:{req['id']}")
        try:
            gate = gates.get(method)
            if gate is not None:
                await gate.wait()
        except asyncio.CancelledError:
            log.append(f"cancelled:{req['id']}")
            raise
        log.append(f"done:{req['id']}")
        return {"jsonrpc": "2.0", "id": req["id"], "result": {}}

    monkeypatch.setattr(ws_mod, "_dispatch_request", dispatch)
    monkeypatch.setattr(ws_mod, "_start_backend_liveness", lambda: None)
    monkeypatch.setattr(server, "_ensure_skin_watcher", lambda: None)
    monkeypatch.setattr(server, "_ensure_lease_watcher", lambda: None)
    monkeypatch.setattr(server, "_WS_ORPHAN_REAP_GRACE_S", 0)
    monkeypatch.setattr(server, "_close_sessions_for_transport", lambda transport, end_reason: (log.append("teardown"), (0, 0))[1])
    return Socket(), gates, log


def rpc(req_id, method="session.context_breakdown"):
    return {"jsonrpc": "2.0", "id": req_id, "method": method, "params": {}}


async def settle(predicate, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            return False
        await asyncio.sleep(0.01)
    return True


@pytest.mark.asyncio
async def test_waiting_context_reads_keep_reader_backpressure(rig):
    sock, gates, log = rig
    gates["session.context_breakdown"] = release = asyncio.Event()
    task = asyncio.create_task(ws_mod.handle_ws(sock))
    burst = ws_mod._WS_DISPATCH_QUEUE_MAX * 4
    for n in range(burst):
        sock.inbound.put_nowait(rpc(n))
    try:
        # A bounded reader parks with frames unread; give it every chance to drain them all.
        assert not await settle(lambda: sock.inbound.empty(), timeout=2.0)
        bound = ws_mod._WS_DISPATCH_QUEUE_MAX + ws_mod._WS_CONCURRENT_READ_LIMIT + ws_mod._WS_WAITING_READ_LIMIT + 2
        assert sock.received <= bound, (sock.received, bound)
        assert sum(entry.startswith("start:") for entry in log) == ws_mod._WS_CONCURRENT_READ_LIMIT
    finally:
        release.set()
    assert await settle(lambda: sock.inbound.empty())
    sock.inbound.put_nowait(None)
    await asyncio.wait_for(task, 30)
    assert len(sock.sent) == burst + 1  # gateway.ready + one reply per read


@pytest.mark.asyncio
async def test_server_shutdown_does_not_wait_for_a_running_context_read(rig):
    sock, gates, log = rig
    gates["session.context_breakdown"] = release = asyncio.Event()
    task = asyncio.create_task(ws_mod.handle_ws(sock))
    sock.inbound.put_nowait(rpc(1))
    try:
        assert await settle(lambda: "start:1" in log)
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=5)
        assert task in done, "socket shutdown still waits for the slow context read"
        assert "cancelled:1" in log
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_hang_up_drops_reads_that_never_started(rig):
    sock, gates, log = rig
    gates["session.context_breakdown"] = release = asyncio.Event()
    task = asyncio.create_task(ws_mod.handle_ws(sock))
    for n in range(6):
        sock.inbound.put_nowait(rpc(n))
    assert await settle(lambda: sock.received == 6 and sum(e.startswith("start:") for e in log) == 2)
    sock.inbound.put_nowait(None)
    assert not await settle(lambda: "teardown" in log, timeout=2.0), "teardown ran under a started read"
    release.set()
    await asyncio.wait_for(task, 10)
    assert sorted(e for e in log if e.startswith("start:")) == ["start:0", "start:1"]
    assert log[-1] == "teardown"


@pytest.mark.asyncio
async def test_a_failed_read_reply_does_not_cancel_the_running_control(rig):
    """The serial lane's handler (a submit, an interrupt, a mutate) finishes; then the link ends."""
    sock, gates, log = rig
    gates["prompt.submit"] = release = asyncio.Event()
    gates["session.context_breakdown"] = read_gate = asyncio.Event()
    sock.fail_ids.add("read")
    task = asyncio.create_task(ws_mod.handle_ws(sock))
    sock.inbound.put_nowait(rpc("read"))
    assert await settle(lambda: "start:read" in log)
    sock.inbound.put_nowait(rpc("submit", "prompt.submit"))
    assert await settle(lambda: "start:submit" in log)
    read_gate.set()  # the read's reply now fails while the submit is mid-await
    assert await settle(lambda: "done:read" in log)
    assert not await settle(lambda: task.done(), timeout=2.0), "the link ended under the running control"
    release.set()
    await asyncio.wait_for(task, 10)
    assert "cancelled:submit" not in log and "done:submit" in log
    assert log[-1] == "teardown"


@pytest.mark.asyncio
async def test_poll_methods_dispatch_off_the_control_lane(rig):
    """poll-methods-dispatch-off-the-control-lane: a parked ``process.list`` never delays a
    later operator control request on the same connection."""
    sock, gates, log = rig
    gates["process.list"] = release = asyncio.Event()
    gates["session.steer"] = steer_release = asyncio.Event()
    task = asyncio.create_task(ws_mod.handle_ws(sock))
    sock.inbound.put_nowait(rpc("poll", "process.list"))
    assert await settle(lambda: "start:poll" in log)
    sock.inbound.put_nowait(rpc("steer", "session.steer"))
    try:
        assert await settle(lambda: "start:steer" in log), "the poll parked the control lane"
        steer_release.set()
        assert await settle(lambda: "done:steer" in log)
        assert "done:poll" not in log
    finally:
        release.set()
        steer_release.set()
    sock.inbound.put_nowait(None)
    await asyncio.wait_for(task, 30)


@pytest.mark.asyncio
async def test_active_list_dispatches_off_the_control_lane(rig):
    """active-list-dispatches-off-the-control-lane: the non-canonical poll gets the same lane."""
    sock, gates, log = rig
    gates["session.active_list"] = release = asyncio.Event()
    task = asyncio.create_task(ws_mod.handle_ws(sock))
    sock.inbound.put_nowait(rpc("poll", "session.active_list"))
    assert await settle(lambda: "start:poll" in log)
    sock.inbound.put_nowait(rpc("steer", "session.steer"))
    try:
        assert await settle(lambda: "done:steer" in log), "the active-list poll parked the control lane"
        assert "done:poll" not in log
    finally:
        release.set()
    sock.inbound.put_nowait(None)
    await asyncio.wait_for(task, 30)


@pytest.mark.asyncio
async def test_mutating_methods_stay_serial(rig):
    """mutating-methods-stay-serial: arrival order, never overlapped, however many reads run."""
    sock, gates, log = rig
    gates["process.list"] = poll_release = asyncio.Event()
    gates["prompt.submit"] = release = asyncio.Event()
    task = asyncio.create_task(ws_mod.handle_ws(sock))
    sock.inbound.put_nowait(rpc("poll", "process.list"))
    assert await settle(lambda: "start:poll" in log)
    sock.inbound.put_nowait(rpc(1, "prompt.submit"))
    assert await settle(lambda: "start:1" in log), "a mutating request waited behind a parked poll"
    sock.inbound.put_nowait(rpc(2, "prompt.submit"))
    assert not await settle(lambda: "start:2" in log, timeout=0.5), "a mutating request ran while another was in flight"
    release.set()
    assert await settle(lambda: "done:2" in log)
    poll_release.set()
    sock.inbound.put_nowait(None)
    await asyncio.wait_for(task, 30)
    assert [e for e in log if e.endswith((":1", ":2"))] == ["start:1", "done:1", "start:2", "done:2"]


def _close_line(caplog):
    line = next((rec.getMessage() for rec in reversed(caplog.records)
                 if rec.getMessage().startswith("ws closed peer=")), None)
    assert line is not None, "no ws close line logged"
    return line


def _unanswered(line):
    match = re.search(r"unanswered=(\d+) oldest_wait_s=([0-9.eE+-]+)", line)
    assert match is not None, line
    return int(match.group(1)), float(match.group(2))


@pytest.mark.asyncio
async def test_close_line_reports_unanswered_and_oldest_wait(rig, caplog):
    """close-line-reports-unanswered-and-oldest-wait: sampled at disconnect, not after the drain."""
    caplog.set_level(logging.INFO, logger="tui_gateway.ws")
    sock, gates, log = rig
    gates["prompt.submit"] = release = asyncio.Event()
    task = asyncio.create_task(ws_mod.handle_ws(sock))
    sock.inbound.put_nowait(rpc(1, "prompt.submit"))
    assert await settle(lambda: "start:1" in log)
    sock.inbound.put_nowait(rpc(2, "prompt.submit"))
    assert await settle(lambda: sock.received == 2)
    sock.inbound.put_nowait(None)  # the disconnect is detected while 1 runs and 2 waits
    await asyncio.sleep(0.1)  # let the read loop take its pre-drain snapshot
    release.set()
    await asyncio.wait_for(task, 30)
    assert "done:2" in log  # the drain really did finish both
    unanswered, oldest = _unanswered(_close_line(caplog))
    assert unanswered == 2 and oldest > 0


@pytest.mark.asyncio
async def test_close_line_idle_reads_zero(rig, caplog):
    """close-line-idle-reads-zero: every request answered before the disconnect."""
    caplog.set_level(logging.INFO, logger="tui_gateway.ws")
    sock, _gates, log = rig
    task = asyncio.create_task(ws_mod.handle_ws(sock))
    sock.inbound.put_nowait(rpc(1))
    assert await settle(lambda: "done:1" in log)
    sock.inbound.put_nowait(None)
    await asyncio.wait_for(task, 30)
    assert _unanswered(_close_line(caplog)) == (0, 0.0)


@pytest.mark.asyncio
async def test_close_line_carries_no_request_params(rig, caplog):
    """close-line-carries-no-request-params: counts and identity only, never prompt text."""
    caplog.set_level(logging.INFO, logger="tui_gateway.ws")
    sock, gates, log = rig
    gates["prompt.submit"] = release = asyncio.Event()
    task = asyncio.create_task(ws_mod.handle_ws(sock))
    sock.inbound.put_nowait({"jsonrpc": "2.0", "id": 7, "method": "prompt.submit",
                             "params": {"prompt": "MARKER-prose-must-not-be-logged"}})
    assert await settle(lambda: "start:7" in log)
    sock.inbound.put_nowait(None)
    await asyncio.sleep(0.1)  # let the read loop take its pre-drain snapshot
    release.set()
    await asyncio.wait_for(task, 30)
    line = _close_line(caplog)
    assert "MARKER-prose-must-not-be-logged" not in line
    assert "unanswered=1" in line
