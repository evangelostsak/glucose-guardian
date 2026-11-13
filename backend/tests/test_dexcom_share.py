import json
from datetime import UTC, datetime, timedelta

import pytest

from glucose_guardian.dexcom_share import (
    NIL_UUID,
    SERVERS,
    ShareAuthBlocked,
    ShareProtocolError,
    ShareRateLimited,
    ShareServerError,
    ShareSessionExpired,
    ShareStatus,
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

MS = 1767225600000
AT = datetime(2026, 1, 1, tzinfo=UTC)
VALID_UUID = "5ad1c0de-0000-4000-8000-00000000a11d"


def entry(**changes: object) -> dict[str, object]:
    return {"WT": f"Date({MS})", "ST": f"Date({MS})", "DT": f"Date({MS}+0100)",
            "Value": 110, "Trend": "Flat"} | changes


def body(*entries: object) -> bytes:
    return json.dumps(list(entries)).encode()


def test_servers_match_the_open_source_clients():
    assert SERVERS == {
        "us": ("https://share2.dexcom.com/ShareWebServices/Services/",
               "d89443d2-327c-4a6f-89e5-496bbb0317db"),
        "ous": ("https://shareous1.dexcom.com/ShareWebServices/Services/",
                "d89443d2-327c-4a6f-89e5-496bbb0317db"),
        "jp": ("https://share.dexcom.jp/ShareWebServices/Services/",
               "d8665ade-9673-4e27-9ff6-92db4ce13d13"),
    }


def test_window_covers_the_maximum_reading_age():
    assert window(600) == (10, 3)
    assert window(1) == (1, 2)
    assert window(10**7) == (1440, 288)


def test_request_bodies_carry_only_the_publisher_login_keys():
    assert json.loads(authenticate_body("user", "secret", "app")) == {
        "accountName": "user", "password": "secret", "applicationId": "app"}
    assert json.loads(login_body("account", "secret", "app")) == {
        "accountId": "account", "password": "secret", "applicationId": "app"}


def test_read_url_carries_the_session_and_window_only():
    url = read_url("https://example.invalid/ShareWebServices/Services/", "session", 10, 3)
    assert url == ("https://example.invalid/ShareWebServices/Services/Publisher/"
                   "ReadPublisherLatestGlucoseValues?sessionId=session&minutes=10&maxCount=3")
    for minutes, count in ((0, 3), (1441, 3), (10, 0), (10, 289)):
        with pytest.raises(ValueError):
            read_url("https://example.invalid/", "session", minutes, count)


def test_entry_keeps_dexcom_timestamps_in_utc():
    (parsed,) = parse_entries(body(entry()), 3)
    assert parsed.timestamp == AT and parsed.timestamp.utcoffset() == timedelta(0)
    assert parsed.system_time == AT and parsed.display_time == AT
    assert (parsed.mg_dl, parsed.trend) == (110, Trend.FLAT)
    (legacy,) = parse_entries(body(entry(WT=f"/Date({MS})/")), 3)
    assert legacy.timestamp == AT


@pytest.mark.parametrize("value", [39, 40, 400, 401])
def test_dexcom_range_and_low_high_sentinels_pass_unchanged(value):
    assert parse_entries(body(entry(Value=value)), 3)[0].mg_dl == value


@pytest.mark.parametrize("change", [
    {"Value": 38}, {"Value": 402}, {"Value": True}, {"Value": 110.0}, {"Value": "110"},
    {"Value": None}, {"Trend": "Sideways"}, {"Trend": True}, {"Trend": 10}, {"Trend": -1},
    {"Trend": None}, {"WT": "2026-01-01T00:00:00Z"}, {"WT": "Date(abc)"},
    {"WT": "Date(1e3)"}, {"WT": "Date(9999999999999999)"}, {"WT": "Date(999999999999999)"},
    {"WT": None}, {"ST": "Date()"}, {"DT": "Date(1767225600000+01)"},
])
def test_malformed_entries_are_rejected(change):
    with pytest.raises(ShareProtocolError):
        parse_entries(body(entry(**change)), 3)


@pytest.mark.parametrize("item", [
    {key: value for key, value in entry().items() if key != "Trend"},
    entry(Extra=1), [], "entry", None,
])
def test_unknown_entry_structures_are_rejected(item):
    with pytest.raises(ShareProtocolError, match="exactly WT"):
        parse_entries(body(item), 3)


def test_trend_names_and_legacy_numbers():
    for number, trend in enumerate(Trend):
        assert parse_entries(body(entry(Trend=trend.value)), 3)[0].trend is trend
        assert parse_entries(body(entry(Trend=number)), 3)[0].trend is trend


@pytest.mark.parametrize("raw,message", [
    (b'{"Value": 110}', "not a list"), (b"not json", "not JSON"), (b"\xff", "not JSON"),
    (body(entry(), entry(), entry(), entry()), "more entries than requested"),
])
def test_unsafe_response_structures_are_rejected(raw, message):
    with pytest.raises(ShareProtocolError, match=message):
        parse_entries(raw, 3)


def test_empty_response_is_no_reading():
    assert parse_entries(b"[]", 3) == []
    assert newest([]) is None


def test_newest_reading_wins_regardless_of_order_and_duplicates_collapse():
    old, mid, new = (entry(WT=f"Date({MS + offset})", Value=value)
                     for offset, value in ((0, 100), (300000, 105), (600000, 110)))
    picked = newest(parse_entries(body(old, new, mid), 3))
    assert picked is not None and picked.mg_dl == 110
    assert picked.timestamp == AT + timedelta(minutes=10)
    same = newest(parse_entries(body(new, new, old), 3))
    assert same is not None and same.mg_dl == 110


def test_conflicting_duplicates_are_rejected():
    with pytest.raises(ShareProtocolError, match="disagree"):
        newest(parse_entries(body(entry(Value=100), entry(Value=101)), 3))


def test_identifiers_must_be_non_nil_uuids():
    assert parse_identifier(json.dumps(VALID_UUID).encode()) == VALID_UUID
    with pytest.raises(ShareAuthBlocked) as blocked:
        parse_identifier(json.dumps(NIL_UUID).encode())
    assert blocked.value.reason == "NilIdentifier"
    for raw in (b'"not-a-uuid"', b"123", b"null", b"<html>", b""):
        with pytest.raises(ShareProtocolError):
            parse_identifier(raw)


def error_body(code: str, message: str) -> bytes:
    return json.dumps({"Code": code, "Message": message}).encode()


@pytest.mark.parametrize("status,raw,authenticating,expected,text", [
    (500, error_body("SessionNotValid", ""), False, ShareSessionExpired, "SessionNotValid"),
    (500, error_body("SessionIdNotFound", ""), False, ShareSessionExpired, "SessionIdNotFound"),
    (500, error_body("SessionNotValid", ""), True, ShareServerError, "HTTP 500 SessionNotValid"),
    (500, error_body("AccountPasswordInvalid", ""), True, ShareAuthBlocked,
     "AccountPasswordInvalid"),
    (500, error_body("SSO_AuthenticateMaxAttemptsExceeded", ""), True, ShareAuthBlocked,
     "SSO_AuthenticateMaxAttemptsExceeded"),
    (500, error_body("SSO_InternalError", "Cannot Authenticate by AccountName user"), True,
     ShareAuthBlocked, "SSO_InternalError"),
    (500, error_body("SSO_InternalError", "Cannot Authenticate by AccountId x"), True,
     ShareAuthBlocked, "SSO_InternalError"),
    (500, error_body("SSO_InternalError", "temporary"), True, ShareServerError,
     "HTTP 500 SSO_InternalError"),
    (500, error_body("InvalidArgument", "accountName"), True, ShareAuthBlocked,
     "InvalidArgument"),
    (500, error_body("InvalidArgument", "minutes"), False, ShareServerError,
     "HTTP 500 InvalidArgument"),
    (500, b"<html>error</html>", False, ShareServerError, "HTTP 500 without code"),
    (503, error_body("weird code / user@example", ""), False, ShareServerError,
     "HTTP 503 unrecognized"),
    (403, b"", True, ShareServerError, "HTTP 403 without code"),
])
def test_errors_are_classified(status, raw, authenticating, expected, text):
    error = classify_error(status, raw, authenticating=authenticating, retry_after=None)
    assert type(error) is expected and text in str(error)


def test_rate_limit_reads_retry_after_seconds_only():
    limited = classify_error(429, b"", authenticating=False, retry_after=" 30 ")
    assert isinstance(limited, ShareRateLimited) and limited.retry_after_s == 30.0
    for header in (None, "Wed, 21 Oct 2015 07:28:00 GMT", "-5", "soon"):
        limited = classify_error(429, b"", authenticating=True, retry_after=header)
        assert isinstance(limited, ShareRateLimited) and limited.retry_after_s is None


def test_error_text_never_echoes_server_messages():
    secret = "publisher@example.invalid hunter2"
    for code in ("AccountPasswordInvalid", "SSO_InternalError", "InvalidArgument",
                 "SessionNotValid", "Unknown"):
        for authenticating in (True, False):
            error = classify_error(500, error_body(code, f"Cannot Authenticate by AccountName "
                                                         f"{secret}"),
                                   authenticating=authenticating, retry_after=None)
            assert "publisher@example.invalid" not in str(error)
            assert "hunter2" not in str(error)


def test_each_error_maps_to_a_display_status():
    assert ShareAuthBlocked("x").status is ShareStatus.AUTH_BLOCKED
    assert ShareRateLimited("x", None).status is ShareStatus.RATE_LIMITED
    assert ShareProtocolError("x").status is ShareStatus.PROTOCOL_ERROR
    assert ShareServerError("x").status is ShareStatus.SERVER_ERROR
