import socket
import ssl
import subprocess
import threading
import time
from collections.abc import Callable, Iterator

import pytest

from glucose_guardian.dexcom_share import ShareTransportError
from glucose_guardian.share_client import MAX_RESPONSE_BYTES, urllib_poster

TIMEOUT_S = 0.5
DRIP_INTERVAL_S = 0.05
DRIP_LIMIT_S = 3.0
SLACK_S = 0.4
SLOW_RESOLVER_S = 0.8
JSON_HEAD = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
PADDED_EMPTY_LIST = b"[" + b" " * 200 + b"]"

Script = Callable[[socket.socket], None]


def read_request(connection: socket.socket) -> None:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = connection.recv(4096)
        if not chunk:
            return
        data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    lengths = [int(line.split(b":", 1)[1]) for line in head.split(b"\r\n")
               if line.lower().startswith(b"content-length:")]
    while lengths and len(body) < lengths[0]:
        chunk = connection.recv(4096)
        if not chunk:
            return
        body += chunk


def drip_then_flush(connection: socket.socket, data: bytes) -> None:
    ends = time.monotonic() + DRIP_LIMIT_S
    sent = 0
    while sent < len(data) and time.monotonic() < ends:
        connection.sendall(data[sent:sent + 1])
        sent += 1
        time.sleep(DRIP_INTERVAL_S)
    connection.sendall(data[sent:])


def drip_for(connection: socket.socket, unit: bytes) -> None:
    ends = time.monotonic() + DRIP_LIMIT_S
    while time.monotonic() < ends:
        connection.sendall(unit)
        time.sleep(DRIP_INTERVAL_S)


def headers_drip(connection: socket.socket) -> None:
    read_request(connection)
    connection.sendall(JSON_HEAD + b"X-Drip: ")
    drip_for(connection, b"a")
    connection.sendall(b"\r\nContent-Length: 2\r\n\r\n[]")


def body_drip_with_length(connection: socket.socket) -> None:
    read_request(connection)
    connection.sendall(JSON_HEAD + b"Content-Length: %d\r\n\r\n" % len(PADDED_EMPTY_LIST))
    drip_then_flush(connection, PADDED_EMPTY_LIST)


def body_drip_chunked(connection: socket.socket) -> None:
    read_request(connection)
    connection.sendall(JSON_HEAD + b"Transfer-Encoding: chunked\r\n\r\n1\r\n[\r\n")
    drip_for(connection, b"1\r\n \r\n")
    connection.sendall(b"1\r\n]\r\n0\r\n\r\n")


def body_drip_until_close(connection: socket.socket) -> None:
    read_request(connection)
    connection.sendall(b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n\r\n")
    drip_then_flush(connection, PADDED_EMPTY_LIST)


def partial_body_then_stall(connection: socket.socket) -> None:
    read_request(connection)
    connection.sendall(JSON_HEAD + b"Content-Length: 100\r\n\r\n[ ")
    time.sleep(DRIP_LIMIT_S)


def partial_body_then_close(connection: socket.socket) -> None:
    read_request(connection)
    connection.sendall(JSON_HEAD + b"Content-Length: 100\r\n\r\n[ ")


def tls_handshake_drip(connection: socket.socket) -> None:
    connection.recv(4096)
    connection.sendall(b"\x16\x03\x03\x40\x00")
    drip_for(connection, b"\x00")


class ScriptServer:
    def __init__(self, script: Script, tls: ssl.SSLContext | None) -> None:
        self._script = script
        self._tls = tls
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port = self._listener.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                connection, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(connection,), daemon=True).start()

    def _handle(self, connection: socket.socket) -> None:
        connection.settimeout(DRIP_LIMIT_S + 2)
        try:
            if self._tls is not None:
                connection = self._tls.wrap_socket(connection, server_side=True)
            with connection:
                self._script(connection)
        except OSError:
            connection.close()

    def close(self) -> None:
        self._listener.close()


@pytest.fixture
def serve() -> Iterator[Callable[[Script], ScriptServer]]:
    servers: list[ScriptServer] = []

    def start(script: Script) -> ScriptServer:
        servers.append(ScriptServer(script, None))
        return servers[-1]

    yield start
    for server in servers:
        server.close()


def poster(context: ssl.SSLContext):
    return urllib_poster(context=context, user_agent="tests", max_bytes=MAX_RESPONSE_BYTES,
                         monotonic=time.monotonic)


def timed_failure(url: str) -> tuple[ShareTransportError, float]:
    return timed_failure_with(url, ssl.create_default_context())


def timed_failure_with(url: str, context: ssl.SSLContext) -> tuple[ShareTransportError, float]:
    post = poster(context)
    started = time.monotonic()
    with pytest.raises(ShareTransportError) as failure:
        post(url, b"{}", TIMEOUT_S)
    return failure.value, time.monotonic() - started


def no_deadline_timers() -> bool:
    return not any(isinstance(thread, threading.Timer) for thread in threading.enumerate())


@pytest.mark.parametrize("script", [headers_drip, body_drip_with_length, body_drip_chunked,
                                    body_drip_until_close, partial_body_then_stall])
def test_slow_drip_responses_end_at_the_request_deadline(serve, script):
    server = serve(script)
    error, elapsed = timed_failure(f"http://127.0.0.1:{server.port}/x")
    assert "deadline exceeded" in str(error)
    assert TIMEOUT_S - 0.05 <= elapsed < TIMEOUT_S + SLACK_S
    assert no_deadline_timers()


def test_slow_tls_handshake_ends_at_the_request_deadline(serve):
    server = serve(tls_handshake_drip)
    error, elapsed = timed_failure(f"https://127.0.0.1:{server.port}/x")
    assert "deadline exceeded" in str(error)
    assert elapsed < TIMEOUT_S + SLACK_S
    assert no_deadline_timers()


@pytest.fixture(scope="module")
def certificate(tmp_path_factory) -> tuple[str, str]:
    directory = tmp_path_factory.mktemp("tls")
    key, cert = directory / "key.pem", directory / "cert.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
                    "-keyout", str(key), "-out", str(cert)],
                   check=True, capture_output=True, timeout=60)
    return str(cert), str(key)


@pytest.mark.parametrize("script", [headers_drip, body_drip_with_length, body_drip_chunked])
def test_slow_drip_over_verified_tls_ends_at_the_request_deadline(certificate, script):
    cert, key = certificate
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert, key)
    server = ScriptServer(script, server_context)
    try:
        error, elapsed = timed_failure_with(f"https://127.0.0.1:{server.port}/x",
                                            ssl.create_default_context(cafile=cert))
    finally:
        server.close()
    assert "deadline exceeded" in str(error)
    assert TIMEOUT_S - 0.05 <= elapsed < TIMEOUT_S + SLACK_S
    assert no_deadline_timers()


def test_verified_tls_response_within_the_deadline_is_returned(certificate):
    cert, key = certificate
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert, key)

    def answer(connection: socket.socket) -> None:
        read_request(connection)
        connection.sendall(JSON_HEAD + b"Content-Length: 2\r\n\r\n[]")

    server = ScriptServer(answer, server_context)
    try:
        response = poster(ssl.create_default_context(cafile=cert))(
            f"https://127.0.0.1:{server.port}/x", b"{}", TIMEOUT_S * 4)
    finally:
        server.close()
    assert (response.status, response.body) == (200, b"[]")
    assert no_deadline_timers()


def test_partial_body_then_close_is_a_transport_error(serve):
    server = serve(partial_body_then_close)
    error, elapsed = timed_failure(f"http://127.0.0.1:{server.port}/x")
    assert "IncompleteRead" in str(error)
    assert elapsed < TIMEOUT_S


@pytest.fixture
def unanswered_port() -> Iterator[int]:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(0)
    port = listener.getsockname()[1]
    held = socket.create_connection(("127.0.0.1", port), timeout=1.0)
    yield port
    held.close()
    listener.close()


def test_connection_delay_ends_at_the_request_deadline(unanswered_port):
    error, elapsed = timed_failure(f"http://127.0.0.1:{unanswered_port}/x")
    assert "deadline exceeded" in str(error)
    assert TIMEOUT_S - 0.05 <= elapsed < TIMEOUT_S + SLACK_S


def test_several_unanswered_addresses_share_one_deadline(unanswered_port, monkeypatch):
    address = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
               ("127.0.0.1", unanswered_port))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [address] * 3)
    error, elapsed = timed_failure("http://share.invalid:80/x")
    assert "deadline exceeded" in str(error)
    assert elapsed < TIMEOUT_S + SLACK_S


def test_slow_name_resolution_is_not_interrupted_but_fails_the_request(simulator,
                                                                       monkeypatch):
    resolve = socket.getaddrinfo

    def slow(*args, **kwargs):
        time.sleep(SLOW_RESOLVER_S)
        return resolve(*args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", slow)
    error, elapsed = timed_failure(simulator.base_url + "General/AuthenticatePublisherAccount")
    assert "deadline exceeded" in str(error)
    assert SLOW_RESOLVER_S <= elapsed < SLOW_RESOLVER_S + SLACK_S
    assert no_deadline_timers()


def test_requests_within_the_deadline_leave_no_timer_behind(simulator):
    post = poster(ssl.create_default_context())
    for _ in range(3):
        response = post(simulator.base_url + "General/AuthenticatePublisherAccount",
                        b'{"accountName": "synthetic-publisher", "password": '
                        b'"synthetic-password", "applicationId": '
                        b'"d89443d2-327c-4a6f-89e5-496bbb0317db"}', TIMEOUT_S * 4)
        assert response.status == 200
    assert no_deadline_timers()
