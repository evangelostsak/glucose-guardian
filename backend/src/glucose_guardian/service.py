"""Simulated runtime entry point. No Dexcom access, serial port or hardware output."""
import argparse
import logging
import signal
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from .config import ConfigError, load_config
from .display import LogDisplay
from .runtime import Runtime
from .simulation import SCENARIOS, ScenarioProvider
from .transport import SimulatedTransport
from .worker import spawn_daemon_thread

log = logging.getLogger("glucose_guardian.service")

EXIT_STOPPED = 0
EXIT_CRASHED = 1
EXIT_CONFIG = 2


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="python -m glucose_guardian.service")
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as error:
        log.error("%s", error)
        return EXIT_CONFIG

    scenario = SCENARIOS[config.scenario]
    start_s = time.monotonic()

    def elapsed() -> float:
        return time.monotonic() - start_s

    runtime = Runtime(
        policy=config.policy, settings=config.runtime,
        provider=ScenarioProvider(scenario, start_wall=datetime.now(UTC), elapsed=elapsed,
                                  hang=lambda seconds: time.sleep(max(0.0, seconds))),
        clock_check=lambda: scenario.segment_at(elapsed())[1].clock_synchronized,
        transport=SimulatedTransport(
            heartbeat_ok=lambda t: scenario.segment_at(t - start_s)[1].heartbeat),
        display=LogDisplay(), wall_clock=lambda: datetime.now(UTC), monotonic=time.monotonic,
        acquisition_spawn=spawn_daemon_thread, clock_spawn=spawn_daemon_thread)

    stop = threading.Event()
    handlers = {sig: signal.signal(sig, lambda signum, frame: stop.set())
                for sig in (signal.SIGTERM, signal.SIGINT)}
    log.info("simulated runtime started: scenario=%s", config.scenario)
    try:
        runtime.run(stop.wait)
    except Exception:
        log.exception("runtime crashed")
        return EXIT_CRASHED
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
    log.info("runtime stopped after %d ticks", runtime.counts["ticks"])
    return EXIT_STOPPED


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
