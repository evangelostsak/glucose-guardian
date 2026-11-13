import json
import logging
import ssl
import subprocess
import threading
import time
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from share_harness import (
    CREDENTIALS,
    START,
    StateWrites,
    make_session,
    make_store,
    synthetic,
)

from glucose_guardian.credentials import Credentials
from glucose_guardian.dexcom_share import (
    ShareAuthBlocked,
    ShareLoginDeferred,
    ShareProtocolError,
    ShareRateLimited,
    ShareServerError,
    ShareTransportError,
)
from glucose_guardian.share_client import MAX_RESPONSE_BYTES, HttpResponse, urllib_poster
from glucose_guardian.share_simulator import (
    SYNTHETIC_ACCOUNT_ID,
    SYNTHETIC_PASSWORD,
    SYNTHETIC_USERNAME,
    Fault,
    share_entry,
)
from glucose_guardian.share_state import LOGIN_UNKNOWN


@pytest.fixture
def state_file(tmp_path):
    return tmp_path / "state.json"


def session_for(simulator, state_file, clock, *, timeout_s, credentials):
    store = make_store(state_file, clock, "boot-a")
    return make_session(simulator, store, clock, request_timeout_s=timeout_s,
                        credentials=credentials)


def counts(simulator):
    with simulator.lock:
        return (simulator.requests["authenticate"], simulator.requests["login"],
                simulator.requests["read"])


def test_login_then_read_reuses_the_session(simulator, state_file, share_clock):
    session = session_for(simulator, state_file, share_clock, timeout_s=2.0,
                          credentials=CREDENTIALS)
    (entry,) = session.read(10, 3)
    assert entry.mg_dl == 110
    assert counts(simulator) == (1, 1, 1)
    share_clock.advance(60)
    session.read(10, 3)
    assert counts(simulator) == (1, 1, 2)


def test_expired_session_renews_once_without_reauthenticating(simulator, state_file,
                                                              share_clock):
    session = session_for(simulator, state_file, share_clock, timeout_s=2.0,
                          credentials=CREDENTIALS)
    session.read(10, 3)
    share_clock.advance(400)
    simulator.expire_sessions()
    assert session.read(10, 3)[0].mg_dl == 110
    assert counts(simulator) == (1, 2, 3)


def test_expired_session_within_the_login_interval_is_deferred(simulator, state_file,
                                                               share_clock):
    session = session_for(simulator, state_file, share_clock, timeout_s=2.0,
                          credentials=CREDENTIALS)
    session.read(10, 3)
    share_clock.advance(60)
    simulator.expire_sessions()
    with pytest.raises(ShareLoginDeferred):
        session.read(10, 3)
    assert counts(simulator) == (1, 1, 2)
    share_clock.advance(240)
    session.read(10, 3)
    assert counts(simulator) == (1, 2, 3)


@pytest.mark.parametrize("password,locked_out,reason", [
    ("not-the-password", False, "AccountPasswordInvalid"),
    (SYNTHETIC_PASSWORD, True, "SSO_AuthenticateMaxAttemptsExceeded"),
])
def test_rejected_credentials_latch_without_further_requests(simulator, state_file,
                                                             share_clock, password,
                                                             locked_out, reason):
    simulator.password = password
    simulator.locked_out = locked_out
    session = session_for(simulator, state_file, share_clock, timeout_s=2.0,
                          credentials=CREDENTIALS)
    with pytest.raises(ShareAuthBlocked, match=reason):
        session.read(10, 3)
    assert counts(simulator) == (1, 0, 0)
    share_clock.advance(10**5)
    with pytest.raises(ShareAuthBlocked):
        session.read(10, 3)
    with pytest.raises(ShareAuthBlocked):
        session_for(simulator, state_file, share_clock, timeout_s=2.0,
                    credentials=CREDENTIALS).read(10, 3)
    assert counts(simulator) == (1, 0, 0)
    assert json.loads(state_file.read_text())["blocked_reason"] == reason


def test_password_rejected_at_session_renewal_latches(simulator, state_file, share_clock):
    session = session_for(simulator, state_file, share_clock, timeout_s=2.0,
                          credentials=CREDENTIALS)
    session.read(10, 3)
    simulator.password = "rotated-elsewhere"
    simulator.expire_sessions()
    share_clock.advance(400)
    with pytest.raises(ShareAuthBlocked, match="AccountPasswordInvalid"):
        session.read(10, 3)
    assert counts(simulator) == (1, 2, 2)


def test_rate_limit_backs_off_without_requests(simulator, state_file, share_clock):
    session = session_for(simulator, state_file, share_clock, timeout_s=2.0,
                          credentials=CREDENTIALS)
    session.read(10, 3)
    simulator.retry_after = "200"
    simulator.set_fault(Fault.RATE_LIMIT, "read")
    with pytest.raises(ShareRateLimited):
        session.read(10, 3)
    share_clock.advance(199)
    with pytest.raises(ShareRateLimited):
        session.read(10, 3)
    assert counts(simulator) == (1, 1, 2)
    simulator.set_fault(None, "read")
    share_clock.advance(1)
    assert session.read(10, 3)[0].mg_dl == 110
    assert json.loads(state_file.read_text())["rate_limit_backoff_s"] is None


def test_rate_limited_login_still_counts_as_a_login_attempt(simulator, state_file,
                                                            share_clock):
    simulator.set_fault(Fault.RATE_LIMIT, "authenticate")
    session = session_for(simulator, state_file, share_clock, timeout_s=2.0,
                          credentials=CREDENTIALS)
    with pytest.raises(ShareRateLimited):
        session.read(10, 3)
    simulator.set_fault(None, "authenticate")
    share_clock.advance(200)
    with pytest.raises(ShareLoginDeferred):
        session.read(10, 3)
    share_clock.advance(100)
    session.read(10, 3)
    assert counts(simulator) == (2, 1, 1)


def persisted(state_file, key):
    return json.loads(state_file.read_text())[key]


def recent(simulator):
    simulator.source = lambda now: [synthetic(110, now - timedelta(seconds=60), "Flat")]


def raw_entry(mg_dl, **changes):
    return share_entry(synthetic(mg_dl, START - timedelta(seconds=60), "Flat")) | changes


@pytest.mark.parametrize("fault,raw_entries", [
    (Fault.MALFORMED, None),
    (None, [raw_entry(110, Value=500)]),
    (None, [raw_entry(110), raw_entry(111)]),
    (None, [raw_entry(110, WT="2026-01-01T00:00:00Z")]),
    (None, {"Value": 110}),
])
def test_invalid_200_reads_keep_the_rate_limit_backoff(simulator, state_file, share_clock,
                                                       fault, raw_entries):
    recent(simulator)
    session = session_for(simulator, state_file, share_clock, timeout_s=2.0,
                          credentials=CREDENTIALS)
    session.read(10, 3)
    simulator.set_fault(Fault.RATE_LIMIT, "read")
    with pytest.raises(ShareRateLimited):
        session.read(10, 3)
    assert persisted(state_file, "rate_limit_backoff_s") == 120.0
    simulator.set_fault(fault, "read")
    simulator.raw_entries = raw_entries
    share_clock.advance(120)
    with pytest.raises(ShareProtocolError):
        session.read(10, 3)
    assert persisted(state_file, "rate_limit_backoff_s") == 120.0
    simulator.set_fault(Fault.RATE_LIMIT, "read")
    simulator.raw_entries = None
    with pytest.raises(ShareRateLimited):
        session.read(10, 3)
    assert persisted(state_file, "rate_limit_backoff_s") == 240.0
    simulator.set_fault(None, "read")
    share_clock.advance(240)
    assert session.read(10, 3)[0].mg_dl == 110
    assert persisted(state_file, "rate_limit_backoff_s") is None
    assert persisted(state_file, "rate_limited_until_boottime_s") is None
    assert counts(simulator) == (1, 1, 5)
    restarted = session_for(simulator, state_file, share_clock, timeout_s=2.0,
                            credentials=CREDENTIALS)
    assert restarted.read(10, 3)[0].mg_dl == 110
    assert counts(simulator) == (2, 2, 6)


def test_invalid_200_login_keeps_the_rate_limit_backoff(simulator, state_file, share_clock):
    recent(simulator)
    simulator.set_fault(Fault.RATE_LIMIT, "authenticate")
    session = session_for(simulator, state_file, share_clock, timeout_s=2.0,
                          credentials=CREDENTIALS)
    with pytest.raises(ShareRateLimited):
        session.read(10, 3)
    simulator.set_fault(Fault.MALFORMED, "authenticate")
    share_clock.advance(300)
    with pytest.raises(ShareProtocolError):
        session.read(10, 3)
    assert persisted(state_file, "rate_limit_backoff_s") == 120.0
    assert persisted(state_file, "login_pending") is False
    simulator.set_fault(None, "authenticate")
    share_clock.advance(300)
    assert session.read(10, 3)[0].mg_dl == 110
    assert persisted(state_file, "rate_limit_backoff_s") is None
    assert counts(simulator) == (3, 1, 1)


def test_block_that_cannot_be_written_sends_no_login_after_restart(simulator, state_file,
                                                                   share_clock, monkeypatch):
    writes = StateWrites(monkeypatch)
    simulator.password = "rotated-elsewhere"
    session = session_for(simulator, state_file, share_clock, timeout_s=2.0,
                          credentials=CREDENTIALS)
    writes.fail_after = writes.attempts + 1
    with pytest.raises(ShareAuthBlocked, match="AccountPasswordInvalid"):
        session.read(10, 3)
    assert writes.failures == 1 and counts(simulator) == (1, 0, 0)
    share_clock.advance(10**5)
    with pytest.raises(ShareAuthBlocked, match="AccountPasswordInvalid"):
        session.read(10, 3)
    writes.fail_after = None
    for _ in range(3):
        restarted = session_for(simulator, state_file, share_clock, timeout_s=2.0,
                                credentials=CREDENTIALS)
        share_clock.advance(10**5)
        with pytest.raises(ShareAuthBlocked, match=LOGIN_UNKNOWN):
            restarted.read(10, 3)
    assert counts(simulator) == (1, 0, 0)


@pytest.mark.parametrize("endpoint", ["authenticate", "login"])
@pytest.mark.parametrize("fault,error", [
    (Fault.SERVER_ERROR, ShareServerError), (Fault.HANG, ShareTransportError),
    (Fault.RATE_LIMIT, ShareRateLimited), (Fault.MALFORMED, ShareProtocolError),
])
def test_every_login_result_is_recorded_before_a_restart(simulator, state_file, share_clock,
                                                         endpoint, fault, error):
    recent(simulator)
    simulator.set_fault(fault, endpoint)
    session = session_for(simulator, state_file, share_clock, timeout_s=0.5,
                          credentials=CREDENTIALS)
    with pytest.raises(error):
        session.read(10, 3)
    assert persisted(state_file, "login_pending") is False
    simulator.set_fault(None, endpoint)
    share_clock.advance(1800)
    restarted = session_for(simulator, state_file, share_clock, timeout_s=2.0,
                            credentials=CREDENTIALS)
    assert restarted.read(10, 3)[0].mg_dl == 110
    assert persisted(state_file, "login_pending") is False


@pytest.mark.parametrize("fault,error,text", [
    (Fault.SERVER_ERROR, ShareServerError, "HTTP 500 ServiceUnavailable"),
    (Fault.HTML_ERROR, ShareServerError, "HTTP 500 without code"),
    (Fault.MALFORMED, ShareProtocolError, "not JSON"),
    (Fault.OVERSIZE, ShareTransportError, "too large"),
    (Fault.REDIRECT, ShareTransportError, "redirect refused"),
])
def test_http_failures_are_classified_and_recover(simulator, state_file, share_clock, fault,
                                                  error, text):
    session = session_for(simulator, state_file, share_clock, timeout_s=2.0,
                          credentials=CREDENTIALS)
    session.read(10, 3)
    simulator.set_fault(fault, "read")
    with pytest.raises(error, match=text):
        session.read(10, 3)
    simulator.set_fault(None, "read")
    assert session.read(10, 3)[0].mg_dl == 110
    assert counts(simulator) == (1, 1, 3)
    assert simulator.requests["/ShareWebServices/Services/Redirected"] == 0


@pytest.mark.parametrize("fault", [Fault.SLOW, Fault.HANG])
def test_slow_and_hung_responses_end_within_the_request_deadline(simulator, state_file,
                                                                 share_clock, fault):
    session = session_for(simulator, state_file, share_clock, timeout_s=0.5,
                          credentials=CREDENTIALS)
    session.read(10, 3)
    simulator.slow_s = 5.0
    simulator.set_fault(fault, "read")
    started = time.monotonic()
    with pytest.raises(ShareTransportError, match="deadline exceeded"):
        session.read(10, 3)
    assert time.monotonic() - started < 2.0


def test_refused_connection_is_a_transport_error(simulator, state_file, share_clock):
    session = session_for(simulator, state_file, share_clock, timeout_s=2.0,
                          credentials=CREDENTIALS)
    simulator.stop()
    with pytest.raises(ShareTransportError, match="ConnectionRefusedError"):
        session.read(10, 3)


def test_environment_proxies_are_ignored(simulator, state_file, share_clock, monkeypatch):
    for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    assert session_for(simulator, state_file, share_clock, timeout_s=2.0,
                       credentials=CREDENTIALS).read(10, 3)[0].mg_dl == 110


def test_untrusted_tls_certificate_is_refused(tmp_path):
    key, cert = tmp_path / "key.pem", tmp_path / "cert.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=127.0.0.1", "-keyout", str(key), "-out", str(cert)],
                   check=True, capture_output=True, timeout=60)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return None

        def do_POST(self):
            self.send_response(200)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        post = urllib_poster(context=ssl.create_default_context(), user_agent="tests",
                             max_bytes=MAX_RESPONSE_BYTES, monotonic=time.monotonic)
        with pytest.raises(ShareTransportError, match="SSLCertVerificationError"):
            post(f"https://127.0.0.1:{server.server_address[1]}/x", b"{}", 2.0)
    finally:
        server.shutdown()
        server.server_close()


def test_requests_put_credentials_only_in_bodies(simulator, state_file, share_clock):
    sent: list[tuple[str, bytes]] = []
    session = session_for(simulator, state_file, share_clock, timeout_s=2.0,
                          credentials=CREDENTIALS)
    real_post = session._post

    def recording(url: str, body: bytes, timeout_s: float) -> HttpResponse:
        sent.append((url, body))
        return real_post(url, body, timeout_s)

    session._post = recording
    session.read(10, 3)
    (auth_url, auth_body), (login_url, login_body), (read_url, read_body) = sent
    for url in (auth_url, login_url, read_url):
        assert SYNTHETIC_PASSWORD not in url and SYNTHETIC_USERNAME not in url
    assert json.loads(auth_body)["accountName"] == SYNTHETIC_USERNAME
    assert json.loads(login_body)["accountId"] == SYNTHETIC_ACCOUNT_ID
    assert read_body == b"{}" and "sessionId=" in read_url


def test_secrets_never_reach_logs_errors_or_state(simulator, state_file, share_clock,
                                                  caplog):
    caplog.set_level(logging.DEBUG)
    errors: list[str] = []
    session = session_for(simulator, state_file, share_clock, timeout_s=2.0,
                          credentials=CREDENTIALS)
    session.read(10, 3)
    with simulator.lock:
        sessions = set(simulator.sessions)
    simulator.set_fault(Fault.SERVER_ERROR, "read")
    with pytest.raises(ShareServerError) as server_error:
        session.read(10, 3)
    errors.append(str(server_error.value))
    simulator.set_fault(None, "read")
    simulator.password = "rotated-password"
    simulator.expire_sessions()
    share_clock.advance(400)
    with pytest.raises(ShareAuthBlocked) as blocked:
        session.read(10, 3)
    errors.append(str(blocked.value))
    wrong = Credentials(SYNTHETIC_USERNAME, "typo-password")
    with pytest.raises(ShareAuthBlocked) as again:
        session_for(simulator, state_file, share_clock, timeout_s=2.0,
                    credentials=wrong).read(10, 3)
    errors.append(str(again.value))
    text = "\n".join([caplog.text, state_file.read_text(), *errors])
    for secret in (SYNTHETIC_USERNAME, SYNTHETIC_PASSWORD, "typo-password",
                   "rotated-password", SYNTHETIC_ACCOUNT_ID, *sessions):
        assert secret not in text
    assert "AccountPasswordInvalid" in text


@pytest.mark.parametrize("declared,sent", [(None, 70000), ("10000000", 10)])
def test_body_size_is_capped_with_and_without_a_declared_length(declared, sent):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, format, *args):
            return None

        def do_POST(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            if declared is not None:
                self.send_header("Content-Length", declared)
            self.end_headers()
            self.wfile.write(b"[" + b" " * sent)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        post = urllib_poster(context=ssl.create_default_context(), user_agent="tests",
                             max_bytes=MAX_RESPONSE_BYTES, monotonic=time.monotonic)
        with pytest.raises(ShareTransportError, match="too large"):
            post(f"http://127.0.0.1:{server.server_address[1]}/x", b"{}", 2.0)
    finally:
        server.shutdown()
        server.server_close()
