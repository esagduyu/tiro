"""Snooze preset computation and `until` validation (tiro/snooze.py) — the
backbone of M3.0 Task 1 / M3.2's swipe-triage inbox."""

from datetime import UTC, datetime, timedelta

import pytest

from tiro import snooze


def _freeze(monkeypatch, local_dt: datetime) -> None:
    """local_dt must be tz-aware (server-local)."""
    monkeypatch.setattr(snooze, "_local_now", lambda: local_dt)


def test_tonight_before_1900_targets_1900_today(monkeypatch):
    now = datetime(2026, 7, 6, 14, 0, tzinfo=UTC)  # 14:00 local
    _freeze(monkeypatch, now)
    result = snooze.compute_preset("tonight")
    assert result == "2026-07-06 19:00:00"


def test_tonight_after_1900_adds_six_hours(monkeypatch):
    now = datetime(2026, 7, 6, 20, 30, tzinfo=UTC)  # 20:30 local, past 19:00
    _freeze(monkeypatch, now)
    result = snooze.compute_preset("tonight")
    expected = (now + timedelta(hours=6)).astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
    assert result == expected


def test_tonight_exactly_at_1900_adds_six_hours(monkeypatch):
    # target <= now boundary: exactly 19:00 counts as "already passed".
    now = datetime(2026, 7, 6, 19, 0, tzinfo=UTC)
    _freeze(monkeypatch, now)
    result = snooze.compute_preset("tonight")
    assert result == "2026-07-07 01:00:00"


def test_tomorrow_is_9am_next_day(monkeypatch):
    now = datetime(2026, 7, 6, 22, 0, tzinfo=UTC)
    _freeze(monkeypatch, now)
    result = snooze.compute_preset("tomorrow")
    assert result == "2026-07-07 09:00:00"


def test_weekend_from_monday_is_this_saturday(monkeypatch):
    now = datetime(2026, 7, 6, 10, 0, tzinfo=UTC)  # Monday
    assert now.weekday() == 0
    _freeze(monkeypatch, now)
    result = snooze.compute_preset("weekend")
    assert result == "2026-07-11 09:00:00"  # the following Saturday


def test_weekend_from_saturday_morning_before_9_is_today(monkeypatch):
    now = datetime(2026, 7, 11, 6, 0, tzinfo=UTC)  # Saturday, before 09:00
    assert now.weekday() == 5
    _freeze(monkeypatch, now)
    result = snooze.compute_preset("weekend")
    assert result == "2026-07-11 09:00:00"


def test_weekend_from_saturday_after_9_rolls_to_next_saturday(monkeypatch):
    now = datetime(2026, 7, 11, 12, 0, tzinfo=UTC)  # Saturday, after 09:00
    _freeze(monkeypatch, now)
    result = snooze.compute_preset("weekend")
    assert result == "2026-07-18 09:00:00"


def test_next_week_from_wednesday_is_next_monday(monkeypatch):
    now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)  # Wednesday
    assert now.weekday() == 2
    _freeze(monkeypatch, now)
    result = snooze.compute_preset("next_week")
    assert result == "2026-07-13 09:00:00"


def test_next_week_from_monday_after_9_rolls_to_following_monday(monkeypatch):
    now = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)  # Monday, after 09:00
    _freeze(monkeypatch, now)
    result = snooze.compute_preset("next_week")
    assert result == "2026-07-13 09:00:00"


def test_unknown_preset_raises_value_error(monkeypatch):
    _freeze(monkeypatch, datetime(2026, 7, 6, 10, 0, tzinfo=UTC))
    with pytest.raises(ValueError):
        snooze.compute_preset("someday")


def test_validate_until_accepts_future_utc_iso():
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    result = snooze.validate_until(future)
    assert result.count("-") == 2 and " " in result  # storage format, not 'T'


def test_validate_until_accepts_z_suffix():
    future = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = snooze.validate_until(future)
    assert result  # parses cleanly, no exception


def test_validate_until_rejects_past():
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    with pytest.raises(ValueError):
        snooze.validate_until(past)


def test_validate_until_rejects_malformed():
    with pytest.raises(ValueError):
        snooze.validate_until("not-a-date")


def test_validate_until_treats_naive_as_utc():
    # A naive future timestamp should be accepted (treated as UTC), not
    # silently misinterpreted as already-past or raise on tz math.
    future_naive = (datetime.now(UTC) + timedelta(days=2)).replace(tzinfo=None).isoformat()
    result = snooze.validate_until(future_naive)
    assert result
