from datetime import datetime, timedelta, timezone

import pytest

from glucose_guardian.providers import Reading
from glucose_guardian.validation import Validity, validate

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def check(value: object, age_s: float) -> Validity:
    reading = Reading(value, NOW - timedelta(seconds=age_s))
    return validate(reading, now=NOW, max_age_s=600, max_future_skew_s=60)


@pytest.mark.parametrize("age_s,expected", [
    (0, Validity.VALID),
    (600, Validity.VALID),
    (600.001, Validity.STALE),
    (-60, Validity.VALID),
    (-60.001, Validity.INVALID),
])
def test_age_bounds(age_s, expected):
    assert check(110, age_s) is expected


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 0, -1, True, False, "50", None])
def test_invalid_values(value):
    assert check(value, 0) is Validity.INVALID


@pytest.mark.parametrize("value", [39, 401, 5000, 72.5])
def test_out_of_display_range_values_are_valid(value):
    assert check(value, 0) is Validity.VALID


@pytest.mark.parametrize("timestamp", [NOW.replace(tzinfo=None), None])
def test_naive_or_missing_timestamp_is_invalid(timestamp):
    reading = Reading(110, timestamp)
    assert validate(reading, now=NOW, max_age_s=600, max_future_skew_s=60) is Validity.INVALID


def test_non_utc_offset_is_compared_correctly():
    reading = Reading(110, NOW.astimezone(timezone(timedelta(hours=2))))
    assert validate(reading, now=NOW, max_age_s=600, max_future_skew_s=60) is Validity.VALID


def test_naive_now_raises():
    with pytest.raises(ValueError):
        validate(Reading(110, NOW), now=NOW.replace(tzinfo=None), max_age_s=600,
                 max_future_skew_s=60)
