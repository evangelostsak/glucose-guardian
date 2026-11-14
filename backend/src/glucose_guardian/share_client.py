"""Dexcom Share HTTPS client, login state machine and glucose provider."""
import contextlib
import functools
import http.client
import logging
import socket
import ssl
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import IO, Any, Protocol

from .credentials import Credentials
from .dexcom_share import (
    AUTHENTICATE,
    LOGIN,
    ShareAuthBlocked,
    ShareEntry,
    ShareError,
    ShareProtocolError,
    ShareRateLimited,
    ShareSessionExpired,
    ShareStatus,
    ShareTransportError,
    authenticate_body,
    check_request,
    classify_error,
    login_body,
    newest,
    parse_entries,
    parse_identifier,
    read_url,
    window,
)
from .providers import Reading
from .share_state import AuthStateStore

log = logging.getLogger(__name__)

MAX_RESPONSE_BYTES = 65536
CHUNK_BYTES = 4096


@dataclass(frozen=True)
class HttpResponse:
    status: int
    retry_after: str | None
    body: bytes


class Poster(Protocol):
    def __call__(self, url: str, body: bytes, timeout_s: float) -> HttpResponse: ...


@dataclass(frozen=True)
class ShareServer:
    base_url: str
    application_id: str


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: urllib.request.Request, fp: IO[bytes], code: int,
                         msg: str, headers: Any, newurl: str) -> None:
        return None


class _Deadline:
    def __init__(self, timeout_s: float, monotonic: Callable[[], float]) -> None:
        self._end_s = monotonic() + timeout_s
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._watched: socket.socket | None = None
        self.expired = False
        self._timer = threading.Timer(timeout_s, self._expire)
        self._timer.daemon = True
        self._timer.start()

    def remaining_s(self) -> float:
        left = self._end_s - self._monotonic()
        if self.expired or left <= 0:
            raise TimeoutError("request deadline exceeded")
        return left

    def connect(self, host: str, port: int) -> socket.socket:
        failure: OSError | None = None
        for family, kind, proto, _, address in socket.getaddrinfo(host, port,
                                                                  type=socket.SOCK_STREAM):
            sock = socket.socket(family, kind, proto)
            try:
                sock.settimeout(self.remaining_s())
                sock.connect(address)
                self._watch(sock)
            except OSError as error:
                sock.close()
                failure = error
                continue
            return sock
        raise failure if failure is not None else OSError("no address to connect to")

    def _watch(self, sock: socket.socket) -> None:
        with self._lock:
            if self.expired:
                raise TimeoutError("request deadline exceeded")
            if self._watched is not None:
                self._watched.close()
            self._watched = sock.dup()

    def _expire(self) -> None:
        with self._lock:
            self.expired = True
            if self._watched is not None:
                with contextlib.suppress(OSError):
                    self._watched.shutdown(socket.SHUT_RDWR)

    def close(self) -> None:
        self._timer.cancel()
        self._timer.join()
        with self._lock:
            if self._watched is not None:
                self._watched.close()
                self._watched = None


class _DeadlineConnection(http.client.HTTPConnection):
    def __init__(self, host: str, *, timeout: float, deadline: _Deadline) -> None:
        super().__init__(host, timeout=timeout)
        self._deadline = deadline

    def connect(self) -> None:
        self.sock = self._deadline.connect(self.host, self.port)


class _DeadlineTLSConnection(_DeadlineConnection):
    default_port = http.client.HTTPS_PORT

    def __init__(self, host: str, *, timeout: float, deadline: _Deadline,
                 tls: ssl.SSLContext) -> None:
        super().__init__(host, timeout=timeout, deadline=deadline)
        self._tls = tls

    def connect(self) -> None:
        sock = self._deadline.connect(self.host, self.port)
        try:
            self.sock = self._tls.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


class _DeadlineHandler(urllib.request.HTTPHandler):
    def __init__(self, deadline: _Deadline) -> None:
        super().__init__()
        self._deadline = deadline

    def http_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        return self.do_open(functools.partial(_DeadlineConnection, deadline=self._deadline),
                            req)


class _DeadlineTLSHandler(urllib.request.HTTPSHandler):
    def __init__(self, deadline: _Deadline, tls: ssl.SSLContext) -> None:
        super().__init__(context=tls)
        self._deadline = deadline
        self._tls = tls

    def https_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        return self.do_open(functools.partial(_DeadlineTLSConnection, deadline=self._deadline,
                                              tls=self._tls), req)


def _reason(error: BaseException) -> str:
    if isinstance(error, urllib.error.URLError) and isinstance(error.reason, BaseException):
        error = error.reason
    if isinstance(error, TimeoutError):
        return "deadline exceeded"
    return type(error).__name__


def _declared_length(headers: Any) -> int | None:
    length = headers.get("Content-Length") if headers is not None else None
    return int(length) if isinstance(length, str) and length.strip().isdigit() else None


def _read_bounded(stream: Any, headers: Any, max_bytes: int) -> bytes:
    declared = _declared_length(headers)
    if declared is not None and declared > max_bytes:
        raise ShareTransportError("response too large")
    chunks: list[bytes] = []
    size = 0
    while True:
        try:
            chunk = stream.read(CHUNK_BYTES)
        except (OSError, http.client.HTTPException, ValueError) as error:
            raise ShareTransportError(f"response read failed: {_reason(error)}") from None
        if not chunk:
            break
        size += len(chunk)
        if size > max_bytes:
            raise ShareTransportError("response too large")
        chunks.append(chunk)
    if declared is not None and size != declared:
        raise ShareTransportError("response read failed: IncompleteRead")
    return b"".join(chunks)


def _exchange(opener: urllib.request.OpenerDirector, request: urllib.request.Request,
              timeout_s: float, max_bytes: int) -> HttpResponse:
    try:
        response = opener.open(request, timeout=timeout_s)
    except urllib.error.HTTPError as error:
        response = error
    except (OSError, http.client.HTTPException, ValueError) as error:
        raise ShareTransportError(f"request failed: {_reason(error)}") from None
    failed = isinstance(response, urllib.error.HTTPError)
    status = int(response.code if failed else response.status)
    has_body = not failed or response.fp is not None
    try:
        if 300 <= status < 400:
            raise ShareTransportError("redirect refused")
        data = _read_bounded(response, response.headers, max_bytes) if has_body else b""
        retry_after = response.headers.get("Retry-After") if response.headers else None
    finally:
        if has_body:
            response.close()
    return HttpResponse(status, retry_after, data)


def urllib_poster(*, context: ssl.SSLContext, user_agent: str, max_bytes: int,
                  monotonic: Callable[[], float]) -> Poster:
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "Accept-Encoding": "identity", "User-Agent": user_agent}

    def post(url: str, body: bytes, timeout_s: float) -> HttpResponse:
        deadline = _Deadline(timeout_s, monotonic)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                             _DeadlineHandler(deadline),
                                             _DeadlineTLSHandler(deadline, context),
                                             _RefuseRedirects())
        request = urllib.request.Request(url, data=body, method="POST", headers=headers)
        try:
            return _exchange(opener, request, timeout_s, max_bytes)
        finally:
            deadline.close()
            if deadline.expired:
                raise ShareTransportError("request deadline exceeded") from None

    return post


class ShareSession:
    def __init__(self, *, server: ShareServer, credentials: Credentials, post: Poster,
                 store: AuthStateStore, request_timeout_s: float,
                 boottime: Callable[[], float]) -> None:
        self._server = server
        self._credentials = credentials
        self._post = post
        self._store = store
        self._timeout_s = request_timeout_s
        self._boottime = boottime
        self._account_id: str | None = None
        self._session_id: str | None = None

    def read(self, minutes: int, max_count: int) -> list[ShareEntry]:
        entries = parse_entries(self.read_raw(minutes, max_count), max_count)
        self._store.succeeded()
        return entries

    def read_raw(self, minutes: int, max_count: int) -> bytes:
        self._store.check_ready(self._boottime())
        if self._session_id is None:
            self._login()
        try:
            return self._read(minutes, max_count)
        except ShareSessionExpired:
            self._session_id = None
            log.info("Dexcom Share session expired")
            self._login()
            return self._read(minutes, max_count)

    def _login(self) -> None:
        self._store.begin_login(self._boottime())
        password, application_id = self._credentials.password, self._server.application_id
        try:
            if self._account_id is None:
                self._account_id = parse_identifier(self._call(
                    self._server.base_url + AUTHENTICATE,
                    authenticate_body(self._credentials.username, password, application_id),
                    authenticating=True))
            self._session_id = parse_identifier(self._call(
                self._server.base_url + LOGIN,
                login_body(self._account_id, password, application_id), authenticating=True))
        except ShareAuthBlocked as error:
            self._account_id = None
            self._session_id = None
            self._store.block(error.reason)
            raise
        finally:
            self._store.end_login()
        log.info("Dexcom Share login succeeded")

    def _read(self, minutes: int, max_count: int) -> bytes:
        session_id = self._session_id
        if session_id is None:
            raise ShareProtocolError("no session")
        url = read_url(self._server.base_url, session_id, minutes, max_count)
        return self._call(url, b"{}", authenticating=False)

    def _call(self, url: str, body: bytes, *, authenticating: bool) -> bytes:
        check_request(self._server.base_url, url, body)
        response = self._post(url, body, self._timeout_s)
        if response.status == 200:
            return response.body
        error = classify_error(response.status, response.body, authenticating=authenticating,
                               retry_after=response.retry_after)
        if isinstance(error, ShareRateLimited):
            self._store.rate_limited(self._boottime(), error.retry_after_s)
        raise error


class DexcomShareProvider:
    def __init__(self, session: ShareSession, *, max_age_s: float) -> None:
        self._session = session
        self._minutes, self._max_count = window(max_age_s)
        self._last: ShareEntry | None = None
        self.status = ShareStatus.STARTING

    def read(self, now: datetime) -> Reading | None:
        try:
            reading = self._accept(newest(self._session.read(self._minutes, self._max_count)))
        except ShareError as error:
            self._report(error.status, str(error))
            raise
        self._report(ShareStatus.OK, "data received")
        return reading

    def _accept(self, entry: ShareEntry | None) -> Reading | None:
        if entry is None:
            return None
        last = self._last
        if last is not None and entry.timestamp <= last.timestamp:
            if entry.timestamp == last.timestamp and entry.mg_dl != last.mg_dl:
                raise ShareProtocolError("reading changed for the same timestamp")
            return None
        self._last = entry
        return Reading(entry.mg_dl, entry.timestamp, entry.trend)

    def _report(self, status: ShareStatus, detail: str) -> None:
        if status is not self.status:
            level = logging.INFO if status is ShareStatus.OK else logging.WARNING
            log.log(level, "Dexcom Share status %s: %s", status.value, detail)
        self.status = status
