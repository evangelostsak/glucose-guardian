import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"


def open_pipes() -> set[tuple[int, str]]:
    found = set()
    for name in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{name}")
        except FileNotFoundError:
            continue
        if target.startswith("pipe:"):
            found.add((int(name), target))
    return found


class Watched:
    def __init__(self, *args: str, env: dict[str, str] | None = None) -> None:
        self.process = subprocess.Popen(
            [sys.executable, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True,
            env={**os.environ, "PYTHONPATH": str(SRC), **(env or {})})
        self.lines: queue.Queue[str] = queue.Queue()
        self.seen: list[str] = []
        self.reader = threading.Thread(target=self.read, daemon=True)
        self.reader.start()

    def read(self) -> None:
        for line in self.process.stdout:
            self.lines.put(line)

    def expect(self, text: str, timeout_s: float) -> float:
        deadline = time.monotonic() + timeout_s
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                line = self.lines.get(timeout=remaining)
            except queue.Empty:
                break
            self.seen.append(line)
            if text in line:
                return time.monotonic()
        raise AssertionError(f"{text!r} not seen within {timeout_s} s")

    def finish(self) -> str:
        self.process.stdin.close()
        self.process.wait(10)
        self.reader.join(10)
        while not self.lines.empty():
            self.seen.append(self.lines.get_nowait())
        return "".join(self.seen)

    def stop(self, sig: signal.Signals) -> str:
        self.process.send_signal(sig)
        return self.finish()

    def kill(self) -> None:
        self.process.kill()
        self.process.wait(10)
        self.reader.join(10)
        self.process.stdin.close()
        self.process.stdout.close()
