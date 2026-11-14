import ast
import inspect
import json
import ssl
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from share_harness import CREDENTIALS, ShareClock, make_store

from glucose_guardian.dexcom_share import (
    APPLICATION_ID,
    APPROVED_BASES,
    AUTHENTICATE,
    LOGIN,
    READ,
    SERVERS,
    SERVICES_PATH,
    ShareRequestRefused,
    ShareTransportError,
    authenticate_body,
    check_request,
    login_body,
    read_url,
)
from glucose_guardian.share_client import (
    MAX_RESPONSE_BYTES,
    HttpResponse,
    ShareServer,
    ShareSession,
    urllib_poster,
)

SOURCE = Path(__file__).resolve().parents[1] / "src" / "glucose_guardian"
SIMULATOR_BASE = f"http://127.0.0.1:8765{SERVICES_PATH}"
ACCOUNT = "5ad1c0de-0000-4000-8000-00000000a11d"
SESSION = "5e551011-0000-4000-8000-000000000001"
NETWORK_MODULES = {"urllib.request", "http.client", "socket", "ssl", "requests", "httpx",
                   "aiohttp", "asyncio", "ftplib", "smtplib", "telnetlib", "xmlrpc.client"}
OUTSIDE_THE_ALLOWLIST = [
    "Publisher/PostReceiverEgvRecords", "Publisher/CreateSubscriptionInvitation",
    "Publisher/DeleteContact", "Publisher/UpdatePublisherAccountRuntimeInfo",
    "Publisher/StartRemoteMonitoringSession", "Publisher/ListPublisherAccountSubscriptions",
    "General/UpdatePublisherAccount", "General/AuthenticatePublisherAccount/",
    "general/authenticatepublisheraccount", READ + "/../../General/UpdatePublisherAccount",
    "../Other", "", "General/", AUTHENTICATE + "#fragment", AUTHENTICATE + "%2F",
]


def approved_requests(base_url: str) -> list[tuple[str, bytes]]:
    return [(base_url + AUTHENTICATE, authenticate_body("user", "secret", APPLICATION_ID)),
            (base_url + LOGIN, login_body(ACCOUNT, "secret", APPLICATION_ID)),
            (read_url(base_url, SESSION, 10, 3), b"{}")]


@pytest.mark.parametrize("base_url", sorted(APPROVED_BASES) + [SIMULATOR_BASE])
def test_the_three_read_only_operations_are_approved(base_url):
    for url, body in approved_requests(base_url):
        check_request(base_url, url, body)


def test_approved_servers_are_exactly_the_dexcom_share_hosts():
    assert APPROVED_BASES == {base for base, _ in SERVERS.values()} == {
        "https://share2.dexcom.com/ShareWebServices/Services/",
        "https://shareous1.dexcom.com/ShareWebServices/Services/",
        "https://share.dexcom.jp/ShareWebServices/Services/",
    }


@pytest.mark.parametrize("endpoint", OUTSIDE_THE_ALLOWLIST)
def test_endpoints_outside_the_allowlist_are_refused(endpoint):
    base_url = SERVERS["ous"][0]
    for body in (b"{}", authenticate_body("user", "secret", APPLICATION_ID)):
        with pytest.raises(ShareRequestRefused, match="not an approved read-only operation"):
            check_request(base_url, base_url + endpoint, body)


@pytest.mark.parametrize("base_url", [
    "http://shareous1.dexcom.com/ShareWebServices/Services/",
    "https://shareous1.dexcom.com.example.invalid/ShareWebServices/Services/",
    "https://shareous1.dexcom.com@example.invalid/ShareWebServices/Services/",
    "https://shareous1.dexcom.com:8443/ShareWebServices/Services/",
    "https://shareous1.dexcom.com/ShareWebServices/",
    "https://example.invalid/ShareWebServices/Services/",
    "http://localhost:8765/ShareWebServices/Services/",
    "http://127.0.0.2:8765/ShareWebServices/Services/",
    "https://127.0.0.1:8765/ShareWebServices/Services/",
    "http://127.0.0.1:8765/ShareWebServices/Services/x/",
])
def test_servers_outside_the_allowlist_are_refused(base_url):
    for url, body in approved_requests(base_url):
        with pytest.raises(ShareRequestRefused, match="not an approved Dexcom Share server"):
            check_request(base_url, url, body)


def test_requests_to_another_host_are_refused_even_with_an_approved_base():
    base_url = SERVERS["ous"][0]
    for url, body in approved_requests("https://example.invalid/ShareWebServices/Services/"):
        with pytest.raises(ShareRequestRefused):
            check_request(base_url, url, body)


@pytest.mark.parametrize("url_tail", [
    AUTHENTICATE + "?accountName=user", AUTHENTICATE + "?", LOGIN + "?sessionId=" + SESSION,
    READ, READ + "?", READ + f"?sessionId={SESSION}&minutes=10",
    READ + f"?sessionId={SESSION}&minutes=10&maxCount=3&delete=1",
    READ + f"?minutes=10&sessionId={SESSION}&maxCount=3",
    READ + f"?sessionId={SESSION}&sessionId={SESSION}&minutes=10&maxCount=3",
    READ + "?sessionId=not-a-session&minutes=10&maxCount=3",
    READ + f"?sessionId={SESSION}&minutes=0&maxCount=3",
    READ + f"?sessionId={SESSION}&minutes=1441&maxCount=3",
    READ + f"?sessionId={SESSION}&minutes=10&maxCount=289",
    READ + f"?sessionId={SESSION}&minutes=1e3&maxCount=3",
    READ + f"?sessionId={SESSION}&minutes=%C2%B3&maxCount=3",
    READ + f"?sessionId={SESSION}&minutes=10&maxCount=3#fragment",
])
def test_unexpected_queries_are_refused(url_tail):
    base_url = SERVERS["ous"][0]
    body = b"{}" if url_tail.startswith(READ) else authenticate_body("u", "p", APPLICATION_ID)
    with pytest.raises(ShareRequestRefused, match="unexpected query"):
        check_request(base_url, base_url + url_tail, body)


@pytest.mark.parametrize("endpoint,body", [
    (AUTHENTICATE, json.dumps({"accountName": "u", "password": "p", "applicationId": "a",
                               "newPassword": "q"}).encode()),
    (AUTHENTICATE, json.dumps({"accountName": "u", "password": "p"}).encode()),
    (AUTHENTICATE, json.dumps({"accountName": "u", "password": 1, "applicationId": "a"}).encode()),
    (AUTHENTICATE, login_body(ACCOUNT, "p", APPLICATION_ID)),
    (LOGIN, authenticate_body("u", "p", APPLICATION_ID)),
    (LOGIN, b"[]"),
    (LOGIN, b"not json"),
    (LOGIN, b"\xff"),
    (READ, b'{"sessionId": "x"}'),
    (READ, b"[]"),
    (READ, b""),
])
def test_unexpected_bodies_are_refused(endpoint, body):
    base_url = SERVERS["ous"][0]
    url = read_url(base_url, SESSION, 10, 3) if endpoint == READ else base_url + endpoint
    with pytest.raises(ShareRequestRefused, match="unexpected body"):
        check_request(base_url, url, body)


class Recording:
    def __init__(self) -> None:
        self.sent: list[tuple[str, bytes]] = []

    def __call__(self, url: str, body: bytes, timeout_s: float) -> HttpResponse:
        self.sent.append((url, body))
        if url.endswith(AUTHENTICATE):
            return HttpResponse(200, None, json.dumps(ACCOUNT).encode())
        if url.endswith(LOGIN):
            return HttpResponse(200, None, json.dumps(SESSION).encode())
        return HttpResponse(200, None, b"[]")


def session_with(base_url: str, post: Recording, tmp_path: Path) -> ShareSession:
    clock = ShareClock()
    return ShareSession(server=ShareServer(base_url, APPLICATION_ID), credentials=CREDENTIALS,
                        post=post, store=make_store(tmp_path / "state.json", clock, "boot-a"),
                        request_timeout_s=2.0, boottime=lambda: clock.boottime)


def test_a_session_for_an_unapproved_server_sends_nothing(tmp_path):
    post = Recording()
    session = session_with("https://example.invalid/ShareWebServices/Services/", post, tmp_path)
    with pytest.raises(ShareRequestRefused):
        session.read(10, 3)
    assert post.sent == []
    assert json.loads((tmp_path / "state.json").read_text())["login_pending"] is False


def test_a_session_refuses_any_other_operation_before_sending(tmp_path):
    post = Recording()
    session = session_with(SERVERS["ous"][0], post, tmp_path)
    assert session.read(10, 3) == []
    sent = list(post.sent)
    for endpoint in OUTSIDE_THE_ALLOWLIST:
        for authenticating in (True, False):
            with pytest.raises(ShareRequestRefused):
                session._call(SERVERS["ous"][0] + endpoint, b"{}",
                              authenticating=authenticating)
    assert post.sent == sent
    assert [url.split("?")[0].removeprefix(SERVERS["ous"][0]) for url, _ in sent] == [
        AUTHENTICATE, LOGIN, READ]


class WireRecorder:
    def __init__(self, redirect_status: int | None) -> None:
        self.calls: list[tuple[str, str]] = []
        recorder = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, format, *args):
                return None

            def _record(self):
                length = self.headers.get("Content-Length")
                if length is not None:
                    self.rfile.read(int(length))
                recorder.calls.append((self.command, self.path))
                if redirect_status is not None:
                    self.send_response(redirect_status)
                    self.send_header("Location", SERVICES_PATH + "Publisher/DeleteContact")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                path = self.path.split("?")[0]
                body = {SERVICES_PATH + AUTHENTICATE: json.dumps(ACCOUNT),
                        SERVICES_PATH + LOGIN: json.dumps(SESSION),
                        SERVICES_PATH + READ: "[]"}[path].encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _record

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}{SERVICES_PATH}"


@pytest.fixture
def wire() -> Iterator[list[WireRecorder]]:
    servers: list[WireRecorder] = []
    yield servers
    for recorder in servers:
        recorder.server.shutdown()
        recorder.server.server_close()


def real_session(base_url: str, tmp_path: Path) -> ShareSession:
    clock = ShareClock()
    post = urllib_poster(context=ssl.create_default_context(), user_agent="tests",
                         max_bytes=MAX_RESPONSE_BYTES, monotonic=time.monotonic)
    return ShareSession(server=ShareServer(base_url, APPLICATION_ID), credentials=CREDENTIALS,
                        post=post, store=make_store(tmp_path / "state.json", clock, "boot-a"),
                        request_timeout_s=2.0, boottime=lambda: clock.boottime)


def test_only_post_reaches_the_wire_and_only_for_the_three_operations(wire, tmp_path):
    wire.append(WireRecorder(None))
    session = real_session(wire[0].base_url, tmp_path)
    assert session.read(10, 3) == []
    assert session.read(10, 3) == []
    assert [(method, path.split("?")[0]) for method, path in wire[0].calls] == [
        ("POST", SERVICES_PATH + AUTHENTICATE), ("POST", SERVICES_PATH + LOGIN),
        ("POST", SERVICES_PATH + READ), ("POST", SERVICES_PATH + READ)]


def test_the_poster_has_no_method_or_header_parameter():
    post = urllib_poster(context=ssl.create_default_context(), user_agent="tests",
                         max_bytes=MAX_RESPONSE_BYTES, monotonic=time.monotonic)
    assert list(inspect.signature(post).parameters) == ["url", "body", "timeout_s"]


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirects_are_never_followed(wire, tmp_path, status):
    wire.append(WireRecorder(status))
    session = real_session(wire[0].base_url, tmp_path)
    with pytest.raises(ShareTransportError, match="redirect refused"):
        session.read(10, 3)
    assert wire[0].calls == [("POST", SERVICES_PATH + AUTHENTICATE)]


def network_imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            found.add(node.module)
    return {name for name in found
            if any(name == module or name.startswith(module + ".")
                   for module in NETWORK_MODULES)}


def test_only_the_share_client_can_open_network_connections():
    users = {str(path.relative_to(SOURCE)): network_imports(path)
             for path in sorted(SOURCE.rglob("*.py")) if network_imports(path)}
    assert users == {"share_client.py": {"http.client", "socket", "ssl", "urllib.request"},
                     "service.py": {"ssl"}, "notify.py": {"socket"}}


def test_the_systemd_notifier_uses_only_unix_sockets():
    tree = ast.parse((SOURCE / "notify.py").read_text())
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
                  and isinstance(node.value, ast.Name) and node.value.id == "socket"}
    assert attributes == {"socket", "AF_UNIX", "SOCK_DGRAM", "SOCK_CLOEXEC"}

