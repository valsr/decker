"""A small worker pool that grows by one thread for each worker blocked waiting on a nested call."""

import threading
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

WORKERS = 4


class ElasticPool:
    """Runs submitted functions on WORKERS threads, plus one more per worker inside ``blocking()``."""

    def __init__(self, name: str = "decker-sdk") -> None:
        self._name: str = name
        self._cond: threading.Condition = threading.Condition()
        self._tasks: deque[tuple[Callable[..., Any], tuple[Any, ...]]] = deque()
        self._threads: int = 0
        self._idle: int = 0
        self._blocked: int = 0
        self._stopped: bool = False
        self._local: threading.local = threading.local()

    def submit(self, fn: Callable[..., Any], *args: Any) -> None:
        """Queue fn(*args); dropped silently after shutdown."""
        with self._cond:
            if self._stopped:
                return
            self._tasks.append((fn, args))
            self._wake()

    @contextmanager
    def blocking(self) -> Iterator[None]:
        """While a pool worker waits inside, the pool may run one extra thread; a no-op elsewhere."""
        if not getattr(self._local, "worker", False):
            yield
            return
        with self._cond:
            self._blocked += 1
            self._wake()
        try:
            yield
        finally:
            with self._cond:
                self._blocked -= 1

    def shutdown(self) -> None:
        """Drop queued work and let idle workers exit; running work is not interrupted."""
        with self._cond:
            self._stopped = True
            self._tasks.clear()
            self._cond.notify_all()

    def _wake(self) -> None:
        """Make sure a queued task has a thread (caller holds the lock)."""
        if self._idle >= len(self._tasks):
            self._cond.notify()
        elif self._threads - self._blocked < WORKERS:
            self._threads += 1
            threading.Thread(target=self._work, name=self._name, daemon=True).start()

    def _next(self) -> tuple[Callable[..., Any], tuple[Any, ...]] | None:
        with self._cond:
            if self._threads - self._blocked > WORKERS:
                self._threads -= 1
                return None  # surplus after blocked workers resumed
            while not self._tasks:
                if self._stopped:
                    self._threads -= 1
                    return None
                self._idle += 1
                self._cond.wait()
                self._idle -= 1
                if self._stopped:
                    self._threads -= 1
                    return None
            return self._tasks.popleft()

    def _work(self) -> None:
        self._local.worker = True
        while (task := self._next()) is not None:
            fn, args = task
            try:
                fn(*args)
            except Exception:  # pylint: disable=broad-exception-caught
                pass  # callers report their own failures
