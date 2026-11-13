"""Local synthetic Dexcom Share server for tests and soak runs. Serves synthetic values only."""
import argparse
import json
import signal
import sys
import threading
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .dexcom_share import APPLICATION_ID, AUTHENTICATE, LOGIN, READ, SERVICES_PATH

SYNTHETIC_USERNAME = "synthetic-publisher"
SYNTHETIC_PASSWORD = "synthetic-password"
SYNTHETIC_ACCOUNT_ID = "5ad1c0de-0000-4000-8000-00000000a11d"
HANG_LIMIT_S = 120.0
OVERSIZE_BYTES = 70000
CYCLE_MG_DL = (110, 115, 120, 115)
ENDPOINTS = {SERVICES_PATH + AUTHENTICATE: "authenticate", SERVICES_PATH + LOGIN: "login",
             SERVICES_PATH + READ: "read"}


class Fault(str, Enum):
    RATE_LIMIT = "rate_limit"
    SERVER_ERROR = "server_error"
    HTML_ERROR = "html_error"
    REDIRECT = "redirect"
    MALFORMED = "malformed"
    OVERSIZE = "oversize"
    SLOW = "slow"
    HANG = "hang"


@dataclass(frozen=True)
class SyntheticReading:
    mg_dl: int
    timestamp: datetime
    trend: str


Source = Callable[[datetime], list[SyntheticReading]]


def share_entry(reading: SyntheticReading) -> dict[str, object]:
    milliseconds = round(reading.timestamp.timestamp() * 1000)
    return {"WT": f"Date({milliseconds})", "ST": f"Date({milliseconds})",
            "DT": f"Date({milliseconds}+0000)", "Value": reading.mg_dl, "Trend": reading.trend}


def static(readings: list[SyntheticReading]) -> Source:
    return lambda now: list(readings)


def cycling(now: datetime) -> list[SyntheticReading]:
    slot = int(now.timestamp() // 300)
    return [SyntheticReading(CYCLE_MG_DL[slot % len(CYCLE_MG_DL)],
                             datetime.fromtimestamp(slot * 300, UTC), "Flat")]


class ShareSimulator:
    def __init__(self, *, port: int, now: Callable[[], datetime], source: Source) -> None:
        self.now = now
        self.source = source
        self.lock = threading.Lock()
        self.raw_entries: list[object] | None = None
        self.faults: dict[str, Fault] = {}
        self.retry_after: str | None = None
        self.slow_s = 0.0
        self.password = SYNTHETIC_PASSWORD
        self.locked_out = False
        self.sessions: set[str] = set()
        self.requests: Counter[str] = Counter()
        self.release = threading.Event()
        self._server = ThreadingHTTPServer(("127.0.0.1", port), self._handler())
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}{SERVICES_PATH}"

    def start(self) -> "ShareSimulator":
        self._thread.start()
        return self

    def stop(self) -> None:
        self.release.set()
        if self._thread.is_alive():
            self._server.shutdown()
        self._server.server_close()

    def set_fault(self, fault: Fault | None, *endpoints: str) -> None:
        with self.lock:
            for endpoint in endpoints:
                if fault is None:
                    self.faults.pop(endpoint, None)
                else:
                    self.faults[endpoint] = fault

    def expire_sessions(self) -> None:
        with self.lock:
            self.sessions.clear()

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        simulator = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return None

            def do_POST(self) -> None:
                simulator._serve(self)

        return Handler

    def _send(self, handler: BaseHTTPRequestHandler, status: int, body: bytes,
              content_type: str, extra: dict[str, str]) -> None:
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        for name, value in extra.items():
            handler.send_header(name, value)
        handler.end_headers()
        handler.wfile.write(body)

    def _json(self, handler: BaseHTTPRequestHandler, status: int, value: object) -> None:
        self._send(handler, status, json.dumps(value).encode(), "application/json", {})

    def _error(self, handler: BaseHTTPRequestHandler, code: str, message: str) -> None:
        self._json(handler, 500, {"Code": code, "Message": message})

    def _serve(self, handler: BaseHTTPRequestHandler) -> None:
        url = urlsplit(handler.path)
        endpoint = ENDPOINTS.get(url.path)
        length = handler.headers.get("Content-Length")
        raw = handler.rfile.read(int(length)) if length else b""
        with self.lock:
            self.requests[endpoint if endpoint is not None else url.path] += 1
            fault = self.faults.get(endpoint) if endpoint is not None else None
            retry_after, slow_s = self.retry_after, self.slow_s
        if endpoint is None:
            self._send(handler, 404, b"not found", "text/plain", {})
            return
        if fault is Fault.HANG:
            self.release.wait(HANG_LIMIT_S)
            handler.close_connection = True
            return
        if fault is Fault.SLOW:
            self.release.wait(slow_s)
        elif fault is not None:
            self._fault(handler, fault, retry_after)
            return
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._error(handler, "InvalidArgument", "body is not JSON")
            return
        if endpoint == "read":
            self._read(handler, parse_qs(url.query))
        else:
            self._login(handler, endpoint, body)

    def _fault(self, handler: BaseHTTPRequestHandler, fault: Fault,
               retry_after: str | None) -> None:
        if fault is Fault.RATE_LIMIT:
            extra = {} if retry_after is None else {"Retry-After": retry_after}
            self._send(handler, 429, b"Too Many Requests", "text/plain", extra)
        elif fault is Fault.SERVER_ERROR:
            self._error(handler, "ServiceUnavailable",
                        f"synthetic outage for {SYNTHETIC_USERNAME}")
        elif fault is Fault.HTML_ERROR:
            self._send(handler, 500, b"<html><body>Server Error</body></html>", "text/html",
                       {})
        elif fault is Fault.REDIRECT:
            self._send(handler, 302, b"", "text/plain",
                       {"Location": f"{self.base_url}Redirected"})
        elif fault is Fault.MALFORMED:
            self._send(handler, 200, b"[{", "application/json", {})
        elif fault is Fault.OVERSIZE:
            self._send(handler, 200, b"[" + b" " * OVERSIZE_BYTES + b"]", "application/json",
                       {})

    def _login(self, handler: BaseHTTPRequestHandler, endpoint: str, body: object) -> None:
        identifier_key = "accountName" if endpoint == "authenticate" else "accountId"
        expected = SYNTHETIC_USERNAME if endpoint == "authenticate" else SYNTHETIC_ACCOUNT_ID
        if not isinstance(body, dict) or set(body) != {identifier_key, "password",
                                                         "applicationId"}:
            self._error(handler, "InvalidArgument", "unexpected body")
            return
        with self.lock:
            locked_out, password = self.locked_out, self.password
        if locked_out:
            self._error(handler, "SSO_AuthenticateMaxAttemptsExceeded",
                        f"maximum attempts for {body[identifier_key]}")
            return
        if (body[identifier_key], body["password"], body["applicationId"]) != (
                expected, password, APPLICATION_ID):
            self._error(handler, "AccountPasswordInvalid",
                        f"cannot authenticate {body[identifier_key]} with {body['password']}")
            return
        if endpoint == "authenticate":
            self._json(handler, 200, SYNTHETIC_ACCOUNT_ID)
            return
        session = str(uuid.uuid4())
        with self.lock:
            self.sessions.add(session)
        self._json(handler, 200, session)

    def _read(self, handler: BaseHTTPRequestHandler, query: dict[str, list[str]]) -> None:
        values = [query.get(key) for key in ("sessionId", "minutes", "maxCount")]
        if any(value is None or len(value) != 1 for value in values):
            self._error(handler, "InvalidArgument", "sessionId, minutes and maxCount required")
            return
        session, minutes, max_count = (value[0] for value in values if value is not None)
        with self.lock:
            known = session in self.sessions
            raw_entries = self.raw_entries
        if not known:
            self._error(handler, "SessionIdNotFound", f"session {session} not found")
            return
        if not minutes.isdigit() or not max_count.isdigit():
            self._error(handler, "InvalidArgument", "minutes and maxCount must be integers")
            return
        if raw_entries is not None:
            self._json(handler, 200, raw_entries)
            return
        now = self.now()
        since = now - timedelta(minutes=int(minutes))
        readings = sorted((reading for reading in self.source(now)
                           if since <= reading.timestamp), key=lambda reading:
                          reading.timestamp, reverse=True)[:int(max_count)]
        self._json(handler, 200, [share_entry(reading) for reading in readings])


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m glucose_guardian.share_simulator")
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--scenario", required=True,
                        choices=("normal", "stale", "empty", "server-error", "rate-limit",
                                 "invalid-password", "lockout", "hang"))
    args = parser.parse_args(argv)
    started = datetime.now(UTC)
    sources: dict[str, Source] = {
        "stale": static(cycling(started)),
        "empty": static([]),
    }
    simulator = ShareSimulator(port=args.port, now=lambda: datetime.now(UTC),
                               source=sources[args.scenario] if args.scenario in sources
                               else cycling)
    faults = {"server-error": Fault.SERVER_ERROR, "rate-limit": Fault.RATE_LIMIT,
              "hang": Fault.HANG}
    if args.scenario in faults:
        simulator.set_fault(faults[args.scenario], "read")
    simulator.password = "not-the-synthetic-password" if args.scenario == "invalid-password" \
        else SYNTHETIC_PASSWORD
    simulator.locked_out = args.scenario == "lockout"
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda signum, frame: stop.set())
    simulator.start()
    print(f"synthetic Dexcom Share simulator on {simulator.base_url} "
          f"(scenario {args.scenario}, synthetic data only)", flush=True)
    stop.wait()
    simulator.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
