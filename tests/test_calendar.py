"""Independent acceptance tests for calendar/DST behavior.

Run from the repository root with::

    python -I tests/run_acceptance.py
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
import zoneinfo

from sampling import build_schedule
from sampling.errors import RRuleError, SamplingError, ValidationError
from sampling.timeutil import configure_tzpath

NYC = "America/New_York"
SYD = "Australia/Sydney"
UTC = "UTC"


def plan(**parts) -> bytes:
    import json

    return json.dumps(parts).encode("utf-8")


def run(parts: dict) -> list[dict]:
    return build_schedule(plan(**parts))


def epoch(tz_name: str, y: int, mo: int, d: int, h: int, mi: int, s: int = 0,
          fold: int = 0) -> int:
    z = ZoneInfo(tz_name)
    return int(datetime(y, mo, d, h, mi, s, fold=fold, tzinfo=z).timestamp())


class TzDatabaseTests(unittest.TestCase):
    def test_vendored_2025_2_is_used(self):
        import glob
        import os

        zoneinfo_dir = configure_tzpath()
        self.assertTrue(zoneinfo_dir.endswith("vendor/lib/tzdata/zoneinfo"))
        # Only the vendored directory may be searched by zoneinfo.
        self.assertEqual(zoneinfo.TZPATH, (zoneinfo_dir,))
        # The pinned version is recorded by the wheel's dist-info metadata.
        dist_info = glob.glob(
            os.path.join(
                os.path.dirname(os.path.dirname(zoneinfo_dir)),
                "tzdata-*.dist-info",
            )
        )
        self.assertEqual(len(dist_info), 1)
        self.assertIn("tzdata-2025.2.dist-info", dist_info[0])
        with open(os.path.join(dist_info[0], "METADATA"),
                  encoding="utf-8") as handle:
            metadata = handle.read()
        self.assertIn("Name: tzdata", metadata)
        self.assertIn("Version: 2025.2", metadata)
        # A zone resolves through the vendored files alone.
        self.assertEqual(ZoneInfo("UTC").key, "UTC")
        self.assertTrue(
            os.path.isfile(os.path.join(zoneinfo_dir, "America", "New_York"))
        )


class BasicTests(unittest.TestCase):
    def test_spec_example_daily_count_with_reschedule(self):
        result = run({
            "tz": UTC,
            "start": "1970-01-01T00:00:00",
            "rule": "FREQ=DAILY;COUNT=2",
            "overrides": {
                "1970-01-01T00:00:00": "1970-01-02T00:00:00",
            },
            "window": [0, 86401],
        })
        self.assertEqual(result, [
            {"rid": "1970-01-01T00:00:00", "utc": 86400},
            {"rid": "1970-01-02T00:00:00", "utc": 86400},
        ])

    def test_half_open_window_bounds(self):
        base = {
            "tz": UTC,
            "start": "1970-01-01T00:00:00",
            "rule": "FREQ=DAILY;COUNT=3",
            "overrides": {},
        }
        self.assertEqual(
            [o["utc"] for o in run({**base, "window": [86400, 2 * 86400]})],
            [86400],
        )
        self.assertEqual(
            [o["utc"] for o in run({**base, "window": [0, 3 * 86400]})],
            [0, 86400, 2 * 86400],
        )

    def test_empty_result(self):
        result = run({
            "tz": UTC,
            "start": "1970-01-01T00:00:00",
            "rule": "FREQ=DAILY;COUNT=2",
            "overrides": {},
            "window": [10 ** 9, 10 ** 9 + 1],
        })
        self.assertEqual(result, [])


class DaylightSavingTests(unittest.TestCase):
    def test_spring_forward_gap_is_skipped_and_does_not_consume_count(self):
        # 02:30 does not exist on 2024-03-10 in New York.
        result = run({
            "tz": NYC,
            "start": "2024-03-09T02:30:00",
            "rule": "FREQ=DAILY;COUNT=2",
            "overrides": {},
            "window": [0, 4102444800],
        })
        self.assertEqual(
            [o["rid"] for o in result],
            ["2024-03-09T02:30:00", "2024-03-11T02:30:00"],
        )
        self.assertEqual(
            result[0]["utc"], epoch(NYC, 2024, 3, 9, 2, 30)
        )
        # 02:30 EDT on the 11th.
        self.assertEqual(
            result[1]["utc"], epoch(NYC, 2024, 3, 11, 2, 30)
        )

    def test_gap_skipped_instance_does_not_refill_count(self):
        # COUNT=2 from the day before the gap: the gap day is skipped, so the
        # two kept instances are the 9th and the 11th (no refill beyond).
        result = run({
            "tz": NYC,
            "start": "2024-03-09T02:30:00",
            "rule": "FREQ=DAILY;COUNT=2",
            "overrides": {},
            "window": [0, 4102444800],
        })
        self.assertEqual(
            [o["rid"] for o in result],
            ["2024-03-09T02:30:00", "2024-03-11T02:30:00"],
        )
        self.assertEqual(len(result), 2)

    def test_fall_back_overlap_takes_earlier_utc(self):
        # 01:30 occurs twice on 2024-11-03 in New York; pick the earlier UTC.
        result = run({
            "tz": NYC,
            "start": "2024-11-03T01:30:00",
            "rule": "FREQ=DAILY;COUNT=1",
            "overrides": {},
            "window": [0, 4102444800],
        })
        expected = epoch(NYC, 2024, 11, 3, 1, 30, fold=0)  # EDT, UTC-4
        later = epoch(NYC, 2024, 11, 3, 1, 30, fold=1)     # EST, UTC-5
        self.assertLess(expected, later)
        self.assertEqual(result, [
            {"rid": "2024-11-03T01:30:00", "utc": expected},
        ])

    def test_start_inside_gap_is_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            run({
                "tz": NYC,
                "start": "2024-03-10T02:30:00",
                "rule": "FREQ=DAILY;COUNT=1",
                "overrides": {},
                "window": [0, 4102444800],
            })
        self.assertEqual(ctx.exception.path, "$.start")

    def test_override_target_in_gap_is_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            run({
                "tz": NYC,
                "start": "2024-03-09T02:30:00",
                "rule": "FREQ=DAILY;COUNT=1",
                "overrides": {
                    "2024-03-09T02:30:00": "2024-03-10T02:30:00",
                },
                "window": [0, 4102444800],
            })
        self.assertTrue(ctx.exception.path.startswith("$.overrides["))

    def test_southern_hemisphere_transition(self):
        # Sydney springs forward 2024-10-06 02:00 -> 03:00; 02:30 is a gap.
        result = run({
            "tz": SYD,
            "start": "2024-10-05T02:30:00",
            "rule": "FREQ=DAILY;COUNT=2",
            "overrides": {},
            "window": [0, 4102444800],
        })
        self.assertEqual(
            [o["rid"] for o in result],
            ["2024-10-05T02:30:00", "2024-10-07T02:30:00"],
        )
        self.assertEqual(
            result[1]["utc"], epoch(SYD, 2024, 10, 7, 2, 30)
        )


class WeeklyTests(unittest.TestCase):
    def test_weekly_byday_monday_week_start(self):
        result = run({
            "tz": UTC,
            "start": "2024-01-01T09:00:00",  # Monday
            "rule": "FREQ=WEEKLY;COUNT=3;BYDAY=MO,WE,FR",
            "overrides": {},
            "window": [0, 4102444800],
        })
        self.assertEqual(
            [o["rid"] for o in result],
            [
                "2024-01-01T09:00:00",  # Mon
                "2024-01-03T09:00:00",  # Wed
                "2024-01-05T09:00:00",  # Fri
            ],
        )

    def test_weekly_interval_two_weeks(self):
        result = run({
            "tz": UTC,
            "start": "2024-01-01T09:00:00",
            "rule": "FREQ=WEEKLY;INTERVAL=2;COUNT=2;BYDAY=MO",
            "overrides": {},
            "window": [0, 4102444800],
        })
        self.assertEqual(
            [o["rid"] for o in result],
            ["2024-01-01T09:00:00", "2024-01-15T09:00:00"],
        )

    def test_start_not_on_byday_is_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            run({
                "tz": UTC,
                "start": "2024-01-02T09:00:00",  # Tuesday
                "rule": "FREQ=WEEKLY;COUNT=1;BYDAY=MO",
                "overrides": {},
                "window": [0, 4102444800],
            })
        self.assertEqual(ctx.exception.path, "$.start")


class MonthlyTests(unittest.TestCase):
    def test_monthly_ordinal_byday(self):
        result = run({
            "tz": UTC,
            "start": "2024-01-15T08:00:00",  # third Monday
            "rule": "FREQ=MONTHLY;COUNT=3;BYDAY=3MO",
            "overrides": {},
            "window": [0, 4102444800],
        })
        self.assertEqual(
            [o["rid"] for o in result],
            [
                "2024-01-15T08:00:00",
                "2024-02-19T08:00:00",
                "2024-03-18T08:00:00",
            ],
        )

    def test_monthly_bymonthday_invalid_dates_dropped(self):
        result = run({
            "tz": UTC,
            "start": "2024-01-31T08:00:00",
            "rule": "FREQ=MONTHLY;COUNT=3;BYMONTHDAY=31",
            "overrides": {},
            "window": [0, 4102444800],
        })
        # Feb 31 / Apr 31 do not exist; only real month-ends are kept.
        self.assertEqual(
            [o["rid"] for o in result],
            [
                "2024-01-31T08:00:00",
                "2024-03-31T08:00:00",
                "2024-05-31T08:00:00",
            ],
        )

    def test_monthly_bysetpos_picks_ordered_survivors(self):
        result = run({
            "tz": UTC,
            "start": "2024-01-01T08:00:00",  # first Monday
            "rule": "FREQ=MONTHLY;COUNT=4;BYDAY=MO;BYSETPOS=1,-1",
            "overrides": {},
            "window": [0, 4102444800],
        })
        # Each month emits its first and last Monday; COUNT=4 covers Jan+Feb.
        self.assertEqual(
            [o["rid"] for o in result],
            [
                "2024-01-01T08:00:00",
                "2024-01-29T08:00:00",
                "2024-02-05T08:00:00",  # first Monday of Feb
                "2024-02-26T08:00:00",  # last Monday of Feb
            ],
        )

    def test_monthly_bymonthday_intersect_byday(self):
        result = run({
            "tz": UTC,
            "start": "2024-01-13T08:00:00",  # Saturday the 13th
            "rule": "FREQ=MONTHLY;COUNT=2;BYMONTHDAY=13,14,15;BYDAY=SA",
            "overrides": {},
            "window": [0, 4102444800],
        })
        # Only Saturdays among the 13th..15th survive.
        self.assertEqual(
            [o["rid"] for o in result],
            ["2024-01-13T08:00:00", "2024-04-13T08:00:00"],
        )

    def test_bysetpos_on_gap_month_filters_survivors(self):
        # Candidates are the 9th (ok), 10th (gap, dropped), 11th (ok).
        # After gap removal BYSETPOS=2 must select the 11th, which is the
        # start, so the start itself conforms to the rule.
        result = run({
            "tz": NYC,
            "start": "2024-03-11T02:30:00",
            "rule": (
                "FREQ=MONTHLY;COUNT=1;"
                "BYMONTHDAY=9,10,11;BYSETPOS=2"
            ),
            "overrides": {},
            "window": [0, 4102444800],
        })
        self.assertEqual(
            [o["rid"] for o in result], ["2024-03-11T02:30:00"]
        )


class UntilAndCountTests(unittest.TestCase):
    def test_until_utc_truncates_on_instant(self):
        result = run({
            "tz": NYC,
            "start": "2024-03-09T02:30:00",
            "rule": "FREQ=DAILY;UNTIL=20240311T073000Z",
            "overrides": {},
            "window": [0, 4102444800],
        })
        # Mar 9 02:30 EST = 07:30Z (in); Mar 10 gap skipped;
        # Mar 11 02:30 EDT = 06:30Z (in); Mar 12 also 06:30Z, which is
        # before the cutoff only if the day still lies within UNTIL -- it
        # does not, because UNTIL=2024-03-11T07:30Z ends on the 11th.
        rids = [o["rid"] for o in result]
        self.assertIn("2024-03-09T02:30:00", rids)
        self.assertIn("2024-03-11T02:30:00", rids)
        self.assertNotIn("2024-03-12T02:30:00", rids)

    def test_until_is_inclusive_at_boundary(self):
        result = run({
            "tz": UTC,
            "start": "1970-01-01T00:00:00",
            "rule": "FREQ=DAILY;UNTIL=19700102T000000Z",
            "overrides": {},
            "window": [0, 4102444800],
        })
        self.assertEqual(
            [o["utc"] for o in result], [0, 86400]
        )

    def test_count_and_until_together_rejected(self):
        with self.assertRaises(RRuleError):
            run({
                "tz": UTC,
                "start": "1970-01-01T00:00:00",
                "rule": "FREQ=DAILY;COUNT=1;UNTIL=19700102T000000Z",
                "overrides": {},
                "window": [0, 1],
            })

    def test_start_later_than_until_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            run({
                "tz": UTC,
                "start": "1970-01-03T00:00:00",
                "rule": "FREQ=DAILY;UNTIL=19700102T000000Z",
                "overrides": {},
                "window": [0, 1],
            })
        self.assertEqual(ctx.exception.path, "$.start")


class OverrideTests(unittest.TestCase):
    def test_cancellation_does_not_refill_count(self):
        result = run({
            "tz": UTC,
            "start": "1970-01-01T00:00:00",
            "rule": "FREQ=DAILY;COUNT=3",
            "overrides": {"1970-01-02T00:00:00": None},
            "window": [0, 10 * 86400],
        })
        self.assertEqual(
            [o["rid"] for o in result],
            ["1970-01-01T00:00:00", "1970-01-03T00:00:00"],
        )

    def test_override_keeps_original_rid(self):
        result = run({
            "tz": UTC,
            "start": "1970-01-01T00:00:00",
            "rule": "FREQ=DAILY;COUNT=1",
            "overrides": {
                "1970-01-01T00:00:00": "1970-01-05T00:00:00",
            },
            "window": [0, 10 * 86400],
        })
        self.assertEqual(result, [
            {"rid": "1970-01-01T00:00:00", "utc": 4 * 86400},
        ])

    def test_override_rescues_instance_whose_original_utc_left_window(self):
        # Original occurrence at utc=0 is outside [100, ...), but its
        # rescheduled target is inside: it must still be considered.
        result = run({
            "tz": UTC,
            "start": "1970-01-01T00:00:00",
            "rule": "FREQ=DAILY;COUNT=1",
            "overrides": {
                "1970-01-01T00:00:00": "1970-01-02T00:00:00",
            },
            "window": [86400, 86401],
        })
        self.assertEqual(result, [
            {"rid": "1970-01-01T00:00:00", "utc": 86400},
        ])

    def test_override_target_outside_window_is_filtered(self):
        result = run({
            "tz": UTC,
            "start": "1970-01-01T00:00:00",
            "rule": "FREQ=DAILY;COUNT=1",
            "overrides": {
                "1970-01-01T00:00:00": "1970-01-10T00:00:00",
            },
            "window": [0, 86401],
        })
        self.assertEqual(result, [])

    def test_unknown_rid_override_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            run({
                "tz": UTC,
                "start": "1970-01-01T00:00:00",
                "rule": "FREQ=DAILY;COUNT=1",
                "overrides": {
                    "1970-01-02T00:00:00": "1970-01-03T00:00:00",
                },
                "window": [0, 10 * 86400],
            })
        self.assertIn("$.overrides[", ctx.exception.path)


class OrderingTests(unittest.TestCase):
    def test_same_instant_different_rids_both_kept_and_rid_sorted(self):
        result = run({
            "tz": UTC,
            "start": "1970-01-01T00:00:00",
            "rule": "FREQ=DAILY;COUNT=3",
            "overrides": {
                "1970-01-03T00:00:00": "1970-01-02T00:00:00",
            },
            "window": [0, 10 * 86400],
        })
        # Two rids land on 86400; both retained, rid ascending.
        self.assertEqual(result, [
            {"rid": "1970-01-01T00:00:00", "utc": 0},
            {"rid": "1970-01-02T00:00:00", "utc": 86400},
            {"rid": "1970-01-03T00:00:00", "utc": 86400},
        ])


class ValidationTests(unittest.TestCase):
    def _expect(self, parts: dict, path: str | None = None,
                exc=ValidationError):
        with self.assertRaises(exc) as ctx:
            run(parts)
        if path is not None:
            self.assertEqual(ctx.exception.path, path)
        return ctx.exception

    def test_unknown_top_level_member(self):
        self._expect({
            "tz": UTC, "start": "1970-01-01T00:00:00",
            "rule": "FREQ=DAILY;COUNT=1", "window": [0, 1],
            "bogus": 1,
        }, "$.bogus")

    def test_missing_member(self):
        self._expect({
            "tz": UTC, "start": "1970-01-01T00:00:00",
            "rule": "FREQ=DAILY;COUNT=1", "overrides": {},
        }, "$.window")
        self._expect({
            "tz": UTC, "start": "1970-01-01T00:00:00",
            "rule": "FREQ=DAILY;COUNT=1", "window": [0, 1],
        }, "$.overrides")

    def test_boolean_window_bound_rejected(self):
        self._expect({
            "tz": UTC, "start": "1970-01-01T00:00:00",
            "rule": "FREQ=DAILY;COUNT=1", "overrides": {}, "window": [True, 5],
        }, "$.window[0]")

    def test_float_window_bound_rejected(self):
        self._expect({
            "tz": UTC, "start": "1970-01-01T00:00:00",
            "rule": "FREQ=DAILY;COUNT=1", "overrides": {}, "window": [0, 1.5],
        }, "$.window[1]")

    def test_empty_window_rejected(self):
        self._expect({
            "tz": UTC, "start": "1970-01-01T00:00:00",
            "rule": "FREQ=DAILY;COUNT=1", "overrides": {}, "window": [5, 5],
        }, "$.window")

    def test_leap_second_rejected(self):
        self._expect({
            "tz": UTC, "start": "2016-12-31T23:59:60",
            "rule": "FREQ=DAILY;COUNT=1", "overrides": {}, "window": [0, 1],
        }, "$.start")

    def test_bad_local_format(self):
        self._expect({
            "tz": UTC, "start": "1970-01-01 00:00:00",
            "rule": "FREQ=DAILY;COUNT=1", "overrides": {}, "window": [0, 1],
        }, "$.start")

    def test_unknown_timezone(self):
        self._expect({
            "tz": "Mars/Olympus", "start": "1970-01-01T00:00:00",
            "rule": "FREQ=DAILY;COUNT=1", "overrides": {}, "window": [0, 1],
        }, "$.tz")

    def test_year_out_of_range_rejected(self):
        self._expect({
            "tz": UTC, "start": "10000-01-01T00:00:00",
            "rule": "FREQ=DAILY;COUNT=1", "overrides": {}, "window": [0, 1],
        }, "$.start")
        self._expect({
            "tz": UTC, "start": "0000-01-01T00:00:00",
            "rule": "FREQ=DAILY;COUNT=1", "overrides": {}, "window": [0, 1],
        }, "$.start")

    def test_rrule_unknown_part(self):
        with self.assertRaises(RRuleError) as ctx:
            run({
                "tz": UTC, "start": "1970-01-01T00:00:00",
                "rule": "FREQ=DAILY;COUNT=1;WKST=MO",
                "overrides": {}, "window": [0, 1],
            })
        # "FREQ=DAILY;COUNT=1;" is 19 bytes, so WKST begins at byte 19.
        self.assertEqual(ctx.exception.byte, 19)

    def test_rrule_bad_freq_byte_offset(self):
        with self.assertRaises(RRuleError) as ctx:
            run({
                "tz": UTC, "start": "1970-01-01T00:00:00",
                "rule": "FREQ=YEARLY;COUNT=1",
                "overrides": {}, "window": [0, 1],
            })
        self.assertEqual(ctx.exception.byte, 5)

    def test_rrule_yearly_rejected(self):
        with self.assertRaises(RRuleError):
            run({
                "tz": UTC, "start": "1970-01-01T00:00:00",
                "rule": "FREQ=YEARLY;COUNT=1",
                "overrides": {}, "window": [0, 1],
            })

    def test_rrule_duplicate_part(self):
        with self.assertRaises(RRuleError) as ctx:
            run({
                "tz": UTC, "start": "1970-01-01T00:00:00",
                "rule": "FREQ=DAILY;COUNT=1;COUNT=2",
                "overrides": {}, "window": [0, 1],
            })
        # "FREQ=DAILY;COUNT=1;" is 19 bytes; the second COUNT starts at 19.
        self.assertEqual(ctx.exception.byte, 19)

    def test_rrule_no_count_or_until(self):
        with self.assertRaises(RRuleError):
            run({
                "tz": UTC, "start": "1970-01-01T00:00:00",
                "rule": "FREQ=DAILY",
                "overrides": {}, "window": [0, 1],
            })

    def test_bymonthday_in_weekly_rejected(self):
        with self.assertRaises(RRuleError):
            run({
                "tz": UTC, "start": "2024-01-01T00:00:00",
                "rule": "FREQ=WEEKLY;COUNT=1;BYMONTHDAY=2",
                "overrides": {}, "window": [0, 1],
            })

    def test_bysetpos_without_candidate_set_rejected(self):
        with self.assertRaises(RRuleError):
            run({
                "tz": UTC, "start": "2024-01-01T00:00:00",
                "rule": "FREQ=DAILY;COUNT=1;BYSETPOS=1",
                "overrides": {}, "window": [0, 1],
            })


    def test_bysetpos_safety_valve_when_count_unreachable(self):
        # Directly exercise the defensive guard by shrinking the period cap:
        # a rule whose later periods yield zero survivors must eventually stop
        # with a validation error rather than spinning forever.
        from sampling import recurrence
        from sampling.rrule_parser import parse_rrule
        from sampling.timeutil import parse_local_datetime, load_zone

        rule = parse_rrule("FREQ=MONTHLY;COUNT=5;BYMONTHDAY=31;BYSETPOS=1")
        start = parse_local_datetime("2024-01-31T00:00:00", "$.start")
        zone = load_zone(UTC)
        original = recurrence._MAX_PERIODS
        recurrence._MAX_PERIODS = 3
        try:
            with self.assertRaises(ValidationError) as ctx:
                recurrence.expand(rule, start, zone, None)
            self.assertEqual(ctx.exception.path, "$.rule")
        finally:
            recurrence._MAX_PERIODS = original

    def test_empty_periods_are_skipped_without_consuming_count(self):
        # BYMONTHDAY=31 yields nothing in short months; COUNT counts only the
        # months that actually contain a 31st.
        result = run({
            "tz": UTC,
            "start": "2024-01-31T00:00:00",
            "rule": "FREQ=MONTHLY;COUNT=3;BYMONTHDAY=31;BYSETPOS=1",
            "overrides": {},
            "window": [0, 9999999999],
        })
        self.assertEqual(
            [o["rid"] for o in result],
            [
                "2024-01-31T00:00:00",
                "2024-03-31T00:00:00",
                "2024-05-31T00:00:00",
            ],
        )

    def test_window_null_bound_rejected(self):
        self._expect({
            "tz": UTC, "start": "1970-01-01T00:00:00",
            "rule": "FREQ=DAILY;COUNT=1", "overrides": {}, "window": [None, 5],
        }, "$.window[0]")

    def test_overrides_must_be_object(self):
        self._expect({
            "tz": UTC, "start": "1970-01-01T00:00:00",
            "rule": "FREQ=DAILY;COUNT=1", "overrides": [], "window": [0, 1],
        }, "$.overrides")

    def test_override_value_wrong_type(self):
        self._expect({
            "tz": UTC, "start": "1970-01-01T00:00:00",
            "rule": "FREQ=DAILY;COUNT=1",
            "overrides": {"1970-01-01T00:00:00": 123},
            "window": [0, 99999999],
        }, '$.overrides["1970-01-01T00:00:00"]')


class YearBoundaryTests(unittest.TestCase):
    BIG_WINDOW = [253000000000, 253402300800]

    def test_occurrences_inside_year_9999_are_emitted(self):
        result = run({
            "tz": UTC,
            "start": "9999-08-31T23:59:59",
            "rule": "FREQ=MONTHLY;COUNT=3;BYMONTHDAY=31",
            "overrides": {},
            "window": self.BIG_WINDOW,
        })
        self.assertEqual(
            [o["rid"] for o in result],
            [
                "9999-08-31T23:59:59",
                "9999-10-31T23:59:59",
                "9999-12-31T23:59:59",
            ],
        )

    def test_daily_crossing_into_10000_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            run({
                "tz": UTC,
                "start": "9999-12-31T00:00:00",
                "rule": "FREQ=DAILY;COUNT=2",
                "overrides": {},
                "window": self.BIG_WINDOW,
            })
        self.assertEqual(ctx.exception.path, "$.rule")
        self.assertIn("10000", ctx.exception.message)

    def test_weekly_crossing_into_10000_rejected(self):
        with self.assertRaises(ValidationError):
            run({
                "tz": UTC,
                "start": "9999-12-27T00:00:00",
                "rule": "FREQ=WEEKLY;COUNT=2;BYDAY=MO",
                "overrides": {},
                "window": self.BIG_WINDOW,
            })

    def test_monthly_crossing_into_10000_rejected(self):
        with self.assertRaises(ValidationError):
            run({
                "tz": UTC,
                "start": "9999-12-15T00:00:00",
                "rule": "FREQ=MONTHLY;COUNT=2",
                "overrides": {},
                "window": self.BIG_WINDOW,
            })

    def test_ordinal_weekday_helper_matches_date_for_in_range_dates(self):
        from datetime import date as _date
        from sampling.recurrence import _weekday_of_ymd

        for y, m, d in [(1, 1, 1), (2024, 3, 10), (9999, 12, 31), (1969, 7, 20)]:
            self.assertEqual(
                _weekday_of_ymd(y, m, d), _date(y, m, d).isoweekday()
            )


class JsonSyntaxTests(unittest.TestCase):
    def test_syntax_error_reports_zero_based_byte(self):
        from sampling.errors import JsonSyntaxError

        bad = b'{"tz": "UTC", "start":, '
        with self.assertRaises(JsonSyntaxError) as ctx:
            build_schedule(bad)
        # The comma after "start": sits at byte 22.
        self.assertEqual(ctx.exception.byte, 22)

    def test_duplicate_member_rejected(self):
        from sampling.errors import JsonSyntaxError

        bad = b'{"tz": "UTC", "tz": "UTC"}'
        with self.assertRaises(JsonSyntaxError) as ctx:
            build_schedule(bad)
        self.assertIn("duplicate", ctx.exception.message)
        self.assertIn("tz", ctx.exception.message)

    def test_byte_offset_counts_utf8_bytes_not_chars(self):
        from sampling.errors import JsonSyntaxError

        # 3-byte UTF-8 char inside a string; error after it must count bytes.
        bad = '{"tz": "€", "start": }'.encode("utf-8")
        with self.assertRaises(JsonSyntaxError) as ctx:
            build_schedule(bad)
            # Position of the stray '}'.
        self.assertEqual(ctx.exception.byte, len(bad) - 1)

    def test_invalid_json_boolean_like_integer_still_int(self):
        # Sanity: 1 stays an int and is usable; only true/false are bools.
        result = run({
            "tz": UTC,
            "start": "1970-01-01T00:00:00",
            "rule": "FREQ=DAILY;COUNT=1",
            "overrides": {},
            "window": [0, 86401],
        })
        self.assertEqual(result[0]["utc"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
