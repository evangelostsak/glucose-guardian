import fcntl
import os
import signal
import subprocess
import threading
import time

import pytest
from processes import Watched, open_pipes

from glucose_guardian.shutdown import SIGNALS, StopRequest, stop_on_signals

EVENT_HANDLER_INSIDE_EVENT_LOCK = """
import signal, sys, threading
stop = threading.Event()
signal.signal(signal.SIGTERM, lambda signum, frame: stop.set())
with stop._cond:
    print("holding the event lock", flush=True)
    signal.raise_signal(signal.SIGTERM)
print("handler returned", flush=True)
"""

TIGHT_WAIT_LOOP = """
import sys
from glucose_guardian.shutdown import stop_on_signals
with stop_on_signals() as stop:
    print("ready", flush=True)
    while not stop.wait(0):
        pass
    print("stopped", flush=True)
    sys.stdin.read()
"""


def new_pipe_ends(before: set[tuple[int, str]]) -> list[int]:
    return sorted(fd for fd, _ in open_pipes() - before)


def test_wait_returns_false_when_the_timeout_passes_without_a_request():
    stop = StopRequest()
    try:
        started = time.monotonic()
        assert stop.wait(0.05) is False
        assert time.monotonic() - started >= 0.049
        assert stop.wait(0) is False
        assert stop.wait(-1) is False
    finally:
        stop.close()


def test_a_request_makes_every_later_wait_return_true_at_once():
    stop = StopRequest()
    try:
        stop.request(signal.SIGTERM, None)
        for _ in range(3):
            started = time.monotonic()
            assert stop.wait(30) is True
            assert time.monotonic() - started < 5
    finally:
        stop.close()


def test_the_pipe_is_non_blocking_close_on_exec_and_closed_once():
    before = open_pipes()
    stop = StopRequest()
    ends = new_pipe_ends(before)
    assert len(ends) == 2
    for fd in ends:
        assert fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_NONBLOCK
        assert fcntl.fcntl(fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC
    stop.close()
    assert open_pipes() == before
    stop.close()
    stop.request(signal.SIGTERM, None)
    assert stop.wait(0) is False


def test_more_requests_than_the_pipe_holds_never_block_or_raise():
    before = open_pipes()
    stop = StopRequest()
    try:
        capacity = fcntl.fcntl(new_pipe_ends(before)[0], fcntl.F_GETPIPE_SZ)
        for _ in range(capacity + 1024):
            stop.request(signal.SIGINT, None)
        assert stop.wait(0) is True
    finally:
        stop.close()


def test_handlers_are_installed_then_restored_and_the_pipe_closed():
    previous = {sig: signal.getsignal(sig) for sig in SIGNALS}
    before = open_pipes()
    with stop_on_signals() as stop:
        assert all(signal.getsignal(sig) == stop.request for sig in SIGNALS)
        assert len(new_pipe_ends(before)) == 2
    assert {sig: signal.getsignal(sig) for sig in SIGNALS} == previous
    assert open_pipes() == before


def test_handlers_are_restored_and_the_pipe_closed_when_the_body_raises():
    previous = {sig: signal.getsignal(sig) for sig in SIGNALS}
    before = open_pipes()
    with pytest.raises(RuntimeError, match="synthetic"), stop_on_signals():
        raise RuntimeError("synthetic")
    assert {sig: signal.getsignal(sig) for sig in SIGNALS} == previous
    assert open_pipes() == before


@pytest.mark.parametrize("sig", SIGNALS)
def test_a_signal_during_an_active_wait_ends_it_promptly(sig):
    with stop_on_signals() as stop:
        timer = threading.Timer(0.2, signal.pthread_kill, (threading.main_thread().ident, sig))
        started = time.monotonic()
        timer.start()
        try:
            assert stop.wait(30) is True
        finally:
            timer.cancel()
            timer.join(5)
        assert time.monotonic() - started < 10


def test_a_burst_of_both_signals_is_absorbed_while_installed():
    with stop_on_signals() as stop:
        for index in range(1000):
            signal.raise_signal(SIGNALS[index % 2])
        assert stop.wait(0) is True


def test_the_replaced_event_handler_deadlocks_inside_the_event_lock():
    child = Watched("-c", EVENT_HANDLER_INSIDE_EVENT_LOCK)
    try:
        child.expect("holding the event lock", 10)
        with pytest.raises(subprocess.TimeoutExpired):
            child.process.wait(2)
        assert child.lines.empty()
    finally:
        child.kill()


def test_a_tight_wait_loop_always_stops_on_either_signal():
    for trial in range(40):
        child = Watched("-c", TIGHT_WAIT_LOOP)
        try:
            child.expect("ready", 10)
            time.sleep((trial % 10) * 0.003)
            child.process.send_signal(SIGNALS[trial % 2])
            child.expect("stopped", 10)
            child.finish()
            assert child.process.returncode == 0
        finally:
            child.kill()


def test_a_rapid_burst_of_signals_to_a_waiting_process_stops_it_once():
    child = Watched("-c", TIGHT_WAIT_LOOP)
    try:
        child.expect("ready", 10)
        for index in range(200):
            child.process.send_signal(SIGNALS[index % 2])
        child.expect("stopped", 10)
        assert child.finish().count("stopped") == 1
        assert child.process.returncode == 0
    finally:
        child.kill()
