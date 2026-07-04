import asyncio

from tiro.scheduler import Scheduler


class FakeState:
    pass


async def _sleepy():
    await asyncio.sleep(3600)


async def test_start_stop_and_mirror():
    state = FakeState()
    sched = Scheduler(state)
    task = sched.start("imap", _sleepy())
    assert sched.get("imap") is task
    assert state.imap_task is task
    sched.stop("imap")
    await asyncio.sleep(0)
    assert task.cancelled() or task.done()
    assert state.imap_task is None


async def test_start_replaces_existing():
    state = FakeState()
    sched = Scheduler(state)
    t1 = sched.start("digest", _sleepy())
    t2 = sched.start("digest", _sleepy())
    await asyncio.sleep(0)
    assert t1.cancelled() or t1.done()
    assert sched.get("digest") is t2
    await sched.shutdown()
    assert sched.get("digest") is None
