import threading

from glucose_guardian.worker import BoundedWorker, Done, Failed, TimedOut, run_inline


def test_inline_job_runs_once_per_interval():
    worker: BoundedWorker[int] = BoundedWorker(interval_s=60, timeout_s=10, spawn=run_inline)
    assert worker.poll(0, lambda: 7) == Done(7)
    assert [worker.poll(t, lambda: 8) for t in (1, 30, 59)] == [None, None, None]
    assert worker.poll(60, lambda: 9) == Done(9)
    assert worker.attempts == 2


def test_job_exception_becomes_failure():
    def boom() -> int:
        raise ValueError("synthetic")

    worker: BoundedWorker[int] = BoundedWorker(interval_s=60, timeout_s=10, spawn=run_inline)
    assert worker.poll(0, boom) == Failed("ValueError")


def test_spawn_failure_becomes_failure():
    def refuse(job):
        raise RuntimeError("can't start new thread")

    worker: BoundedWorker[int] = BoundedWorker(interval_s=60, timeout_s=10, spawn=refuse)
    assert worker.poll(0, lambda: 1) == Failed("RuntimeError")
    assert worker.poll(60, lambda: 1) == Failed("RuntimeError")


def test_timeout_blocks_new_jobs_and_discards_late_result():
    held = []
    worker: BoundedWorker[int] = BoundedWorker(interval_s=60, timeout_s=10, spawn=held.append)
    assert worker.poll(0, lambda: 1) is None
    assert worker.poll(9.9, lambda: 1) is None
    assert worker.poll(10, lambda: 1) == TimedOut()
    assert [worker.poll(t, lambda: 1) for t in (11, 60, 500)] == [None, None, None]
    assert worker.attempts == 1
    held.pop()()
    assert worker.poll(501, lambda: 2) is None
    assert worker.attempts == 2
    held.pop()()
    assert worker.poll(502, lambda: 3) == Done(2)


def test_hung_thread_never_accumulates_threads():
    release = threading.Event()
    threads: list[threading.Thread] = []

    def spawn(job):
        thread = threading.Thread(target=job, daemon=True)
        threads.append(thread)
        thread.start()

    worker: BoundedWorker[bool] = BoundedWorker(interval_s=1, timeout_s=0.5, spawn=spawn)
    outcomes = [worker.poll(float(t), release.wait) for t in range(1000)]
    assert len(threads) == 1 and worker.attempts == 1 and threads[0].is_alive()
    assert outcomes.count(TimedOut()) == 1
    release.set()
    threads[0].join(5)
    later = [worker.poll(1000.0, lambda: True)]
    threads[1].join(5)
    later.append(worker.poll(1000.5, lambda: True))
    assert [outcome for outcome in later if outcome is not None] == [Done(True)]
    assert len(threads) == 2
