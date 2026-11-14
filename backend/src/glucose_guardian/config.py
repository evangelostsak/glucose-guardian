"""Strict TOML configuration loading. Every value is required except fail-safe flags."""
import tomllib
from dataclasses import dataclass, fields
from math import isfinite
from pathlib import Path
from typing import Any

from .alarms import Policy
from .dexcom_share import MAX_REQUESTS_PER_FETCH, SERVERS
from .simulation import SCENARIOS

PROVIDERS = frozenset({"simulated", "dexcom_share"})
TRANSPORTS = frozenset({"simulated", "serial"})
DISPLAY_KINDS = frozenset({"log", "snapshot"})
DISPLAY_PATHS = ("snapshot_file", "framebuffer", "font_file")
SHARE_SERVERS = frozenset(SERVERS) | {"simulator"}
MIN_LOGIN_INTERVAL_S = 300.0


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class RuntimeSettings:
    tick_s: float
    fetch_interval_s: float
    fetch_timeout_s: float
    clock_check_interval_s: float
    clock_check_timeout_s: float


@dataclass(frozen=True)
class SerialSettings:
    device: str
    reconnect_initial_s: float
    reconnect_max_s: float


@dataclass(frozen=True)
class DexcomSettings:
    server: str
    simulator_port: int | None
    login_min_interval_s: float
    rate_limit_initial_s: float
    rate_limit_max_s: float
    request_timeout_s: float
    state_file: Path


@dataclass(frozen=True)
class _ShareTiming:
    login_min_interval_s: float
    rate_limit_initial_s: float
    rate_limit_max_s: float
    request_timeout_s: float


@dataclass(frozen=True)
class DisplaySettings:
    snapshot_file: Path
    snapshot_max_age_s: float
    refresh_s: float
    last_known_cutoff_s: float
    framebuffer: Path
    font_file: Path


@dataclass(frozen=True)
class _DisplayTiming:
    snapshot_max_age_s: float
    refresh_s: float
    last_known_cutoff_s: float


@dataclass(frozen=True)
class Config:
    provider: str
    scenario: str | None
    policy: Policy
    runtime: RuntimeSettings
    serial: SerialSettings | None
    dexcom: DexcomSettings | None
    display: DisplaySettings | None
    outputs_enabled: bool


def load_config(path: Path) -> Config:
    try:
        with path.open("rb") as file:
            data = tomllib.load(file)
    except FileNotFoundError:
        raise ConfigError(f"{path}: file not found") from None
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path}: invalid TOML: {error}") from None
    return _parse(data, str(path))


def _parse(data: dict[str, Any], source: str) -> Config:
    if "provider" not in data:
        raise ConfigError(f"{source}: top level is missing ['provider']")
    provider = data["provider"]
    if not isinstance(provider, str) or provider not in PROVIDERS:
        raise ConfigError(f"{source}: provider must be one of {sorted(PROVIDERS)}")
    acquisition = "simulation" if provider == "simulated" else "dexcom"
    _check_keys(data, {"provider", "alarm", "runtime", "transport", "display", acquisition},
                {"hardware"}, "top level", source)
    scenario = None
    if provider == "simulated":
        simulation = _table(data, "simulation", source)
        _check_keys(simulation, {"scenario"}, set(), "[simulation]", source)
        if simulation["scenario"] not in SCENARIOS:
            raise ConfigError(f"{source}: [simulation] scenario must be one of "
                              f"{sorted(SCENARIOS)}")
        scenario = simulation["scenario"]
    if "hardware" in data:
        hardware = _table(data, "hardware", source)
        _check_keys(hardware, set(), {"outputs_enabled"}, "[hardware]", source)
        if "outputs_enabled" in hardware and hardware["outputs_enabled"] is not False:
            raise ConfigError(f"{source}: [hardware] outputs_enabled must be false; outputs "
                              "are not implemented and require explicit approval")
    try:
        policy = Policy(**_numbers(_table(data, "alarm", source), Policy, "[alarm]", source))
    except ValueError as error:
        raise ConfigError(f"{source}: [alarm] {error}") from None
    runtime = RuntimeSettings(**_numbers(_table(data, "runtime", source), RuntimeSettings,
                                         "[runtime]", source))
    _check_runtime(runtime, policy, source)
    dexcom = None if provider == "simulated" \
        else _dexcom(_table(data, "dexcom", source), runtime, source)
    display = _display(_table(data, "display", source), runtime, policy, source)
    return Config(provider, scenario, policy, runtime,
                  _serial(_table(data, "transport", source), source), dexcom, display,
                  outputs_enabled=False)


def _display(table: dict[str, Any], runtime: RuntimeSettings, policy: Policy,
             source: str) -> DisplaySettings | None:
    kind = table.get("kind")
    if kind not in DISPLAY_KINDS:
        raise ConfigError(f"{source}: [display] kind must be one of {sorted(DISPLAY_KINDS)}")
    if kind == "log":
        _check_keys(table, {"kind"}, set(), "[display]", source)
        return None
    timing_keys = {field.name for field in fields(_DisplayTiming)}
    _check_keys(table, {"kind", *DISPLAY_PATHS} | timing_keys, set(), "[display]", source)
    timing = _DisplayTiming(**_numbers({key: table[key] for key in timing_keys},
                                       _DisplayTiming, "[display]", source))
    for key in DISPLAY_PATHS:
        if not isinstance(table[key], str) or not Path(table[key]).is_absolute():
            raise ConfigError(f"{source}: [display] {key} must be an absolute path")
    rules = (
        (timing.snapshot_max_age_s > runtime.tick_s,
         "[display] snapshot_max_age_s must exceed [runtime] tick_s"),
        (timing.refresh_s < timing.snapshot_max_age_s,
         "[display] refresh_s must be below snapshot_max_age_s"),
        (timing.last_known_cutoff_s > policy.max_age_s,
         "[display] last_known_cutoff_s must exceed [alarm] max_age_s"),
    )
    for ok, message in rules:
        if not ok:
            raise ConfigError(f"{source}: {message}")
    return DisplaySettings(Path(table["snapshot_file"]), timing.snapshot_max_age_s,
                           timing.refresh_s, timing.last_known_cutoff_s,
                           Path(table["framebuffer"]), Path(table["font_file"]))


def _dexcom(table: dict[str, Any], runtime: RuntimeSettings, source: str) -> DexcomSettings:
    server = table.get("server")
    if not isinstance(server, str) or server not in SHARE_SERVERS:
        raise ConfigError(f"{source}: [dexcom] server must be one of {sorted(SHARE_SERVERS)}")
    timing_keys = {field.name for field in fields(_ShareTiming)}
    port_keys = {"simulator_port"} if server == "simulator" else set()
    _check_keys(table, {"server", "state_file"} | timing_keys | port_keys, set(), "[dexcom]",
                source)
    timing = _ShareTiming(**_numbers({key: table[key] for key in timing_keys}, _ShareTiming,
                                     "[dexcom]", source))
    port = table["simulator_port"] if port_keys else None
    if port_keys and (isinstance(port, bool) or not isinstance(port, int)
                      or not 1 <= port <= 65535):
        raise ConfigError(f"{source}: [dexcom] simulator_port must be an integer port")
    state_file = table["state_file"]
    if not isinstance(state_file, str) or not Path(state_file).is_absolute():
        raise ConfigError(f"{source}: [dexcom] state_file must be an absolute path")
    rules = (
        (timing.login_min_interval_s >= MIN_LOGIN_INTERVAL_S,
         f"[dexcom] login_min_interval_s must be at least {MIN_LOGIN_INTERVAL_S:g}"),
        (timing.rate_limit_initial_s <= timing.rate_limit_max_s,
         "[dexcom] rate_limit_initial_s must not exceed rate_limit_max_s"),
        (timing.request_timeout_s * MAX_REQUESTS_PER_FETCH <= runtime.fetch_timeout_s,
         f"[dexcom] request_timeout_s times {MAX_REQUESTS_PER_FETCH} must not exceed "
         "[runtime] fetch_timeout_s"),
    )
    for ok, message in rules:
        if not ok:
            raise ConfigError(f"{source}: {message}")
    return DexcomSettings(server, port, timing.login_min_interval_s,
                          timing.rate_limit_initial_s, timing.rate_limit_max_s,
                          timing.request_timeout_s, Path(state_file))


def _serial(transport: dict[str, Any], source: str) -> SerialSettings | None:
    kind = transport.get("kind")
    if kind not in TRANSPORTS:
        raise ConfigError(f"{source}: [transport] kind must be one of {sorted(TRANSPORTS)}")
    if kind == "simulated":
        _check_keys(transport, {"kind"}, set(), "[transport]", source)
        return None
    _check_keys(transport, {"kind", "device", "reconnect_initial_s", "reconnect_max_s"}, set(),
                "[transport]", source)
    device = transport["device"]
    if not isinstance(device, str) or not device:
        raise ConfigError(f"{source}: [transport] device must be a non-empty string")
    timing = {key: value for key, value in transport.items() if key.startswith("reconnect_")}
    settings = SerialSettings(device, **_numbers(
        timing, _ReconnectTiming, "[transport]", source))
    if settings.reconnect_initial_s > settings.reconnect_max_s:
        raise ConfigError(f"{source}: [transport] reconnect_initial_s must not exceed "
                          "reconnect_max_s")
    return settings


@dataclass(frozen=True)
class _ReconnectTiming:
    reconnect_initial_s: float
    reconnect_max_s: float


def _check_runtime(runtime: RuntimeSettings, policy: Policy, source: str) -> None:
    rules = (
        (runtime.tick_s < policy.heartbeat_timeout_s,
         "[runtime] tick_s must be below [alarm] heartbeat_timeout_s"),
        (runtime.fetch_timeout_s <= runtime.fetch_interval_s,
         "[runtime] fetch_timeout_s must not exceed fetch_interval_s"),
        (runtime.fetch_interval_s < policy.max_age_s,
         "[runtime] fetch_interval_s must be below [alarm] max_age_s"),
        (runtime.clock_check_timeout_s <= runtime.clock_check_interval_s,
         "[runtime] clock_check_timeout_s must not exceed clock_check_interval_s"),
    )
    for ok, message in rules:
        if not ok:
            raise ConfigError(f"{source}: {message}")


def _table(data: dict[str, Any], key: str, source: str) -> dict[str, Any]:
    value = data[key]
    if not isinstance(value, dict):
        raise ConfigError(f"{source}: {key} must be a table")
    return value


def _check_keys(table: dict[str, Any], required: set[str], optional: set[str], where: str,
                source: str) -> None:
    missing = required - table.keys()
    if missing:
        raise ConfigError(f"{source}: {where} is missing {sorted(missing)}")
    unknown = table.keys() - required - optional
    if unknown:
        raise ConfigError(f"{source}: {where} has unknown keys {sorted(unknown)}")


def _numbers(table: dict[str, Any], schema: type, where: str, source: str) -> dict[str, float]:
    names = {field.name for field in fields(schema)}
    _check_keys(table, names, set(), where, source)
    for name in sorted(names):
        value = table[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not isfinite(value) or value <= 0:
            raise ConfigError(f"{source}: {where} {name} must be a positive number")
    return {name: float(table[name]) for name in names}
