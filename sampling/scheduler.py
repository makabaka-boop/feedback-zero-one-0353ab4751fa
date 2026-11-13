"""Top-level orchestration: validate the plan and compute the sample list."""

from __future__ import annotations

from typing import Any

from .errors import ValidationError
from .jsonparser import parse_json
from .recurrence import expand, validate_start_conforms
from .rrule_parser import parse_rrule
from .timeutil import (
    configure_tzpath,
    format_rid,
    load_zone,
    localize,
    parse_local_datetime,
    require_existing,
)

ALLOWED_TOP_LEVEL = {"tz", "start", "rule", "overrides", "window"}


def build_schedule(document: bytes) -> list[dict[str, int | str]]:
    """Validate a JSON plan document and return the ``{rid, utc}`` array."""
    configure_tzpath()
    plan = parse_json(document)
    _check_top_level(plan)

    zone = load_zone(plan["tz"])
    rule = parse_rrule(plan["rule"])
    start_naive = parse_local_datetime(plan["start"], "$.start")
    window_lo, window_hi = _check_window(plan["window"])
    overrides = _check_overrides_shape(plan["overrides"])

    # UNTIL / start relationship, expressed as UTC instants.
    until_epoch: int | None = None
    if rule.until is not None:
        until_epoch = int(rule.until.timestamp())
        start_result = localize(start_naive, zone)
        if start_result.kind == "gap":
            raise ValidationError(
                "$.start",
                f"local datetime {format_rid(start_naive)} does not exist in "
                f"zone {zone.key} (skipped by a DST transition)",
            )
        if start_result.utc is not None and start_result.utc > until_epoch:
            raise ValidationError(
                "$.start", "start must not be later than UNTIL"
            )
    else:
        # COUNT rule: an explicit non-existent start moment is rejected.
        require_existing(start_naive, zone, "$.start")

    validate_start_conforms(rule, start_naive, zone)

    # 1. Expand the original recurrence (COUNT includes only kept instances).
    occurrences = expand(rule, start_naive, zone, until_epoch)
    valid_rids = {item.rid for item in occurrences}

    # 2. Validate every override against the generated recurrence ids.
    parsed_overrides = _validate_overrides(overrides, valid_rids, zone)

    # 3. Apply reschedules / cancellations, then filter by the half-open
    #    window.  Override targets are considered even when the original
    #    instance itself fell outside the window.
    output: list[dict[str, int | str]] = []
    for item in occurrences:
        if item.rid in parsed_overrides:
            target_utc = parsed_overrides[item.rid]
            if target_utc is None:
                continue  # cancellation: no replacement, COUNT not refilled
        else:
            target_utc = item.utc
        if window_lo <= target_utc < window_hi:
            output.append({"rid": item.rid, "utc": target_utc})

    # 4. Deterministic order: UTC instant first, then rid.
    output.sort(key=lambda entry: (entry["utc"], entry["rid"]))
    return output


# -- validation helpers ------------------------------------------------------


def _check_top_level(plan: Any) -> None:
    if not isinstance(plan, dict):
        raise ValidationError("$", "top-level JSON value must be an object")
    unknown = set(plan) - ALLOWED_TOP_LEVEL
    if unknown:
        name = sorted(unknown)[0]
        raise ValidationError(f"$.{name}", "unknown top-level member")
    for required in ("tz", "start", "rule", "overrides", "window"):
        if required not in plan:
            raise ValidationError(f"$.{required}", "missing required member")


def _check_window(value: Any) -> tuple[int, int]:
    path = "$.window"
    if not isinstance(value, list):
        raise ValidationError(path, "window must be an array of two integers")
    if len(value) != 2:
        raise ValidationError(path, "window must contain exactly two elements")
    for idx, bound in enumerate(value):
        if isinstance(bound, bool):
            raise ValidationError(
                f"{path}[{idx}]", "window bounds must be integers, not booleans"
            )
        if not isinstance(bound, int):
            kind = type(bound).__name__
            raise ValidationError(
                f"{path}[{idx}]",
                f"window bounds must be integers (got {kind})",
            )
    lo, hi = value
    if not lo < hi:
        raise ValidationError(
            path, "window must be a non-empty half-open interval [lo, hi)"
        )
    return lo, hi


def _check_overrides_shape(value: Any) -> dict:
    path = "$.overrides"
    if not isinstance(value, dict):
        raise ValidationError(path, "overrides must be an object")
    return value


def _validate_overrides(
    overrides: dict, valid_rids: set[str], zone
) -> dict[str, int | None]:
    parsed: dict[str, int | None] = {}
    for rid, target in overrides.items():
        path = f'$.overrides[{_quote_key(rid)}]'
        # Validate the key's shape first, then its membership in the rule.
        parse_local_datetime(rid, path)
        if rid not in valid_rids:
            raise ValidationError(
                path, "override rid must be produced by the original recurrence"
            )
        if target is None:
            parsed[rid] = None
        else:
            target_naive = parse_local_datetime(target, path)
            parsed[rid] = require_existing(target_naive, zone, path)
    return parsed


def _quote_key(key: str) -> str:
    import json as _json

    return _json.dumps(key, ensure_ascii=False)
