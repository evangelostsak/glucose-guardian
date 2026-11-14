"""Signal-safe stop request: SIGTERM and SIGINT write one byte to a non-blocking self-pipe."""
import contextlib
import os
import select
import signal
from collections.abc import Iterator
from types import FrameType

SIGNALS = (signal.SIGTERM, signal.SIGINT)


class StopRequest:
    def __init__(self) -> None:
        self._read, self._write = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
        self._poller = select.poll()
        self._poller.register(self._read, select.POLLIN)

    def request(self, signum: int, frame: FrameType | None) -> None:
        try:
            os.write(self._write, b"\0")
        except OSError:
            pass

    def wait(self, timeout_s: float) -> bool:
        return bool(self._poller.poll(max(timeout_s, 0.0) * 1000))

    def close(self) -> None:
        if self._read < 0:
            return
        self._poller.unregister(self._read)
        os.close(self._read)
        os.close(self._write)
        self._read = self._write = -1


@contextlib.contextmanager
def stop_on_signals() -> Iterator[StopRequest]:
    with contextlib.ExitStack() as stack:
        stop = StopRequest()
        stack.callback(stop.close)
        for sig in SIGNALS:
            stack.callback(signal.signal, sig, signal.signal(sig, stop.request))
        yield stop
