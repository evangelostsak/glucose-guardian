"""Runtime entry point: simulated or Dexcom Share acquisition. No hardware output."""
import argparse
import logging
import os
import signal
import ssl
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

from .clock import systemd_clock_synchronized
from .config import Config, ConfigError, load_config
from .credentials import CredentialsError, load_credentials
from .dexcom_share import APPLICATION_ID, SERVERS, SERVICES_PATH
from .display import LogDisplay
from .providers import GlucoseProvider
from .runtime import Runtime
from .serial_transport import SerialTransport, open_serial
from .share_client import (
    MAX_RESPONSE_BYTES,
    DexcomShareProvider,
    ShareServer,
    ShareSession,
    urllib_poster,
)
from .share_state import BOOT_ID, AuthStateStore, read_boot_id
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


def share_server(config: Config) -> ShareServer:
    settings = config.dexcom
    if settings is None:
        raise ConfigError("provider dexcom_share requires a [dexcom] table")
    if settings.server == "simulator":
        return ShareServer(f"http://127.0.0.1:{settings.simulator_port}{SERVICES_PATH}",
                           APPLICATION_ID)
    base_url, application_id = SERVERS[settings.server]
    return ShareServer(base_url, application_id)


def build_share_session(config: Config, credentials_directory: str | None, *,
                        boot_id_path: Path, boottime: Callable[[], float],
                        utc: Callable[[], datetime], monotonic: Callable[[], float],
                        context: ssl.SSLContext) -> ShareSession:
    settings = config.dexcom
    if settings is None:
        raise ConfigError("provider dexcom_share requires a [dexcom] table")
    if not settings.state_file.parent.is_dir():
        raise ConfigError(f"[dexcom] state_file directory {settings.state_file.parent} "
                          "does not exist")
    credentials = load_credentials(credentials_directory)
    store = AuthStateStore(path=settings.state_file, boot_id=read_boot_id(boot_id_path),
                           boottime=boottime, utc=utc,
                           login_min_interval_s=settings.login_min_interval_s,
                           rate_limit_initial_s=settings.rate_limit_initial_s,
                           rate_limit_max_s=settings.rate_limit_max_s)
    post = urllib_poster(context=context,
                         user_agent=f"glucose-guardian/{version('glucose-guardian')}",
                         max_bytes=MAX_RESPONSE_BYTES, monotonic=monotonic)
    return ShareSession(server=share_server(config), credentials=credentials, post=post,
                        store=store, request_timeout_s=settings.request_timeout_s,
                        boottime=boottime)


def production_session(config: Config) -> ShareSession:
    return build_share_session(
        config, os.environ.get("CREDENTIALS_DIRECTORY"), boot_id_path=BOOT_ID,
        boottime=lambda: time.clock_gettime(time.CLOCK_BOOTTIME),
        utc=lambda: datetime.now(UTC), monotonic=time.monotonic,
        context=ssl.create_default_context())


Acquisition = tuple[GlucoseProvider, Callable[[], bool], Callable[[float], bool], str]


def build_acquisition(config: Config, start_s: float) -> Acquisition:
    if config.provider == "dexcom_share" and config.dexcom is not None:
        share = DexcomShareProvider(production_session(config),
                                    max_age_s=config.policy.max_age_s)
        timeout_s = config.runtime.clock_check_timeout_s
        return (share, lambda: systemd_clock_synchronized(subprocess.run, timeout_s),
                lambda t: True, f"provider=dexcom_share server={config.dexcom.server}")
    if config.scenario is None:
        raise ConfigError("provider simulated requires a [simulation] scenario")
    scenario = SCENARIOS[config.scenario]

    def elapsed() -> float:
        return time.monotonic() - start_s

    provider = ScenarioProvider(scenario, start_wall=datetime.now(UTC), elapsed=elapsed,
                                hang=lambda seconds: time.sleep(max(0.0, seconds)))
    return (provider, lambda: scenario.segment_at(elapsed())[1].clock_synchronized,
            lambda t: scenario.segment_at(t - start_s)[1].heartbeat,
            f"scenario={config.scenario}")


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

    start_s = time.monotonic()
    try:
        provider, clock_check, heartbeat_ok, description = build_acquisition(config, start_s)
    except (ConfigError, CredentialsError, OSError) as error:
        log.error("%s", error)
        return EXIT_CONFIG
    runtime = Runtime(
        policy=config.policy, settings=config.runtime, provider=provider,
        clock_check=clock_check, transport=build_transport(config, heartbeat_ok),
        display=LogDisplay(), wall_clock=lambda: datetime.now(UTC), monotonic=time.monotonic,
        acquisition_spawn=spawn_daemon_thread, clock_spawn=spawn_daemon_thread)

    stop = threading.Event()
    handlers = {sig: signal.signal(sig, lambda signum, frame: stop.set())
                for sig in (signal.SIGTERM, signal.SIGINT)}
    log.info("runtime started: %s transport=%s", description,
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
