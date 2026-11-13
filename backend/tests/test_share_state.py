import json
import stat
from dataclasses import asdict

import pytest
from share_harness import START, ShareClock, make_store

from glucose_guardian import share_state
from glucose_guardian.dexcom_share import (
    ShareAuthBlocked,
    ShareLoginDeferred,
    ShareRateLimited,
    ShareStateError,
)
from glucose_guardian.share_state import FIELDS, UNREADABLE, fresh_state, unblock


@pytest.fixture
def path(tmp_path):
    return tmp_path / "dexcom-share-state.json"


def test_login_attempt_is_recorded_before_any_request(path, share_clock):
    store = make_store(path, share_clock, "boot-a")
    assert not path.exists()
    store.begin_login(share_clock.boottime)
    data = json.loads(path.read_text())
    assert set(data) == FIELDS
    assert data["last_login_boottime_s"] == 1000.0 and data["boot_id"] == "boot-a"
    assert data["last_login_utc"] == START.isoformat()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not path.with_name(path.name + ".partial").exists()


def test_login_interval_holds_within_and_across_restarts(path, share_clock):
    make_store(path, share_clock, "boot-a").begin_login(share_clock.boottime)
    share_clock.advance(299)
    with pytest.raises(ShareLoginDeferred):
        make_store(path, share_clock, "boot-a").begin_login(share_clock.boottime)
    restarted = make_store(path, share_clock, "boot-a")
    with pytest.raises(ShareLoginDeferred):
        restarted.begin_login(share_clock.boottime)
    share_clock.advance(1)
    restarted.begin_login(share_clock.boottime)


def test_reboot_restarts_the_cooldown_from_the_new_boot(path, share_clock):
    make_store(path, share_clock, "boot-a").begin_login(share_clock.boottime)
    share_clock.boottime = 12.0
    rebooted = make_store(path, share_clock, "boot-b")
    with pytest.raises(ShareLoginDeferred):
        rebooted.begin_login(share_clock.boottime)
    share_clock.boottime = 311.0
    with pytest.raises(ShareLoginDeferred):
        rebooted.begin_login(share_clock.boottime)
    share_clock.boottime = 312.0
    rebooted.begin_login(share_clock.boottime)


def test_reboot_without_any_previous_login_allows_the_first_login(path, share_clock):
    store = make_store(path, share_clock, "boot-a")
    store.rate_limited(share_clock.boottime, None)
    share_clock.boottime = 5.0
    make_store(path, share_clock, "boot-b").begin_login(share_clock.boottime)


@pytest.mark.parametrize("boot_id", ["boot-a", "boot-b"])
def test_block_latch_survives_restart_and_reboot(path, share_clock, boot_id):
    store = make_store(path, share_clock, "boot-a")
    store.block("AccountPasswordInvalid")
    with pytest.raises(ShareAuthBlocked, match="AccountPasswordInvalid"):
        store.begin_login(share_clock.boottime)
    share_clock.advance(10**6)
    with pytest.raises(ShareAuthBlocked):
        make_store(path, share_clock, boot_id).check_ready(share_clock.boottime)
    assert json.loads(path.read_text())["blocked_reason"] == "AccountPasswordInvalid"


def test_unblock_keeps_the_login_interval(path, share_clock):
    store = make_store(path, share_clock, "boot-a")
    store.begin_login(share_clock.boottime)
    store.block("SSO_AuthenticateMaxAttemptsExceeded")
    share_clock.advance(10)
    state = unblock(path, "boot-a", share_clock.boottime, share_clock.wall)
    assert state.blocked_reason is None and state.last_login_boottime_s == 1000.0
    reloaded = make_store(path, share_clock, "boot-a")
    with pytest.raises(ShareLoginDeferred):
        reloaded.begin_login(share_clock.boottime)
    share_clock.advance(290)
    reloaded.begin_login(share_clock.boottime)


def test_rate_limit_backoff_doubles_honours_retry_after_and_resets(path, share_clock):
    store = make_store(path, share_clock, "boot-a")
    store.rate_limited(1000.0, None)
    with pytest.raises(ShareRateLimited):
        store.check_ready(1119.0)
    store.check_ready(1120.0)
    store.rate_limited(1120.0, None)
    assert store.state.rate_limit_backoff_s == 240.0
    with pytest.raises(ShareRateLimited):
        make_store(path, share_clock, "boot-a").check_ready(1359.0)
    store.rate_limited(1360.0, 3600.0)
    assert store.state.rate_limited_until_boottime_s == 1360.0 + 1800.0
    for _ in range(5):
        store.rate_limited(5000.0, None)
    assert store.state.rate_limit_backoff_s == 1800.0
    store.succeeded()
    store.check_ready(5000.0)
    assert json.loads(path.read_text())["rate_limit_backoff_s"] is None


def test_successful_requests_do_not_rewrite_unchanged_state(path, share_clock):
    store = make_store(path, share_clock, "boot-a")
    store.begin_login(share_clock.boottime)
    modified = path.stat().st_mtime_ns
    path.chmod(0o400)
    store.succeeded()
    assert path.stat().st_mtime_ns == modified


@pytest.mark.parametrize("content", [
    "", "{", "[]", "null",
    json.dumps(asdict(fresh_state("boot-a")) | {"schema": "other"}),
    json.dumps(asdict(fresh_state("boot-a")) | {"extra": 1}),
    json.dumps(asdict(fresh_state("boot-a")) | {"last_login_boottime_s": "1000"}),
    json.dumps(asdict(fresh_state("boot-a")) | {"last_login_boottime_s": True}),
    json.dumps(asdict(fresh_state("boot-a")) | {"blocked_reason": 5}),
])
def test_unreadable_state_blocks_until_unblocked(path, share_clock, content):
    path.write_text(content)
    store = make_store(path, share_clock, "boot-a")
    with pytest.raises(ShareAuthBlocked, match=UNREADABLE):
        store.begin_login(share_clock.boottime)
    unblock(path, "boot-a", share_clock.boottime, share_clock.wall)
    recovered = make_store(path, share_clock, "boot-a")
    with pytest.raises(ShareLoginDeferred):
        recovered.begin_login(share_clock.boottime)
    share_clock.advance(300)
    recovered.begin_login(share_clock.boottime)


def test_unwritable_state_refuses_every_login(tmp_path, share_clock):
    store = make_store(tmp_path / "missing" / "state.json", share_clock, "boot-a")
    with pytest.raises(ShareStateError):
        store.begin_login(share_clock.boottime)
    share_clock.advance(10**4)
    with pytest.raises(ShareStateError):
        store.begin_login(share_clock.boottime)


def test_cli_reports_and_unblocks(path, share_clock, tmp_path, monkeypatch, capsys):
    boot_id = tmp_path / "boot_id"
    boot_id.write_text("boot-a\n")
    monkeypatch.setattr(share_state, "BOOT_ID", boot_id)
    assert share_state.main(["--state-file", str(path), "status"]) == share_state.EXIT_OK
    assert "no state file" in capsys.readouterr().out
    make_store(path, share_clock, "boot-a").block("AccountPasswordInvalid")
    assert share_state.main(["--state-file", str(path), "status"]) == share_state.EXIT_OK
    assert "blocked: AccountPasswordInvalid since" in capsys.readouterr().out
    assert share_state.main(["--state-file", str(path), "unblock"]) == share_state.EXIT_OK
    assert "blocked: no" in capsys.readouterr().out
    path.write_text("garbage")
    assert share_state.main(["--state-file", str(path), "status"]) == share_state.EXIT_FAILED
    assert "unreadable" in capsys.readouterr().err


def test_state_store_has_no_place_for_secrets():
    assert FIELDS == {"schema", "boot_id", "last_login_boottime_s", "last_login_utc",
                      "rate_limited_until_boottime_s", "rate_limit_backoff_s",
                      "blocked_reason", "blocked_utc"}


def test_share_clock_starts_at_the_synthetic_epoch():
    assert ShareClock().wall == START
