"""Recurrence expansion engine for the supported RRULE subset.

The engine works purely on calendar values (dates and wall-clock datetimes);
it never increments epoch seconds, so DST offset changes cannot shift the
sampled local time.

Per period the pipeline is, in order:

1. expand candidate calendar dates (invalid calendar dates are dropped),
2. attach the start's wall-clock time and drop non-existent local times
   (spring-forward gaps),
3. apply BYSETPOS to the survivors,
4. truncate with UNTIL / COUNT.

Skipped candidates do not consume COUNT.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .errors import ValidationError
from .rrule_parser import (
    FREQ_DAILY,
    FREQ_WEEKLY,
    RRule,
)
from .timeutil import check_year, format_rid, localize


#: Safety valve for COUNT rules without UNTIL.  A valid rule emits at least
#: one instance per period whenever it emits at all, so reaching this many
#: empty periods means the rule can never satisfy COUNT (e.g. a BYSETPOS that
#: points past every period's candidate set).
_MAX_PERIODS = 100_000


@dataclass
class Occurrence:
    """One generated occurrence before overrides are applied."""

    rid: str          # original local wall-clock time, YYYY-MM-DDTHH:MM:SS
    naive: datetime   # same value as a naive datetime
    utc: int          # epoch seconds (earlier instant on DST overlap)
    ambiguous: bool   # True when the local time was a fall-back overlap


# -- calendar primitives -----------------------------------------------------


def _days_in_month(year: int, month: int) -> int:
    # Avoid constructing date(year + 1, ...) when year == 9999, which would
    # overflow the supported calendar range before our own check runs.
    last_day = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    if month == 2 and _is_leap(year):
        return 29
    return last_day


def _is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _ordinal_to_ymd(ordinal: int) -> tuple[int, int, int]:
    """Decode a day ordinal (proleptic Gregorian) to (year, month, day).

    Works for ordinals beyond ``datetime.date``'s own range so the result can
    be rejected by :func:`check_year` instead of raising a ValueError.
    """
    ordinal -= 1  # days since an imaginary year-0 Jan 1 (a Monday)
    year = 1
    while True:
        span = 366 if _is_leap(year) else 365
        if ordinal < span:
            break
        ordinal -= span
        year += 1
    month = 1
    while True:
        dim = _days_in_month(year, month)
        if ordinal < dim:
            break
        ordinal -= dim
        month += 1
    return year, month, ordinal + 1


def _add_days(year: int, month: int, day: int, delta: int) -> tuple[int, int, int]:
    """Calendar day arithmetic that never constructs an out-of-range date.

    Accepts input years outside 1..9999 (such as an anchor already moved into
    year 10000) and returns a plain tuple, leaving range rejection to the
    caller via :func:`check_year`.
    """
    ordinal = _ymd_to_ordinal(year, month, day) + delta
    return _ordinal_to_ymd(ordinal)


def _ymd_to_ordinal(year: int, month: int, day: int) -> int:
    """Day ordinal for any (possibly out-of-range) proleptic-Gregorian date."""
    years_before = year - 1
    leap_before = (
        years_before // 4
        - years_before // 100
        + years_before // 400
    )
    ordinal = years_before * 365 + leap_before + day
    for m in range(1, month):
        ordinal += _days_in_month(year, m)
    return ordinal


def _weekday_of_ymd(year: int, month: int, day: int) -> int:
    """ISO weekday (Monday=1) without constructing an out-of-range date."""
    # date(1, 1, 1).isoweekday() == 1 (a Monday); ordinal arithmetic repeats
    # every seven days, so (ordinal - 1) % 7 + 1 holds for any proleptic date.
    ordinal = _ymd_to_ordinal(year, month, day)
    return (ordinal - 1) % 7 + 1


def _add_months(year: int, month: int, k: int) -> tuple[int, int]:
    index = (year * 12 + (month - 1)) + k
    return index // 12, index % 12 + 1


def _monday_on_or_before(d: date) -> date:
    return d - timedelta(days=d.isoweekday() - 1)


def _nth_weekday_dates(
    year: int, month: int, weekday: int, ordinal: int
) -> list[date]:
    """Dates of the nth given weekday in a month (ordinal may be negative)."""
    dim = _days_in_month(year, month)
    matches = [
        date(year, month, day)
        for day in range(1, dim + 1)
        if date(year, month, day).isoweekday() == weekday
    ]
    if ordinal > 0:
        return [matches[ordinal - 1]] if ordinal <= len(matches) else []
    idx = len(matches) + ordinal
    return [matches[idx]] if idx >= 0 else []


# -- per-frequency candidate dates -------------------------------------------


def _daily_dates(period_date: date, rule: RRule) -> list[date]:
    if not rule.byday:
        return [period_date]
    allowed = {entry.weekday for entry in rule.byday}
    return [period_date] if period_date.isoweekday() in allowed else []


def _weekly_dates(anchor: date, rule: RRule, start_weekday: int) -> list[date]:
    week_start = _monday_on_or_before(anchor)
    allowed = (
        {entry.weekday for entry in rule.byday}
        if rule.byday
        else {start_weekday}
    )
    return [
        week_start + timedelta(days=offset)
        for offset in range(7)
        if (offset + 1) in allowed
    ]


def _monthly_dates(
    year: int, month: int, rule: RRule, start_day: int
) -> list[date]:
    dim = _days_in_month(year, month)

    if rule.has_bymonthday:
        valid_days: set[int] = set()
        for md in rule.bymonthday:
            day = md if md > 0 else dim + md + 1
            # Invalid calendar dates (e.g. 31 in a 30-day month) are dropped.
            if 1 <= day <= dim:
                valid_days.add(day)
        if rule.has_byday:
            plain_weekdays = {
                e.weekday for e in rule.byday if e.ordinal is None
            }
            ordinal_dates: set[date] = set()
            for entry in rule.byday:
                if entry.ordinal is not None:
                    ordinal_dates.update(
                        _nth_weekday_dates(
                            year, month, entry.weekday, entry.ordinal
                        )
                    )
            valid_days = {
                day
                for day in valid_days
                if date(year, month, day).isoweekday() in plain_weekdays
                or date(year, month, day) in ordinal_dates
            }
        return [date(year, month, day) for day in sorted(valid_days)]

    if rule.has_byday:
        picked: set[date] = set()
        for entry in rule.byday:
            if entry.ordinal is None:
                picked.update(
                    date(year, month, day)
                    for day in range(1, dim + 1)
                    if date(year, month, day).isoweekday() == entry.weekday
                )
            else:
                picked.update(
                    _nth_weekday_dates(
                        year, month, entry.weekday, entry.ordinal
                    )
                )
        return sorted(picked)

    # Plain MONTHLY: repeat on the start's day-of-month when it exists.
    if start_day <= dim:
        return [date(year, month, start_day)]
    return []


# -- BYSETPOS ----------------------------------------------------------------


def _apply_bysetpos(
    occurrences: list[Occurrence], positions: list[int]
) -> list[Occurrence]:
    total = len(occurrences)
    chosen: set[str] = set()
    result: list[Occurrence] = []
    for pos in positions:
        idx = pos - 1 if pos > 0 else total + pos
        # Out-of-range positions match nothing (RFC 5545 semantics).
        if 0 <= idx < total and occurrences[idx].rid not in chosen:
            chosen.add(occurrences[idx].rid)
            result.append(occurrences[idx])
    result.sort(key=lambda item: item.naive)
    return result


# -- start conformance -------------------------------------------------------


def validate_start_conforms(
    rule: RRule, start_naive: datetime, zone: ZoneInfo
) -> None:
    """Verify DTSTART would itself be produced by the rule.

    The check replays the first period's full pipeline (candidate dates,
    gap removal, BYSETPOS) so every rule part is taken into account.
    """
    d = start_naive.date()
    if rule.freq == FREQ_DAILY:
        dates = _daily_dates(d, rule)
    elif rule.freq == FREQ_WEEKLY:
        dates = _weekly_dates(d, rule, d.isoweekday())
    else:
        dates = _monthly_dates(d.year, d.month, rule, d.day)

    kept: list[Occurrence] = []
    for cand in dates:
        naive = datetime.combine(cand, start_naive.time())
        result = localize(naive, zone)
        if result.kind == "gap":
            continue
        assert result.utc is not None
        kept.append(
            Occurrence(
                rid=format_rid(naive),
                naive=naive,
                utc=result.utc,
                ambiguous=result.kind == "overlap",
            )
        )
    if rule.bysetpos:
        kept = _apply_bysetpos(kept, rule.bysetpos)

    if format_rid(start_naive) not in {item.rid for item in kept}:
        raise ValidationError(
            "$.start", "start is not produced by the rule in its first period"
        )


# -- main expansion ----------------------------------------------------------


def expand(
    rule: RRule,
    start_naive: datetime,
    zone: ZoneInfo,
    until_epoch: int | None,
) -> list[Occurrence]:
    """Expand the rule into occurrences, honoring gaps, COUNT and UNTIL."""
    wall_time = start_naive.time()
    start_d = start_naive.date()
    emitted: list[Occurrence] = []
    period_index = 0

    while True:
        # Compute the period anchor and earliest calendar date first so that
        # overflow beyond year 9999 is caught by our own check rather than by
        # a raw datetime ValueError during candidate construction.
        if rule.freq == FREQ_DAILY:
            ay, am, ad = _add_days(
                start_d.year, start_d.month, start_d.day,
                period_index * rule.interval,
            )
            anchor_ymd = (ay, am, ad)
            earliest_ymd = anchor_ymd
        elif rule.freq == FREQ_WEEKLY:
            ay, am, ad = _add_days(
                start_d.year, start_d.month, start_d.day,
                period_index * rule.interval * 7,
            )
            anchor_ymd = (ay, am, ad)
            anchor_weekday = _weekday_of_ymd(ay, am, ad)
            # Earliest date is the Monday of that week (Monday == ISO 1).
            earliest_ymd = _add_days(ay, am, ad, -(anchor_weekday - 1))
        else:
            year, month = _add_months(
                start_d.year, start_d.month, period_index * rule.interval
            )
            anchor_ymd = None
            earliest_ymd = (year, month, 1)

        check_year(earliest_ymd[0], "$.rule")
        earliest = date(*earliest_ymd)

        if rule.freq == FREQ_DAILY:
            anchor = date(*anchor_ymd)
            period_dates = _daily_dates(anchor, rule)
        elif rule.freq == FREQ_WEEKLY:
            anchor = date(*anchor_ymd)
            period_dates = _weekly_dates(anchor, rule, start_d.isoweekday())
        else:
            period_dates = _monthly_dates(year, month, rule, start_d.day)

        # UNTIL termination: once the earliest possible instant in a period
        # is past UNTIL, no later period can contribute.
        if until_epoch is not None:
            earliest_naive = datetime.combine(earliest, wall_time)
            earliest_result = localize(earliest_naive, zone)
            earliest_utc = (
                earliest_result.utc
                if earliest_result.utc is not None
                else _gap_shifted_epoch(earliest_naive, zone)
            )
            if earliest_utc > until_epoch:
                break

        # Build occurrences, dropping non-existent local times (gaps).
        kept: list[Occurrence] = []
        for d in period_dates:
            check_year(d.year, "$.rule")
            naive = datetime.combine(d, wall_time)
            result = localize(naive, zone)
            if result.kind == "gap":
                continue  # skipped instance; does not consume COUNT
            assert result.utc is not None
            kept.append(
                Occurrence(
                    rid=format_rid(naive),
                    naive=naive,
                    utc=result.utc,
                    ambiguous=result.kind == "overlap",
                )
            )

        if rule.bysetpos:
            kept = _apply_bysetpos(kept, rule.bysetpos)

        # Truncation: UNTIL first (kept is chronological), then COUNT.
        for occ in kept:
            if until_epoch is not None and occ.utc > until_epoch:
                break
            if rule.count is not None and len(emitted) >= rule.count:
                break
            emitted.append(occ)

        if rule.count is not None and len(emitted) >= rule.count:
            break
        period_index += 1
        if until_epoch is None and period_index > _MAX_PERIODS:
            raise ValidationError(
                "$.rule",
                "rule cannot produce the requested COUNT within "
                f"{_MAX_PERIODS} periods (check BYSETPOS / BYDAY coverage)",
            )

    return emitted


def _gap_shifted_epoch(naive: datetime, zone: ZoneInfo) -> int:
    """Epoch of the instant zoneinfo shifts a non-existent time onto."""
    aware = naive.replace(tzinfo=zone)
    return int(aware.astimezone(timezone.utc).timestamp())
