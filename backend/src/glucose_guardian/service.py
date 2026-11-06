"""Runtime entry point with simulated acquisition. No Dexcom access or hardware output."""
import argparse
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from .config import Config, ConfigError, load_config
from .display import LogDisplay
from .runtime import Runtime
from .serial_transport import SerialTransport, open_serial
from .simulation import SCENARIOS, ScenarioProvider
from .transport import AlarmTransport, SimulatedTransport
from .worker import spawn_daemon_thread

log = logging.getLogger("glucose_guardian.service")

EXIT_STOPPED = 0
EXIT_CRASHED = 1
EXIT_CONFIG = 2


def build_transport(config: Config, heartbeat_ok: Callable[[float], bool]) -> AlarmTransport:
    if config.serial is None:
        return SimulatedTransport(heartbeat_ok=heartbeat_ok)
    return SerialTransport(device=config.serial.device,
                           session=int.from_bytes(os.urandom(2), "big"),
                           reconnect_initial_s=config.serial.reconnect_initial_s,
                           reconnect_max_s=config.serial.reconnect_max_s,
                           open_port=open_serial)


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
        transport=build_transport(
            config, lambda t: scenario.segment_at(t - start_s)[1].heartbeat),
        display=LogDisplay(), wall_clock=lambda: datetime.now(UTC), monotonic=time.monotonic,
        acquisition_spawn=spawn_daemon_thread, clock_spawn=spawn_daemon_thread)

    stop = threading.Event()
    handlers = {sig: signal.signal(sig, lambda signum, frame: stop.set())
                for sig in (signal.SIGTERM, signal.SIGINT)}
    log.info("runtime started: scenario=%s transport=%s", config.scenario,
             "simulated" if config.serial is None else "serial")
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
