"""Strict TOML configuration loading. Every value is required except fail-safe flags."""
import tomllib
from dataclasses import dataclass, fields
from math import isfinite
from pathlib import Path
from typing import Any

from .alarms import Policy
from .simulation import SCENARIOS

PROVIDERS = frozenset({"simulated"})
TRANSPORTS = frozenset({"simulated", "serial"})


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
class Config:
    provider: str
    scenario: str
    policy: Policy
    runtime: RuntimeSettings
    serial: SerialSettings | None


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
    _check_keys(data, {"provider", "alarm", "runtime", "simulation", "transport"}, {"hardware"},
                "top level", source)
    provider = data["provider"]
    if provider not in PROVIDERS:
        raise ConfigError(f"{source}: provider must be one of {sorted(PROVIDERS)}; "
                          "real acquisition is not available yet")
    simulation = _table(data, "simulation", source)
    _check_keys(simulation, {"scenario"}, set(), "[simulation]", source)
    if simulation["scenario"] not in SCENARIOS:
        raise ConfigError(f"{source}: [simulation] scenario must be one of {sorted(SCENARIOS)}")
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
    return Config(provider, simulation["scenario"], policy, runtime,
                  _serial(_table(data, "transport", source), source))


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
