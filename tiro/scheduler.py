"""Named background-task registry — the one place periodic loops live.

The IMAP sync, digest schedule, and vector retry loops register here;
Phase 1b (wiki sync/lint) and Phase 4 (RSS) add registrations instead of
new ad-hoc app.state task attributes. For back-compat, every start/stop
mirrors the task to app.state.{name}_task, which healthz and tests read.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class PeriodicTask:
    """Shared loop machinery for a background task (M4.0).

    Wraps a single work cycle (`run_once`, an async zero-arg callable) with a
    live-config delay (`next_delay_seconds`, a sync zero-arg callable returning
    float seconds re-read every cycle) into one uniform loop with error
    isolation, exponential loop-level backoff, and introspection — so the imap,
    digest, vector-retry, and (Phase 4) RSS loops all ride one abstraction
    instead of hand-rolling their own `while True: sleep / work / log`.

    Semantics preserved from the pre-M4.0 loops:
    - `next_delay_seconds()` returning `<= 0` ends the loop cleanly (the imap
      "interval 0 = manual only" / digest-disabled stop condition).
    - Loops sleep BEFORE their first work cycle by default (`first_delay=True`),
      matching every existing loop's `while True: sleep(...) ; work` shape. Set
      `first_delay=False` for run-first loops.
    - A `run_once` exception is caught and logged (loop survives), records
      `last_error` + increments `consecutive_failures`, and backs the NEXT sleep
      off by `2 ** min(consecutive_failures, 5)`. A successful cycle resets the
      counter. `asyncio.CancelledError` always propagates (clean shutdown).
    """

    def __init__(
        self,
        name: str,
        run_once: Callable[[], Awaitable[None]],
        next_delay_seconds: Callable[[], float],
        first_delay: bool = True,
    ):
        self.name = name
        self._run_once = run_once
        self._next_delay_seconds = next_delay_seconds
        self._first_delay = first_delay
        self.running = False
        self.last_run_at: str | None = None
        self.last_success_at: str | None = None
        self.last_error: str | None = None
        self.consecutive_failures = 0

    def status(self) -> dict:
        return {
            "name": self.name,
            "running": self.running,
            "last_run_at": self.last_run_at,
            "last_success_at": self.last_success_at,
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
        }

    async def _sleep_next(self) -> bool:
        """Sleep until the next cycle. Returns False if the loop should stop."""
        delay = self._next_delay_seconds()
        if delay <= 0:
            return False
        backoff = 2 ** min(self.consecutive_failures, 5)
        await asyncio.sleep(delay * backoff)
        return True

    async def _run_cycle(self) -> None:
        self.last_run_at = _now_iso()
        try:
            await self._run_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.consecutive_failures += 1
            self.last_error = str(e)
            logger.error(
                "Periodic task %r failed (%d consecutive): %s",
                self.name, self.consecutive_failures, e,
            )
        else:
            self.last_success_at = _now_iso()
            self.last_error = None
            self.consecutive_failures = 0

    async def loop(self) -> None:
        """The coroutine to register with the scheduler."""
        self.running = True
        try:
            while True:
                if self._first_delay:
                    if not await self._sleep_next():
                        return
                    await self._run_cycle()
                else:
                    await self._run_cycle()
                    if not await self._sleep_next():
                        return
        finally:
            self.running = False


class Scheduler:
    def __init__(self, state=None):
        self._state = state
        self._tasks: dict[str, asyncio.Task] = {}
        self._periodic: dict[str, PeriodicTask] = {}

    def _mirror(self, name: str, task: asyncio.Task | None) -> None:
        if self._state is not None:
            setattr(self._state, f"{name}_task", task)

    def start(self, name: str, coro) -> asyncio.Task:
        self.stop(name)
        task = asyncio.create_task(coro, name=f"tiro-{name}")
        self._tasks[name] = task
        self._mirror(name, task)
        logger.info("Scheduler: started task %r", name)
        return task

    def start_periodic(self, name: str, periodic_task: "PeriodicTask") -> asyncio.Task:
        """Register + start a PeriodicTask, retaining the instance so
        periodic_status() can introspect it. Mirrors app.state.{name}_task
        exactly like start()."""
        self._periodic[name] = periodic_task
        return self.start(name, periodic_task.loop())

    def periodic_status(self) -> dict[str, dict]:
        """Status of every registered PeriodicTask (for /healthz detail)."""
        return {name: pt.status() for name, pt in self._periodic.items()}

    def stop(self, name: str) -> None:
        task = self._tasks.pop(name, None)
        if task and not task.done():
            task.cancel()
        self._mirror(name, None)

    async def stop_and_wait(self, name: str) -> None:
        """Cancel AND await the named task's teardown before returning.

        Use this before start() when replacing a loop whose body does
        non-interruptible work (asyncio.to_thread) — cancel() alone lets the
        old body finish in the background and briefly overlap its successor
        (e.g. two concurrent IMAP sessions racing the SELECT-before-INSERT
        email dedup). Lifespan shutdown doesn't need it (shutdown() already
        awaits); fire-and-forget stop() remains for non-replacement stops.
        """
        task = self._tasks.pop(name, None)
        self._mirror(name, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def get(self, name: str) -> asyncio.Task | None:
        return self._tasks.get(name)

    async def shutdown(self) -> None:
        for name in list(self._tasks):
            task = self._tasks.pop(name)
            self._mirror(name, None)
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
