"""Snooze preset computation and `until` validation.

Snooze is the backbone of M3.2's swipe-triage inbox: hide an article from
the default inbox view until a future time, without deleting or archiving
it — see tiro/queries.py's `include_snoozed` builder param for the
visibility mechanism this feeds, and PATCH /api/articles/{id}/snooze
(tiro/api/routes_articles.py) for the route that calls this module.

Timestamp convention: `articles.snoozed_until` is stored the same way every
other timestamp column in this schema is (`ingested_at`, `sessions.
expires_at`, ...) — a naive 'YYYY-MM-DD HH:MM:SS' string representing UTC,
directly comparable lexically against SQLite's `datetime('now')` (see
tiro/queries.py). Both the preset path and the custom `until` path funnel
through `_to_storage_format()` before handing a value back to the route.

Presets use *server-local* wall-clock time deliberately: Tiro is a
single-user local-first app and there's no per-request client timezone
plumbed through yet (that's M3.2/M3.3 territory) — "tonight" means 19:00
where the server process runs, not 19:00 UTC. The local `now` is read
through `_local_now()`, a seam tests monkeypatch to freeze time instead of
patching `datetime.now` globally.
"""

from datetime import UTC, datetime, timedelta

PRESETS = ("tonight", "tomorrow", "weekend", "next_week")

_STORAGE_FORMAT = "%Y-%m-%d %H:%M:%S"


def _local_now() -> datetime:
    """Clock seam: timezone-aware datetime in the server's local timezone.
    Tests monkeypatch this function (not `datetime.now`) to freeze time for
    deterministic preset computation."""
    return datetime.now().astimezone()


def _to_storage_format(dt: datetime) -> str:
    """Convert an aware datetime to the naive UTC 'YYYY-MM-DD HH:MM:SS'
    string convention every timestamp column in this schema uses."""
    return dt.astimezone(UTC).strftime(_STORAGE_FORMAT)


def _next_weekday_at(now: datetime, *, weekday: int, hour: int) -> datetime:
    """Next occurrence of `weekday` (Mon=0..Sun=6) at `hour`:00, strictly
    after `now` — if today is already the target weekday but `hour`:00 has
    passed, rolls to next week rather than returning a timestamp in the
    past."""
    days_ahead = (weekday - now.weekday()) % 7
    candidate = (now + timedelta(days=days_ahead)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate


def compute_preset(preset: str) -> str:
    """Compute `snoozed_until` for a named preset, pre-formatted for
    storage (see module docstring for the timestamp convention).

    | preset      | target (server-local wall-clock time)                    |
    |-------------|------------------------------------------------------------|
    | tonight     | today 19:00, or now + 6h if 19:00 has already passed today |
    | tomorrow    | tomorrow 09:00                                              |
    | weekend     | next Saturday 09:00 (rolls a week if Sat 09:00 has passed) |
    | next_week   | next Monday 09:00 (rolls a week if Mon 09:00 has passed)   |

    Raises ValueError for an unrecognized preset name.
    """
    if preset not in PRESETS:
        raise ValueError(f"Unknown preset: {preset!r}")

    now = _local_now()

    if preset == "tonight":
        target = now.replace(hour=19, minute=0, second=0, microsecond=0)
        if target <= now:
            target = now + timedelta(hours=6)
        return _to_storage_format(target)

    if preset == "tomorrow":
        target = (now + timedelta(days=1)).replace(
            hour=9, minute=0, second=0, microsecond=0
        )
        return _to_storage_format(target)

    if preset == "weekend":
        return _to_storage_format(_next_weekday_at(now, weekday=5, hour=9))

    # next_week
    return _to_storage_format(_next_weekday_at(now, weekday=0, hour=9))


def validate_until(until: str) -> str:
    """Parse and validate a client-supplied `until` ISO timestamp.

    Naive input (no tzinfo) is treated as UTC, matching this schema's
    storage convention. Raises ValueError if the string is malformed or not
    strictly in the future; otherwise returns it pre-formatted for storage.
    """
    try:
        parsed = datetime.fromisoformat(until)
    except (ValueError, TypeError) as e:
        raise ValueError(f"Malformed until timestamp: {until!r}") from e

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)

    if parsed <= datetime.now(UTC):
        raise ValueError("until must be in the future")

    return _to_storage_format(parsed)
