"""Dexcom Share protocol: endpoints, request bodies, strict parsing, error classes. No I/O."""
import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any, ClassVar
from urllib.parse import parse_qsl, urlencode

AUTHENTICATE = "General/AuthenticatePublisherAccount"
LOGIN = "General/LoginPublisherAccountById"
READ = "Publisher/ReadPublisherLatestGlucoseValues"
SERVICES_PATH = "/ShareWebServices/Services/"
APPLICATION_ID = "d89443d2-327c-4a6f-89e5-496bbb0317db"
APPLICATION_ID_JP = "d8665ade-9673-4e27-9ff6-92db4ce13d13"
SERVERS = {
    "us": ("https://share2.dexcom.com" + SERVICES_PATH, APPLICATION_ID),
    "ous": ("https://shareous1.dexcom.com" + SERVICES_PATH, APPLICATION_ID),
    "jp": ("https://share.dexcom.jp" + SERVICES_PATH, APPLICATION_ID_JP),
}
APPROVED_BASES = frozenset(base for base, _ in SERVERS.values())
SIMULATOR_BASE = re.compile(r"http://127\.0\.0\.1:[0-9]{1,5}" + re.escape(SERVICES_PATH))
APPROVED_BODIES = {
    AUTHENTICATE: frozenset({"accountName", "password", "applicationId"}),
    LOGIN: frozenset({"accountId", "password", "applicationId"}),
    READ: frozenset(),
}
READ_QUERY = ("sessionId", "minutes", "maxCount")
WINDOW_NUMBER = re.compile(r"[0-9]{1,4}")
NIL_UUID = "00000000-0000-0000-0000-000000000000"
UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
DATE = re.compile(r"/?Date\((\d{1,15})([+-]\d{4})?\)/?")
CODE = re.compile(r"[A-Za-z_]{1,64}")
REQUIRED_KEYS = frozenset({"WT", "ST", "DT", "Value", "Trend"})
LOW_MG_DL = 39
HIGH_MG_DL = 401
MAX_MINUTES = 1440
MAX_COUNT = 288
READING_INTERVAL_MIN = 5
MAX_REQUESTS_PER_FETCH = 3
SESSION_CODES = frozenset({"SessionIdNotFound", "SessionNotValid"})
CREDENTIAL_CODES = frozenset({"AccountPasswordInvalid", "SSO_AuthenticateMaxAttemptsExceeded"})
REJECTED_LOGIN_MESSAGES = ("Cannot Authenticate by AccountName",
                           "Cannot Authenticate by AccountId")


class Trend(str, Enum):
    NONE = "None"
    DOUBLE_UP = "DoubleUp"
    SINGLE_UP = "SingleUp"
    FORTY_FIVE_UP = "FortyFiveUp"
    FLAT = "Flat"
    FORTY_FIVE_DOWN = "FortyFiveDown"
    SINGLE_DOWN = "SingleDown"
    DOUBLE_DOWN = "DoubleDown"
    NOT_COMPUTABLE = "NotComputable"
    RATE_OUT_OF_RANGE = "RateOutOfRange"


LEGACY_TRENDS = tuple(Trend)


class ShareStatus(str, Enum):
    STARTING = "starting"
    OK = "ok"
    NETWORK_ERROR = "network_error"
    SERVER_ERROR = "server_error"
    PROTOCOL_ERROR = "protocol_error"
    RATE_LIMITED = "rate_limited"
    LOGIN_DEFERRED = "login_deferred"
    AUTH_BLOCKED = "auth_blocked"
    STATE_ERROR = "state_error"


class ShareError(Exception):
    status: ClassVar[ShareStatus]


class ShareTransportError(ShareError):
    status = ShareStatus.NETWORK_ERROR


class ShareServerError(ShareError):
    status = ShareStatus.SERVER_ERROR


class ShareProtocolError(ShareError):
    status = ShareStatus.PROTOCOL_ERROR


class ShareRequestRefused(ShareProtocolError):
    pass


class ShareSessionExpired(ShareError):
    status = ShareStatus.SERVER_ERROR


class ShareLoginDeferred(ShareError):
    status = ShareStatus.LOGIN_DEFERRED


class ShareStateError(ShareError):
    status = ShareStatus.STATE_ERROR


class ShareRateLimited(ShareError):
    status = ShareStatus.RATE_LIMITED

    def __init__(self, message: str, retry_after_s: float | None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class ShareAuthBlocked(ShareError):
    status = ShareStatus.AUTH_BLOCKED

    def __init__(self, reason: str) -> None:
        super().__init__(f"automatic login blocked: {reason}")
        self.reason = reason


@dataclass(frozen=True)
class ShareEntry:
    timestamp: datetime
    system_time: datetime
    display_time: datetime
    mg_dl: int
    trend: Trend


def window(max_age_s: float) -> tuple[int, int]:
    minutes = min(MAX_MINUTES, max(1, math.ceil(max_age_s / 60)))
    return minutes, min(MAX_COUNT, math.ceil(minutes / READING_INTERVAL_MIN) + 1)


def authenticate_body(account_name: str, password: str, application_id: str) -> bytes:
    return json.dumps({"accountName": account_name, "password": password,
                       "applicationId": application_id}).encode()


def login_body(account_id: str, password: str, application_id: str) -> bytes:
    return json.dumps({"accountId": account_id, "password": password,
                       "applicationId": application_id}).encode()


def read_url(base_url: str, session_id: str, minutes: int, max_count: int) -> str:
    if not 1 <= minutes <= MAX_MINUTES or not 1 <= max_count <= MAX_COUNT:
        raise ValueError("minutes or maxCount out of range")
    query = urlencode({"sessionId": session_id, "minutes": minutes, "maxCount": max_count})
    return f"{base_url}{READ}?{query}"


def _read_query(query: str) -> bool:
    try:
        pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return False
    if [key for key, _ in pairs] != list(READ_QUERY):
        return False
    session, minutes, count = (value for _, value in pairs)
    if UUID.fullmatch(session) is None or WINDOW_NUMBER.fullmatch(minutes) is None \
            or WINDOW_NUMBER.fullmatch(count) is None:
        return False
    return 1 <= int(minutes) <= MAX_MINUTES and 1 <= int(count) <= MAX_COUNT


def _body_keys(body: bytes) -> frozenset[str] | None:
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or not all(isinstance(value, str) for value in data.values()):
        return None
    return frozenset(data)


def check_request(base_url: str, url: str, body: bytes) -> None:
    if base_url not in APPROVED_BASES and SIMULATOR_BASE.fullmatch(base_url) is None:
        raise ShareRequestRefused("request refused: not an approved Dexcom Share server")
    path, separator, query = url.partition("?")
    endpoint = path[len(base_url):] if path.startswith(base_url) else None
    if endpoint is None or endpoint not in APPROVED_BODIES:
        raise ShareRequestRefused("request refused: not an approved read-only operation")
    query_ok = _read_query(query) if endpoint == READ else separator == ""
    if not query_ok:
        raise ShareRequestRefused("request refused: unexpected query")
    if _body_keys(body) != APPROVED_BODIES[endpoint]:
        raise ShareRequestRefused("request refused: unexpected body")


def _json(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ShareProtocolError("response is not JSON") from None


def parse_identifier(body: bytes) -> str:
    value = _json(body)
    if not isinstance(value, str) or UUID.fullmatch(value) is None:
        raise ShareProtocolError("identifier is not a UUID")
    if value == NIL_UUID:
        raise ShareAuthBlocked("NilIdentifier")
    return value


def _date(value: object, field: str) -> datetime:
    match = DATE.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ShareProtocolError(f"entry {field} is not a Dexcom date")
    try:
        return datetime.fromtimestamp(int(match.group(1)) / 1000, UTC)
    except (OverflowError, OSError, ValueError):
        raise ShareProtocolError(f"entry {field} is out of range") from None


def _trend(value: object) -> Trend:
    if isinstance(value, bool):
        raise ShareProtocolError("entry Trend is not a known trend")
    if isinstance(value, int) and 0 <= value < len(LEGACY_TRENDS):
        return LEGACY_TRENDS[value]
    if isinstance(value, str):
        try:
            return Trend(value)
        except ValueError:
            pass
    raise ShareProtocolError("entry Trend is not a known trend")


def _entry(item: object) -> ShareEntry:
    if not isinstance(item, dict):
        raise ShareProtocolError("entry is not an object")
    missing = REQUIRED_KEYS - item.keys()
    if missing:
        raise ShareProtocolError(f"entry is missing {', '.join(sorted(missing))}")
    value = item["Value"]
    if isinstance(value, bool) or not isinstance(value, int) \
            or not LOW_MG_DL <= value <= HIGH_MG_DL:
        raise ShareProtocolError(f"entry Value is not an integer from {LOW_MG_DL} to "
                                 f"{HIGH_MG_DL}")
    return ShareEntry(timestamp=_date(item["WT"], "WT"), system_time=_date(item["ST"], "ST"),
                      display_time=_date(item["DT"], "DT"), mg_dl=value,
                      trend=_trend(item["Trend"]))


def parse_entries(body: bytes, max_count: int) -> list[ShareEntry]:
    items = _json(body)
    if not isinstance(items, list):
        raise ShareProtocolError("glucose response is not a list")
    if len(items) > max_count:
        raise ShareProtocolError("glucose response has more entries than requested")
    entries = [_entry(item) for item in items]
    by_time: dict[datetime, int] = {}
    for entry in entries:
        if by_time.setdefault(entry.timestamp, entry.mg_dl) != entry.mg_dl:
            raise ShareProtocolError("entries with the same timestamp disagree")
    return entries


def newest(entries: Sequence[ShareEntry]) -> ShareEntry | None:
    return max(entries, key=lambda entry: entry.timestamp, default=None)


def retry_after_s(header: str | None) -> float | None:
    if header is None or not header.strip().isdigit():
        return None
    return float(header.strip())


def _error_code(body: bytes) -> tuple[str | None, str]:
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, ""
    if not isinstance(data, dict):
        return None, ""
    code, message = data.get("Code"), data.get("Message")
    return (code if isinstance(code, str) else None,
            message if isinstance(message, str) else "")


def classify_error(status: int, body: bytes, *, authenticating: bool,
                   retry_after: str | None) -> ShareError:
    if status == 429:
        return ShareRateLimited("HTTP 429", retry_after_s(retry_after))
    code, message = _error_code(body)
    if code in SESSION_CODES and not authenticating:
        return ShareSessionExpired(code)
    if code in CREDENTIAL_CODES:
        return ShareAuthBlocked(code)
    if code == "SSO_InternalError" and any(text in message
                                            for text in REJECTED_LOGIN_MESSAGES):
        return ShareAuthBlocked(code)
    if authenticating and code == "InvalidArgument":
        return ShareAuthBlocked(code)
    shown = code if code is not None and CODE.fullmatch(code) else "unrecognized"
    return ShareServerError(f"HTTP {status} {shown if code is not None else 'without code'}")
