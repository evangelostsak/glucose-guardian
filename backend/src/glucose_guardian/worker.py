"""Bounded background work: one job in flight at most, abandoned after a hard timeout."""
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")
Spawn = Callable[[Callable[[], None]], None]


def spawn_daemon_thread(job: Callable[[], None]) -> None:
    threading.Thread(target=job, daemon=True).start()


def run_inline(job: Callable[[], None]) -> None:
    job()


@dataclass(frozen=True)
class Done(Generic[T]):
    value: T


@dataclass(frozen=True)
class Failed:
    error: str


@dataclass(frozen=True)
class TimedOut:
    pass


class _Attempt(Generic[T]):
    def __init__(self, started_s: float) -> None:
        self.started_s = started_s
        self.finished = threading.Event()
        self.result: Done[T] | Failed | None = None
        self.timed_out = False


class BoundedWorker(Generic[T]):
    def __init__(self, *, interval_s: float, timeout_s: float, spawn: Spawn) -> None:
        self._interval_s = interval_s
        self._timeout_s = timeout_s
        self._spawn = spawn
        self._attempt: _Attempt[T] | None = None
        self._next_start_s: float | None = None
        self.attempts = 0

    def poll(self, monotonic_s: float,
             job: Callable[[], T]) -> Done[T] | Failed | TimedOut | None:
        outcome = self._collect(monotonic_s)
        due = self._next_start_s is None or monotonic_s >= self._next_start_s
        if self._attempt is None and due:
            self._start(monotonic_s, job)
            if outcome is None:
                outcome = self._collect(monotonic_s)
        return outcome

    def _collect(self, monotonic_s: float) -> Done[T] | Failed | TimedOut | None:
        attempt = self._attempt
        if attempt is None:
            return None
        if attempt.finished.is_set():
            self._attempt = None
            return None if attempt.timed_out else attempt.result
        if not attempt.timed_out and monotonic_s - attempt.started_s >= self._timeout_s:
            attempt.timed_out = True
            return TimedOut()
        return None

    def _start(self, monotonic_s: float, job: Callable[[], T]) -> None:
        attempt: _Attempt[T] = _Attempt(monotonic_s)

        def run() -> None:
            try:
                attempt.result = Done(job())
            except Exception as error:
                attempt.result = Failed(type(error).__name__)
            finally:
                attempt.finished.set()

        self._attempt = attempt
        self._next_start_s = monotonic_s + self._interval_s
        self.attempts += 1
        try:
            self._spawn(run)
        except Exception as error:
            attempt.result = Failed(type(error).__name__)
            attempt.finished.set()
