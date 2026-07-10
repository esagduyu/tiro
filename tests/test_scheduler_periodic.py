"""M4.0: PeriodicTask — shared loop machinery for background tasks.

Covers the interface Task 2's RSS loop (and the refactored imap/digest/vector
loops) ride: repeated run_once at the next_delay cadence, failure isolation +
backoff, success reset, zero-delay stop, cancellation, and start_periodic's
app.state mirroring / periodic_status introspection.
"""

import asyncio

from tiro.scheduler import PeriodicTask, Scheduler


class FakeState:
    pass


async def test_run_once_called_repeatedly_at_delay():
    """(a) run_once fires repeatedly; next_delay_seconds sets the cadence."""
    calls = []

    async def run_once():
        calls.append(1)

    task = PeriodicTask("t", run_once, lambda: 0.001)
    handle = asyncio.create_task(task.loop())
    try:
        # Wait until we've observed several cycles.
        for _ in range(200):
            if len(calls) >= 3:
                break
            await asyncio.sleep(0.005)
        assert len(calls) >= 3
        assert task.status()["running"] is True
    finally:
        handle.cancel()
        try:
            await handle
        except asyncio.CancelledError:
            pass


async def test_failure_isolated_and_backed_off(monkeypatch):
    """(b) run_once raising: loop survives, last_error set, consecutive_failures
    increments, and the observed sleep is backed off by 2**failures."""
    slept = []

    real_sleep = asyncio.sleep

    async def fake_sleep(secs, *a, **k):
        slept.append(secs)
        await real_sleep(0)  # yield without actually waiting

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def run_once():
        raise RuntimeError("boom")

    task = PeriodicTask("t", run_once, lambda: 10.0)
    handle = asyncio.create_task(task.loop())
    try:
        for _ in range(500):
            if len(slept) >= 3:
                break
            await real_sleep(0)
        handle.cancel()
        try:
            await handle
        except asyncio.CancelledError:
            pass

        # Base delay 10; after each failure the next sleep doubles.
        assert slept[0] == 10.0            # cf=0 -> 10 * 2**0
        assert slept[1] == 20.0            # cf=1 -> 10 * 2**1
        assert slept[2] == 40.0            # cf=2 -> 10 * 2**2
        assert task.status()["consecutive_failures"] >= 2
        assert "boom" in task.status()["last_error"]
    finally:
        if not handle.done():
            handle.cancel()


async def test_backoff_caps_at_five(monkeypatch):
    """Backoff exponent caps at 5 (2**5 = 32x)."""
    slept = []
    real_sleep = asyncio.sleep

    async def fake_sleep(secs, *a, **k):
        slept.append(secs)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def run_once():
        raise RuntimeError("boom")

    task = PeriodicTask("t", run_once, lambda: 1.0)
    handle = asyncio.create_task(task.loop())
    try:
        for _ in range(2000):
            if len(slept) >= 8:
                break
            await real_sleep(0)
    finally:
        handle.cancel()
        try:
            await handle
        except asyncio.CancelledError:
            pass
    # Once cf >= 5 the multiplier stops growing at 32.
    assert slept[6] == 32.0
    assert slept[7] == 32.0


async def test_success_resets_failure_counter(monkeypatch):
    """(c) a successful cycle resets consecutive_failures and last_error."""
    slept = []
    real_sleep = asyncio.sleep

    async def fake_sleep(secs, *a, **k):
        slept.append(secs)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    outcomes = iter([RuntimeError("boom"), RuntimeError("boom"), None, None])

    async def run_once():
        exc = next(outcomes, None)
        if exc is not None:
            raise exc

    seen = []

    task = PeriodicTask("t", run_once, lambda: 5.0)

    async def watcher():
        for _ in range(2000):
            seen.append(task.status()["consecutive_failures"])
            await real_sleep(0)

    handle = asyncio.create_task(task.loop())
    w = asyncio.create_task(watcher())
    try:
        for _ in range(2000):
            if len(slept) >= 4:
                break
            await real_sleep(0)
    finally:
        handle.cancel()
        w.cancel()
        for h in (handle, w):
            try:
                await h
            except asyncio.CancelledError:
                pass
    # It climbed to 2 then reset to 0 after a success.
    assert 2 in seen
    assert task.status()["consecutive_failures"] == 0
    assert task.status()["last_error"] is None


async def test_zero_delay_exits_cleanly():
    """(d) next_delay_seconds() <= 0 ends the loop without running work."""
    calls = []

    async def run_once():
        calls.append(1)

    task = PeriodicTask("t", run_once, lambda: 0.0)
    await asyncio.wait_for(task.loop(), timeout=1.0)  # returns, no hang
    assert calls == []
    assert task.status()["running"] is False


async def test_negative_delay_after_some_cycles(monkeypatch):
    """A next_delay that flips <= 0 mid-life stops the loop."""
    real_sleep = asyncio.sleep

    async def fake_sleep(secs, *a, **k):
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    ran = []
    delays = iter([1.0, 1.0, -1.0])

    async def run_once():
        ran.append(1)

    def next_delay():
        return next(delays, -1.0)

    task = PeriodicTask("t", run_once, next_delay)
    await asyncio.wait_for(task.loop(), timeout=1.0)
    # Two positive delays -> two work cycles, then stop.
    assert len(ran) == 2
    assert task.status()["running"] is False


async def test_cancellation_propagates_and_clears_running():
    """(e) cancelling the loop raises CancelledError and leaves running False."""
    async def run_once():
        pass

    task = PeriodicTask("t", run_once, lambda: 3600.0)
    handle = asyncio.create_task(task.loop())
    await asyncio.sleep(0)  # let it start and enter the sleep
    handle.cancel()
    cancelled = False
    try:
        await handle
    except asyncio.CancelledError:
        cancelled = True
    assert cancelled
    assert task.status()["running"] is False


async def test_run_first_mode_runs_before_sleeping():
    """first_delay=False runs one cycle before the first sleep."""
    calls = []

    async def run_once():
        calls.append(1)

    task = PeriodicTask("t", run_once, lambda: 3600.0, first_delay=False)
    handle = asyncio.create_task(task.loop())
    try:
        for _ in range(200):
            if calls:
                break
            await asyncio.sleep(0.005)
        assert calls == [1]  # ran immediately, now parked in the long sleep
    finally:
        handle.cancel()
        try:
            await handle
        except asyncio.CancelledError:
            pass


async def test_start_periodic_mirrors_state_like_start():
    """(f) start_periodic mirrors app.state.{name}_task and retains the instance."""
    state = FakeState()
    sched = Scheduler(state)

    async def run_once():
        pass

    task = PeriodicTask("imap", run_once, lambda: 3600.0)
    handle = sched.start_periodic("imap", task)
    assert sched.get("imap") is handle
    assert state.imap_task is handle
    assert sched.periodic_status()["imap"]["name"] == "imap"

    await sched.stop_and_wait("imap")
    assert state.imap_task is None
    assert sched.get("imap") is None


async def test_periodic_status_reports_all_registered():
    """periodic_status aggregates every registered PeriodicTask's status()."""
    sched = Scheduler(FakeState())

    async def noop():
        pass

    sched.start_periodic("a", PeriodicTask("a", noop, lambda: 3600.0))
    sched.start_periodic("b", PeriodicTask("b", noop, lambda: 3600.0))
    status = sched.periodic_status()
    assert set(status) == {"a", "b"}
    assert set(status["a"]) == {
        "name", "running", "last_run_at", "last_success_at",
        "last_error", "consecutive_failures",
    }
    await sched.shutdown()
