"""Persistent Dexcom Share login state: cooldown, rate limit and block latch. Holds no secrets."""
import argparse
import contextlib
import json
import logging
import os
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from .dexcom_share import (
    ShareAuthBlocked,
    ShareLoginDeferred,
    ShareRateLimited,
    ShareStateError,
)

log = logging.getLogger(__name__)

SCHEMA = "glucose-guardian/dexcom-share-state/1"
UNREADABLE = "StateUnreadable"
LOGIN_UNKNOWN = "LoginOutcomeUnknown"
BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
EXIT_OK = 0
EXIT_FAILED = 1


@dataclass(frozen=True)
class AuthState:
    schema: str
    boot_id: str
    last_login_boottime_s: float | None
    last_login_utc: str | None
    rate_limited_until_boottime_s: float | None
    rate_limit_backoff_s: float | None
    blocked_reason: str | None
    blocked_utc: str | None
    login_pending: bool


FIELDS = frozenset(AuthState.__dataclass_fields__)


def fresh_state(boot_id: str) -> AuthState:
    return AuthState(SCHEMA, boot_id, None, None, None, None, None, None, False)


def block_reason(state: AuthState) -> str | None:
    if state.blocked_reason is None and state.login_pending:
        return LOGIN_UNKNOWN
    return state.blocked_reason


def _optional(data: dict[str, object], key: str, kinds: tuple[type, ...]) -> None:
    value = data[key]
    if value is not None and (isinstance(value, bool) or not isinstance(value, kinds)):
        raise ValueError(key)


def parse_state(text: str) -> AuthState:
    data = json.loads(text)
    if not isinstance(data, dict) or set(data) != FIELDS or data["schema"] != SCHEMA \
            or not isinstance(data["boot_id"], str) or not isinstance(data["login_pending"], bool):
        raise ValueError("structure")
    for key in ("last_login_boottime_s", "rate_limited_until_boottime_s",
                "rate_limit_backoff_s"):
        _optional(data, key, (int, float))
        if data[key] is not None:
            data[key] = float(data[key])
    for key in ("last_login_utc", "blocked_reason", "blocked_utc"):
        _optional(data, key, (str,))
    return AuthState(**data)


def read_boot_id(path: Path) -> str:
    return path.read_text().strip()


def write_state(path: Path, state: AuthState) -> None:
    partial = path.with_name(path.name + ".partial")
    descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w") as file:
            os.fchmod(file.fileno(), 0o600)
            file.write(json.dumps(asdict(state), indent=2) + "\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(partial, path)
    except OSError:
        with contextlib.suppress(OSError):
            partial.unlink()
        raise
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


class AuthStateStore:
    def __init__(self, *, path: Path, boot_id: str, boottime: Callable[[], float],
                 utc: Callable[[], datetime], login_min_interval_s: float,
                 rate_limit_initial_s: float, rate_limit_max_s: float) -> None:
        self._path = path
        self._boot_id = boot_id
        self._boottime = boottime
        self._utc = utc
        self._login_min_interval_s = login_min_interval_s
        self._rate_limit_initial_s = rate_limit_initial_s
        self._rate_limit_max_s = rate_limit_max_s
        self._dirty = False
        self.state = self._load()

    def _load(self) -> AuthState:
        try:
            state = parse_state(self._path.read_text())
        except FileNotFoundError:
            return fresh_state(self._boot_id)
        except (OSError, ValueError, TypeError, KeyError):
            log.error("Dexcom Share state file is unreadable; automatic login blocked")
            return replace(fresh_state(self._boot_id), blocked_reason=UNREADABLE,
                           blocked_utc=self._utc().isoformat())
        if block_reason(state) == LOGIN_UNKNOWN:
            log.error("Dexcom Share login outcome was not recorded; automatic login blocked")
            state = replace(state, blocked_reason=LOGIN_UNKNOWN,
                            blocked_utc=self._utc().isoformat())
        if state.boot_id == self._boot_id:
            return state
        last = self._boottime() if state.last_login_utc is not None else None
        return replace(state, boot_id=self._boot_id, last_login_boottime_s=last,
                       rate_limited_until_boottime_s=None)

    def _save(self, state: AuthState) -> None:
        self.state = state
        try:
            write_state(self._path, state)
        except OSError:
            self._dirty = True
            log.error("Dexcom Share state could not be written")
            raise ShareStateError("login state could not be written") from None
        self._dirty = False

    def _save_soon(self, state: AuthState) -> None:
        try:
            self._save(state)
        except ShareStateError:
            pass

    def check_ready(self, now_s: float) -> None:
        if self.state.blocked_reason is not None:
            raise ShareAuthBlocked(self.state.blocked_reason)
        until = self.state.rate_limited_until_boottime_s
        if until is not None and now_s < until:
            raise ShareRateLimited("rate limit backoff in effect", None)

    def begin_login(self, now_s: float) -> None:
        self.check_ready(now_s)
        last = self.state.last_login_boottime_s
        if last is not None and now_s - last < self._login_min_interval_s:
            raise ShareLoginDeferred("login interval not yet elapsed")
        self._save(replace(self.state, last_login_boottime_s=now_s,
                           last_login_utc=self._utc().isoformat(), login_pending=True))

    def end_login(self) -> None:
        if self.state.login_pending:
            self._save_soon(replace(self.state, login_pending=False))

    def rate_limited(self, now_s: float, retry_after_s: float | None) -> None:
        previous = self.state.rate_limit_backoff_s
        backoff = self._rate_limit_initial_s if previous is None \
            else min(self._rate_limit_max_s, previous * 2)
        requested = 0.0 if retry_after_s is None else min(retry_after_s, self._rate_limit_max_s)
        wait = max(backoff, requested)
        self._save_soon(replace(self.state, rate_limit_backoff_s=backoff,
                                rate_limited_until_boottime_s=now_s + wait))

    def succeeded(self) -> None:
        if self._dirty or self.state.rate_limit_backoff_s is not None \
                or self.state.rate_limited_until_boottime_s is not None:
            self._save_soon(replace(self.state, rate_limit_backoff_s=None,
                                    rate_limited_until_boottime_s=None))

    def block(self, reason: str) -> None:
        log.error("Dexcom Share automatic login blocked: %s", reason)
        self._save_soon(replace(self.state, blocked_reason=reason,
                                blocked_utc=self._utc().isoformat(), login_pending=False))


def unblock(path: Path, boot_id: str, boottime_s: float, utc: datetime) -> AuthState:
    try:
        state = parse_state(path.read_text())
    except FileNotFoundError:
        state = fresh_state(boot_id)
    except (OSError, ValueError, TypeError, KeyError):
        state = replace(fresh_state(boot_id), last_login_boottime_s=boottime_s,
                        last_login_utc=utc.isoformat())
    state = replace(state, blocked_reason=None, blocked_utc=None, login_pending=False)
    write_state(path, state)
    return state


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m glucose_guardian.share_state")
    parser.add_argument("--state-file", required=True, type=Path)
    parser.add_argument("command", choices=("status", "unblock"))
    args = parser.parse_args(argv)
    if args.command == "unblock":
        try:
            state = unblock(args.state_file, read_boot_id(BOOT_ID),
                            time.clock_gettime(time.CLOCK_BOOTTIME), datetime.now(UTC))
        except OSError as error:
            print(f"unblock failed: {type(error).__name__}", file=sys.stderr)
            return EXIT_FAILED
        print("automatic login unblocked; the login interval still applies")
    else:
        try:
            state = parse_state(args.state_file.read_text())
        except FileNotFoundError:
            print("no state file: no login has been attempted")
            return EXIT_OK
        except (OSError, ValueError, TypeError, KeyError):
            print("state file unreadable: automatic login is blocked", file=sys.stderr)
            return EXIT_FAILED
    reason = block_reason(state)
    if reason is None:
        blocked = "no"
    elif state.blocked_reason is None:
        blocked = f"{reason}: no outcome recorded for the login attempt at {state.last_login_utc}"
    else:
        blocked = f"{reason} since {state.blocked_utc}"
    print(f"blocked: {blocked}")
    print(f"last login attempt (UTC): {state.last_login_utc}")
    print(f"rate limit backoff (s): {state.rate_limit_backoff_s}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
