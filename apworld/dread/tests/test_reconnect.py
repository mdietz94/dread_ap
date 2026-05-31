"""Reconnect-path hardening tests for the REAL DreadContext.

These drive the actual :class:`DreadContext` (no mocks but the AP ``send_msgs``
sink) against the loopback :class:`FakeDreadGame` exlaunch server and prove the
``/dread_connect`` / ``reconnect_switch`` contract:

  * a manual reconnect while connected fully REPLACES the connection — the old
    executor is closed and exactly one executor + one poll task remain,
  * repeated reconnect cycles leak no tasks or sockets (prior read / keep-alive
    tasks are done, the executor reference is fresh each time),
  * a dial against a refused port fails gracefully and the supervisor retries
    (succeeding once the server comes up), with the backoff reset on a manual
    retry,
  * ``/dread_connect host:port`` re-points ``switch_host`` / ``switch_port`` and
    bare ``/dread_connect`` reconnects to the existing target.

Run with:  python -m pytest apworld/dread/tests/test_reconnect.py -v
"""
from __future__ import annotations

import asyncio
import sys
import unittest.mock
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from dread.client.context import (  # noqa: E402
    DreadContext,
    DreadClientCommandProcessor,
    _SWITCH_BACKOFF_START,
)
from dread.client.datapackage import DataPackage  # noqa: E402
from dread.client.state import BridgeState  # noqa: E402
from dread.tests.fakeswitch import FakeDreadGame  # noqa: E402

DATA = ROOT / "data"


# ---- helpers --------------------------------------------------------------

async def _await_until(predicate, timeout: float = 2.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


def _make_ctx(port: int, state: BridgeState | None = None) -> DreadContext:
    state = state or BridgeState()
    dp = DataPackage(apworld_data_dir=DATA)
    ctx = DreadContext(None, None, state=state, datapackage=dp,
                       switch_host="127.0.0.1", switch_port=port)
    ctx.send_msgs = unittest.mock.AsyncMock()  # type: ignore[method-assign]
    return ctx


async def _drain_poll_task(ctx: DreadContext) -> None:
    """Cancel/await the auto-started poll loop so the test controls timing."""
    if ctx._poll_task is not None:
        ctx._poll_task.cancel()
        try:
            await ctx._poll_task
        except asyncio.CancelledError:
            pass
        ctx._poll_task = None


# ---- tests ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_reconnect_replaces_connection_cleanly():
    """A manual reconnect while connected closes the old executor and leaves
    exactly one live executor + one poll task. No two executors / poll loops
    ever coexist."""
    fake = FakeDreadGame()
    await fake.start()
    try:
        ctx = _make_ctx(fake.port)
        await ctx.connect_switch()
        assert ctx.executor is not None and ctx._bootstrapped
        old_exec = ctx.executor
        old_poll = ctx._poll_task
        assert old_poll is not None
        old_read = old_exec._read_task
        old_keepalive = old_exec._keep_alive_task

        # Reconnect while live.
        await ctx.reconnect_switch()

        assert ctx.executor is not None
        assert ctx.executor is not old_exec, "executor not replaced"
        assert ctx._bootstrapped
        # The old executor is fully torn down.
        assert old_exec._writer is None
        assert old_exec._read_task is None and old_exec._keep_alive_task is None
        assert old_read is not None and old_read.done()
        assert old_keepalive is not None and old_keepalive.done()
        # Exactly one poll task, and it is the fresh one.
        assert ctx._poll_task is not None
        assert ctx._poll_task is not old_poll
        assert old_poll.done(), "old poll task left running"
        assert not ctx._poll_task.done()

        await _drain_poll_task(ctx)
        await ctx.executor.close()
    finally:
        await fake.stop()


@pytest.mark.asyncio
async def test_repeated_reconnects_do_not_leak_tasks_or_sockets():
    """Several reconnect cycles in a row: each leaves a fresh executor, and the
    prior read / keep-alive / poll tasks are all done (cancelled), so nothing
    accumulates."""
    fake = FakeDreadGame()
    await fake.start()
    try:
        ctx = _make_ctx(fake.port)
        await ctx.connect_switch()
        assert ctx.executor is not None

        seen_execs = []
        prior_tasks = []
        for _ in range(5):
            prev = ctx.executor
            assert prev is not None
            seen_execs.append(id(prev))
            prior_tasks.append(prev._read_task)
            prior_tasks.append(prev._keep_alive_task)
            prior_poll = ctx._poll_task

            await ctx.reconnect_switch()

            assert ctx.executor is not None
            assert ctx.executor is not prev
            assert ctx._bootstrapped
            # Prior connection fully unwound.
            assert prev._writer is None
            if prior_poll is not None:
                assert prior_poll.done()

        # Every executor instance was distinct (no reuse / no duplicates).
        assert len(set(seen_execs)) == len(seen_execs)
        # All prior read / keep-alive tasks finished.
        for t in prior_tasks:
            if t is not None:
                assert t.done(), "a prior background task is still alive"
        # Exactly one live poll task remains.
        assert ctx._poll_task is not None and not ctx._poll_task.done()

        await _drain_poll_task(ctx)
        await ctx.executor.close()
    finally:
        await fake.stop()


@pytest.mark.asyncio
async def test_dial_failure_is_graceful_then_supervisor_recovers():
    """Dialing a port with nothing listening must fail cleanly (no crash, no
    dangling executor); once the server comes up the supervisor's next tick
    connects. Also exercises the live supervisor loop end-to-end."""
    # Reserve a port, then close it so the dial is refused.
    probe = FakeDreadGame()
    await probe.start()
    dead_port = probe.port
    await probe.stop()

    ctx = _make_ctx(dead_port)
    # First dial: refused. Must not raise, must leave executor None.
    await ctx.connect_switch()
    assert ctx.executor is None
    assert not ctx._bootstrapped
    snap = ctx.state.snapshot()
    assert "error" in (snap.get("switch_conn") or "").lower()

    # Bring a real server up on a NEW port and re-point + reconnect.
    fake = FakeDreadGame()
    await fake.start()
    try:
        # Drive the real supervisor: it should keep retrying with backoff and
        # connect once we point it at the live server and wake it.
        sup = asyncio.create_task(ctx._switch_supervisor())
        try:
            ctx.switch_host = "127.0.0.1"
            ctx.switch_port = fake.port
            ctx.request_redial()
            assert await _await_until(lambda: ctx.executor is not None, timeout=3.0)
            assert ctx._bootstrapped
        finally:
            ctx.exit_event.set()
            ctx.request_redial()
            sup.cancel()
            try:
                await sup
            except asyncio.CancelledError:
                pass
        await _drain_poll_task(ctx)
        await ctx.executor.close()
    finally:
        await fake.stop()


@pytest.mark.asyncio
async def test_request_redial_resets_backoff_and_wakes():
    """A user-initiated retry must reset the exponential backoff to its start
    value (so the next auto-redial is prompt) and set the redial event (so a
    sleeping supervisor wakes immediately)."""
    fake = FakeDreadGame()
    await fake.start()
    try:
        ctx = _make_ctx(fake.port)
        # Simulate the supervisor having climbed to a high backoff after a run
        # of failed dials.
        ctx._switch_backoff = 16.0
        ctx._redial_event.clear()

        ctx.request_redial()

        assert ctx._switch_backoff == _SWITCH_BACKOFF_START, (
            "request_redial must reset the supervisor backoff to its start "
            f"value, got {ctx._switch_backoff}"
        )
        assert ctx._redial_event.is_set(), "redial event must be set to wake the supervisor"
    finally:
        await fake.stop()


@pytest.mark.asyncio
async def test_dread_connect_repoints_host_and_port():
    """``/dread_connect host:port`` updates switch_host + switch_port before
    reconnecting; the actual dial goes to the new target."""
    fake = FakeDreadGame()
    await fake.start()
    try:
        ctx = _make_ctx(fake.port)
        # Start pointed somewhere else entirely.
        ctx.switch_host = "10.0.0.99"
        ctx.switch_port = 1234

        proc = DreadClientCommandProcessor(ctx)
        proc.output = lambda _msg: None  # swallow CLI echo

        proc._cmd_dread_connect(f"127.0.0.1:{fake.port}")
        # The command schedules reconnect_switch via ensure_future.
        assert await _await_until(lambda: ctx.executor is not None, timeout=3.0)

        assert ctx.switch_host == "127.0.0.1"
        assert ctx.switch_port == fake.port
        assert ctx._bootstrapped

        await _drain_poll_task(ctx)
        await ctx.executor.close()
    finally:
        await fake.stop()


@pytest.mark.asyncio
async def test_dread_connect_bare_reconnects_existing_target():
    """Bare ``/dread_connect`` (no arg) re-dials the current target without
    changing it."""
    fake = FakeDreadGame()
    await fake.start()
    try:
        ctx = _make_ctx(fake.port)
        await ctx.connect_switch()
        assert ctx.executor is not None
        old_exec = ctx.executor
        host_before, port_before = ctx.switch_host, ctx.switch_port

        proc = DreadClientCommandProcessor(ctx)
        proc.output = lambda _msg: None

        proc._cmd_dread_connect("")  # bare
        # Wait for the replacement to land.
        assert await _await_until(
            lambda: ctx.executor is not None and ctx.executor is not old_exec,
            timeout=3.0,
        )

        assert ctx.switch_host == host_before
        assert ctx.switch_port == port_before
        assert ctx._bootstrapped
        assert old_exec._writer is None  # old one torn down

        await _drain_poll_task(ctx)
        await ctx.executor.close()
    finally:
        await fake.stop()
