"""Dexcom Share HTTPS client, login state machine and glucose provider."""
import http.client
import logging
import ssl
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
    Trend,
    authenticate_body,
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


def _reason(error: BaseException) -> str:
    if isinstance(error, urllib.error.URLError) and isinstance(error.reason, BaseException):
        return type(error.reason).__name__
    return type(error).__name__


def _read_bounded(stream: Any, headers: Any, deadline_s: float, max_bytes: int,
                  monotonic: Callable[[], float]) -> bytes:
    length = headers.get("Content-Length") if headers is not None else None
    if isinstance(length, str) and length.strip().isdigit() and int(length) > max_bytes:
        raise ShareTransportError("response too large")
    chunks: list[bytes] = []
    size = 0
    while True:
        if monotonic() > deadline_s:
            raise ShareTransportError("response deadline exceeded")
        try:
            chunk = stream.read(CHUNK_BYTES)
        except (OSError, http.client.HTTPException, ValueError) as error:
            raise ShareTransportError(f"response read failed: {_reason(error)}") from None
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > max_bytes:
            raise ShareTransportError("response too large")
        chunks.append(chunk)


def urllib_poster(*, context: ssl.SSLContext, user_agent: str, max_bytes: int,
                  monotonic: Callable[[], float]) -> Poster:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                         urllib.request.HTTPSHandler(context=context),
                                         _RefuseRedirects())
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "Accept-Encoding": "identity", "User-Agent": user_agent}

    def post(url: str, body: bytes, timeout_s: float) -> HttpResponse:
        deadline_s = monotonic() + timeout_s
        request = urllib.request.Request(url, data=body, method="POST", headers=headers)
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
            data = _read_bounded(response, response.headers, deadline_s, max_bytes,
                                 monotonic) if has_body else b""
            retry_after = response.headers.get("Retry-After") if response.headers else None
        finally:
            if has_body:
                response.close()
        return HttpResponse(status, retry_after, data)

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
        return parse_entries(self.read_raw(minutes, max_count), max_count)

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
        log.info("Dexcom Share login succeeded")

    def _read(self, minutes: int, max_count: int) -> bytes:
        session_id = self._session_id
        if session_id is None:
            raise ShareProtocolError("no session")
        url = read_url(self._server.base_url, session_id, minutes, max_count)
        return self._call(url, b"{}", authenticating=False)

    def _call(self, url: str, body: bytes, *, authenticating: bool) -> bytes:
        response = self._post(url, body, self._timeout_s)
        if response.status == 200:
            self._store.succeeded()
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
        self.latest_trend: Trend | None = None

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
        self.latest_trend = entry.trend
        return Reading(entry.mg_dl, entry.timestamp)

    def _report(self, status: ShareStatus, detail: str) -> None:
        if status is not self.status:
            level = logging.INFO if status is ShareStatus.OK else logging.WARNING
            log.log(level, "Dexcom Share status %s: %s", status.value, detail)
        self.status = status
