"""Named background-task registry — the one place periodic loops live.

The IMAP sync, digest schedule, and vector retry loops register here;
Phase 1b (wiki sync/lint) and Phase 4 (RSS) add registrations instead of
new ad-hoc app.state task attributes. For back-compat, every start/stop
mirrors the task to app.state.{name}_task, which healthz and tests read.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, state=None):
        self._state = state
        self._tasks: dict[str, asyncio.Task] = {}

    def _mirror(self, name: str, task: "asyncio.Task | None") -> None:
        if self._state is not None:
            setattr(self._state, f"{name}_task", task)

    def start(self, name: str, coro) -> asyncio.Task:
        self.stop(name)
        task = asyncio.create_task(coro, name=f"tiro-{name}")
        self._tasks[name] = task
        self._mirror(name, task)
        logger.info("Scheduler: started task %r", name)
        return task

    def stop(self, name: str) -> None:
        task = self._tasks.pop(name, None)
        if task and not task.done():
            task.cancel()
        self._mirror(name, None)

    def get(self, name: str) -> "asyncio.Task | None":
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
