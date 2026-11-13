"""Time-zone handling and wall-clock / UTC conversion helpers.

The scheduler reasons in *local wall-clock* time because the sampling plan
repeats at a local time (e.g. 02:30 every day).  Converting by adding fixed
second deltas would drift across DST transitions and sample at the wrong
instant, so every occurrence is converted independently through ``zoneinfo``.

The time-zone database is pinned to ``tzdata==2025.2`` shipped inside the
repository (``vendor/lib/tzdata``); ``reset_tzpath`` makes sure the operating
system's own database is never used by accident.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, reset_tzpath

from .errors import ValidationError

#: First year representable by the scheduler (RFC 5545 / datetime limit).
MIN_YEAR = 1
#: Last year representable by the scheduler.
MAX_YEAR = 9999

#: Strict local-time pattern: YYYY-MM-DDTHH:MM:SS (no fractional seconds).
_LOCAL_LEN = 19


def configure_tzpath() -> str:
    """Point zoneinfo at the vendored tzdata 2025.2 database.

    Returns the zoneinfo directory that will be searched first.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    zoneinfo_dir = os.path.normpath(
        os.path.join(here, os.pardir, "vendor", "lib", "tzdata", "zoneinfo")
    )
    if not os.path.isdir(zoneinfo_dir):
        raise RuntimeError(
            f"vendored tzdata database not found at {zoneinfo_dir}"
        )
    reset_tzpath(to=[zoneinfo_dir])
    return zoneinfo_dir


def load_zone(tz_name: str) -> ZoneInfo:
    """Resolve an IANA time-zone name against the vendored database."""
    if not isinstance(tz_name, str) or not tz_name:
        raise ValidationError("$.tz", "time zone must be a non-empty string")
    try:
        return ZoneInfo(tz_name)
    except (KeyError, ValueError):
        raise ValidationError("$.tz", f"unknown IANA time zone {tz_name!r}")


def parse_local_datetime(value, path: str) -> datetime:
    """Parse a ``YYYY-MM-DDTHH:MM:SS`` local wall-clock datetime.

    Leap seconds (``:60``) and any other out-of-range component are rejected.
    """
    if not isinstance(value, str):
        raise ValidationError(path, "local datetime must be a string")
    if len(value) != _LOCAL_LEN or value[4] != "-" or value[7] != "-" \
            or value[10] != "T" or value[13] != ":" or value[16] != ":":
        raise ValidationError(
            path, "local datetime must use YYYY-MM-DDTHH:MM:SS"
        )
    try:
        year = int(value[0:4])
        month = int(value[5:7])
        day = int(value[8:10])
        hour = int(value[11:13])
        minute = int(value[14:16])
        second = int(value[17:19])
    except ValueError:
        raise ValidationError(path, "local datetime contains non-digits")
    if second == 60:
        raise ValidationError(path, "leap seconds (:60) are not allowed")
    try:
        return datetime(year, month, day, hour, minute, second)
    except ValueError as exc:
        raise ValidationError(path, f"invalid local datetime: {exc}")


def parse_utc_datetime(value: str, path: str) -> datetime:
    """Parse an RFC 5545 UTC UNTIL value ``YYYYMMDDTHHMMSSZ``."""
    if not isinstance(value, str):
        raise ValidationError(path, "UNTIL must be a string")
    if len(value) != 16 or value[8] != "T" or value[15] != "Z":
        raise ValidationError(
            path, "UNTIL must be UTC in YYYYMMDDTHHMMSSZ form"
        )
    digits = value[:8] + value[9:15]
    if not digits.isdigit():
        raise ValidationError(path, "UNTIL must contain only digits plus T and Z")
    try:
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise ValidationError(path, f"invalid UNTIL datetime: {exc}")


def check_year(year: int, path: str) -> None:
    """Reject datetimes outside years 1..9999."""
    if year < MIN_YEAR or year > MAX_YEAR:
        raise ValidationError(
            path, f"year {year} is outside the allowed range 1..9999"
        )


class LocalizeResult:
    """Outcome of mapping a local wall-clock datetime to an instant."""

    __slots__ = ("kind", "utc", "fold")

    def __init__(self, kind: str, utc: int | None, fold: int):
        #: "ok" (unambiguous), "gap" (skipped time), or "overlap" (repeated time)
        self.kind = kind
        #: epoch seconds, or ``None`` for a gap
        self.utc = utc
        #: fold side used for an overlap (0 = first, 1 = second)
        self.fold = fold


def localize(naive: datetime, zone: ZoneInfo) -> LocalizeResult:
    """Map a naive local datetime into the zone, classifying DST edge cases.

    * A non-existent local time (spring-forward gap) returns kind ``"gap"``.
    * An ambiguous local time (fall-back overlap) returns kind ``"overlap"``
      with the earlier of the two possible instants (``fold=0``).
    * Everything else returns kind ``"ok"``.
    """
    aware = naive.replace(tzinfo=zone)
    # Round-trip through UTC.  For a skipped time zoneinfo silently shifts the
    # wall clock forward, so the naive part no longer matches the input.
    back = aware.astimezone(timezone.utc).astimezone(zone)
    if back.replace(tzinfo=None) != naive:
        return LocalizeResult("gap", None, 0)

    offset_first = naive.replace(tzinfo=zone, fold=0).utcoffset()
    offset_second = naive.replace(tzinfo=zone, fold=1).utcoffset()
    if offset_first != offset_second:
        # Ambiguous: take the earlier UTC instant (larger UTC offset means
        # the local clock represents an earlier point on the timeline).
        if offset_first > offset_second:
            chosen_fold, chosen_offset = 0, offset_first
        else:
            chosen_fold, chosen_offset = 1, offset_second
        epoch = _naive_epoch_seconds(naive, chosen_offset)
        return LocalizeResult("overlap", epoch, chosen_fold)

    epoch = int(aware.astimezone(timezone.utc).timestamp())
    return LocalizeResult("ok", epoch, 0)


def _naive_epoch_seconds(naive: datetime, offset: timedelta) -> int:
    """Epoch seconds for a naive local datetime given a fixed UTC offset."""
    return int(
        (naive - datetime(1970, 1, 1) - offset).total_seconds()
    )


def require_existing(naive: datetime, zone: ZoneInfo, path: str) -> int:
    """Convert a local datetime that must exist (start, override targets).

    A DST gap here is an explicit non-existent moment and is rejected.
    Overlaps resolve to the earlier UTC instant.
    """
    result = localize(naive, zone)
    if result.kind == "gap":
        raise ValidationError(
            path,
            f"local datetime {naive.isoformat()} does not exist in zone "
            f"{zone.key} (skipped by a DST transition)",
        )
    assert result.utc is not None
    return result.utc


def format_rid(naive: datetime) -> str:
    """Render a recurrence id / local datetime in the wire format."""
    return naive.strftime("%Y-%m-%dT%H:%M:%S")
