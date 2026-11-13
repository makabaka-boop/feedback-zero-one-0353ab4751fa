"""Parser for the supported RFC 5545 section 3.3.10 RRULE subset.

Supported content lines::

    RRULE:FREQ=DAILY|WEEKLY|MONTHLY
          [;INTERVAL=positive integer]
          [;COUNT=positive integer | ;UNTIL=UTC datetime]   (exactly one)
          [;BYDAY=[+/-]n?MO,...,+/-]n?SU]
          [;BYMONTHDAY=integer in -31..-1,1..31 (may repeat)]
          [;BYSETPOS=integer (may repeat)]

The week always starts on Monday (``WKST`` is fixed to MO; an explicit WKST
is rejected rather than silently ignored).  Every other RRULE part is an
unknown member and rejected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from .errors import RRuleError, ValidationError

FREQ_DAILY = "DAILY"
FREQ_WEEKLY = "WEEKLY"
FREQ_MONTHLY = "MONTHLY"
FREQUENCIES = (FREQ_DAILY, FREQ_WEEKLY, FREQ_MONTHLY)

#: ISO weekday numbers with Monday == 1.
WEEKDAYS = {
    "MO": 1,
    "TU": 2,
    "WE": 3,
    "TH": 4,
    "FR": 5,
    "SA": 6,
    "SU": 7,
}

_BYDAY_RE = re.compile(r"^([+-]?\d{1,2})?(MO|TU|WE|TH|FR|SA|SU)$")
_INT_RE = re.compile(r"^[+-]?\d+$")


@dataclass
class ByDayEntry:
    """One BYDAY element, e.g. ``2MO`` -> ordinal 2, weekday 1."""

    weekday: int
    ordinal: int | None = None  # None means plain weekday, no nth prefix


@dataclass
class RRule:
    freq: str
    interval: int = 1
    count: int | None = None
    until: datetime | None = None  # timezone-aware UTC datetime
    byday: list[ByDayEntry] = field(default_factory=list)
    bymonthday: list[int] = field(default_factory=list)
    bysetpos: list[int] = field(default_factory=list)

    @property
    def has_byday(self) -> bool:
        return bool(self.byday)

    @property
    def has_bymonthday(self) -> bool:
        return bool(self.bymonthday)

    @property
    def has_bysetpos(self) -> bool:
        return bool(self.bysetpos)


def parse_rrule(text) -> RRule:
    """Parse an RRULE value (with or without the ``RRULE:`` prefix)."""
    if not isinstance(text, str):
        raise ValidationError("$.rule", "RRULE must be a string")
    raw = text
    # Byte and character offsets coincide: every character allowed in an
    # RRULE content line is ASCII; a non-ASCII character is itself an error.
    if raw.startswith("RRULE:"):
        raw = raw[len("RRULE:"):]
    if not raw:
        raise RRuleError(0, "empty RRULE")

    seen: set[str] = set()
    rule = RRule(freq="")
    pos = 0
    parts = raw.split(";")
    for part in parts:
        if part == "":
            raise RRuleError(pos, "empty rule part (stray or trailing ';')")
        eq = part.find("=")
        if eq <= 0:
            raise RRuleError(pos, "rule part must be NAME=VALUE")
        name = part[:eq]
        value = part[eq + 1:]
        value_pos = pos + eq + 1
        if value == "":
            raise RRuleError(value_pos, f"{name} has an empty value")
        if name in seen:
            raise RRuleError(pos, f"duplicate rule part {name}")
        seen.add(name)
        handler = _PART_HANDLERS.get(name)
        if handler is None:
            raise RRuleError(
                pos,
                f"unsupported rule part {name}; supported parts are FREQ, "
                "INTERVAL, COUNT, UNTIL, BYDAY, BYMONTHDAY, BYSETPOS",
            )
        handler(rule, value, value_pos)
        pos += len(part) + 1  # account for the ';' separator

    if not rule.freq:
        raise RRuleError(0, "FREQ is required")
    if (rule.count is None) == (rule.until is None):
        raise RRuleError(
            0, "exactly one of COUNT or UNTIL must be specified"
        )
    _validate_combination(rule)
    return rule


# -- part handlers -----------------------------------------------------------


def _parse_int(value: str, pos: int, name: str) -> int:
    if not _INT_RE.match(value):
        raise RRuleError(pos, f"{name} must be an integer")
    try:
        return int(value)
    except ValueError:
        raise RRuleError(pos, f"{name} integer is out of range")


def _handle_freq(rule: RRule, value: str, pos: int) -> None:
    if value not in FREQUENCIES:
        raise RRuleError(
            pos, f"FREQ must be one of {', '.join(FREQUENCIES)}"
        )
    rule.freq = value


def _handle_interval(rule: RRule, value: str, pos: int) -> None:
    if "," in value:
        raise RRuleError(pos, "INTERVAL takes a single value")
    n = _parse_int(value, pos, "INTERVAL")
    if n < 1:
        raise RRuleError(pos, "INTERVAL must be a positive integer")
    rule.interval = n


def _handle_count(rule: RRule, value: str, pos: int) -> None:
    if "," in value:
        raise RRuleError(pos, "COUNT takes a single value")
    n = _parse_int(value, pos, "COUNT")
    if n < 1:
        raise RRuleError(pos, "COUNT must be a positive integer")
    rule.count = n


def _handle_until(rule: RRule, value: str, pos: int) -> None:
    from .timeutil import parse_utc_datetime

    if "," in value:
        raise RRuleError(pos, "UNTIL takes a single value")
    try:
        rule.until = parse_utc_datetime(value, "$.rule")
    except ValidationError as exc:
        # Re-anchor the message at a byte offset inside $.rule.
        raise RRuleError(pos, exc.message)


def _handle_byday(rule: RRule, value: str, pos: int) -> None:
    entries: list[ByDayEntry] = []
    cursor = pos
    for token in value.split(","):
        if token == "":
            raise RRuleError(cursor, "empty BYDAY element")
        m = _BYDAY_RE.match(token)
        if not m:
            raise RRuleError(
                cursor,
                "BYDAY elements look like MO..SU with an optional +/-n prefix",
            )
        ordinal = int(m.group(1)) if m.group(1) is not None else None
        if ordinal is not None and not (-53 <= ordinal <= 53):
            raise RRuleError(cursor, "BYDAY ordinal must be within +/-53")
        entries.append(ByDayEntry(weekday=WEEKDAYS[m.group(2)], ordinal=ordinal))
        cursor += len(token) + 1
    rule.byday = entries


def _handle_bymonthday(rule: RRule, value: str, pos: int) -> None:
    days: list[int] = []
    cursor = pos
    for token in value.split(","):
        if token == "":
            raise RRuleError(cursor, "empty BYMONTHDAY element")
        n = _parse_int(token, cursor, "BYMONTHDAY")
        if not (-31 <= n <= -1 or 1 <= n <= 31) or n == 0:
            raise RRuleError(
                cursor, "BYMONTHDAY values must be in -31..-1 or 1..31"
            )
        if n not in days:
            days.append(n)
        cursor += len(token) + 1
    rule.bymonthday = days


def _handle_bysetpos(rule: RRule, value: str, pos: int) -> None:
    positions: list[int] = []
    cursor = pos
    for token in value.split(","):
        if token == "":
            raise RRuleError(cursor, "empty BYSETPOS element")
        n = _parse_int(token, cursor, "BYSETPOS")
        if n == 0:
            raise RRuleError(cursor, "BYSETPOS must not be zero")
        if n not in positions:
            positions.append(n)
        cursor += len(token) + 1
    rule.bysetpos = positions


_PART_HANDLERS = {
    "FREQ": _handle_freq,
    "INTERVAL": _handle_interval,
    "COUNT": _handle_count,
    "UNTIL": _handle_until,
    "BYDAY": _handle_byday,
    "BYMONTHDAY": _handle_bymonthday,
    "BYSETPOS": _handle_bysetpos,
}


def _validate_combination(rule: RRule) -> None:
    """Frequency-specific legality rules from RFC 5545."""
    if rule.freq == FREQ_DAILY:
        for entry in rule.byday:
            if entry.ordinal is not None:
                raise RRuleError(
                    0, "ordinal BYDAY (e.g. 2MO) is only valid with MONTHLY"
                )
        if rule.bymonthday:
            raise RRuleError(
                0, "BYMONTHDAY is only valid with FREQ=MONTHLY"
            )
    elif rule.freq == FREQ_WEEKLY:
        for entry in rule.byday:
            if entry.ordinal is not None:
                raise RRuleError(
                    0, "ordinal BYDAY (e.g. 2MO) is only valid with MONTHLY"
                )
        if rule.bymonthday:
            raise RRuleError(
                0, "BYMONTHDAY is only valid with FREQ=MONTHLY"
            )
    else:  # MONTHLY
        pass  # all parts are legal; ordinal handling happens during expansion

    if rule.bysetpos and not (rule.byday or rule.bymonthday):
        raise RRuleError(
            0,
            "BYSETPOS requires BYDAY or BYMONTHDAY to define a candidate set",
        )
