import json
import unittest
from datetime import datetime, timedelta, timezone

from app.scheduling import ScheduleError, due_moments, parse_schedule, scheduled_moment

U = timezone.utc


def S(time="08:00", tz="Europe/Berlin", days=(0, 1, 2, 3, 4, 5, 6)):
    return {"delivery_time": time, "timezone": tz, "weekdays_json": json.dumps(list(days))}


def utc(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=U)


class CatchUp(unittest.TestCase):
    # 2026-03-04 is a Wednesday; Berlin is UTC+1 then, so 08:00 local == 07:00Z
    def test_due_in_exact_minute(self):
        self.assertEqual(due_moments(S(), utc(2026, 3, 4, 7, 0), 15), [utc(2026, 3, 4, 7, 0)])

    def test_not_due_before_scheduled_minute(self):
        self.assertEqual(due_moments(S(), utc(2026, 3, 4, 6, 59), 15), [])

    def test_due_within_catchup_window(self):
        self.assertEqual(due_moments(S(), utc(2026, 3, 4, 7, 10), 15), [utc(2026, 3, 4, 7, 0)])

    def test_window_boundary_inclusive_then_expired(self):
        self.assertEqual(len(due_moments(S(), utc(2026, 3, 4, 7, 15), 15)), 1)
        self.assertEqual(due_moments(S(), utc(2026, 3, 4, 7, 16), 15), [])

    def test_restart_ten_minutes_late_still_delivers(self):
        self.assertEqual(len(due_moments(S(), utc(2026, 3, 4, 7, 10, 30), 15)), 1)

    def test_moment_is_stable_so_idempotency_key_is_stable(self):
        a = due_moments(S(), utc(2026, 3, 4, 7, 1), 15)
        b = due_moments(S(), utc(2026, 3, 4, 7, 14), 15)
        self.assertEqual(a, b)

    def test_zero_catchup_means_exact_minute_only(self):
        self.assertEqual(len(due_moments(S(), utc(2026, 3, 4, 7, 0, 0), 0)), 1)
        self.assertEqual(due_moments(S(), utc(2026, 3, 4, 7, 1, 0), 0), [])

    def test_seconds_after_the_minute_are_due(self):
        self.assertEqual(len(due_moments(S(), utc(2026, 3, 4, 7, 0, 30), 15)), 1)

    def test_other_timezone(self):
        # New York is UTC-5 on 2026-03-04 -> 08:00 local == 13:00Z
        self.assertEqual(due_moments(S(tz="America/New_York"), utc(2026, 3, 4, 13, 2), 15), [utc(2026, 3, 4, 13, 0)])

    def test_midnight_crossing(self):
        # 23:55 Berlin == 22:55Z. At 23:05Z (00:05 local, next day) the run from "yesterday" is still due.
        m = due_moments(S("23:55"), utc(2026, 3, 4, 23, 5), 15)
        self.assertEqual(m, [utc(2026, 3, 4, 22, 55)])

    def test_midnight_crossing_uses_weekday_of_scheduled_local_day(self):
        wed_only = S("23:55", days=(2,))
        self.assertEqual(len(due_moments(wed_only, utc(2026, 3, 4, 23, 5), 15)), 1)   # Thursday 00:05 local, belongs to Wednesday
        thu_only = S("23:55", days=(3,))
        self.assertEqual(due_moments(thu_only, utc(2026, 3, 4, 23, 5), 15), [])

    def test_weekday_filter(self):
        self.assertEqual(due_moments(S(days=(0, 1)), utc(2026, 3, 4, 7, 0), 15), [])
        self.assertEqual(len(due_moments(S(days=(2,)), utc(2026, 3, 4, 7, 0), 15)), 1)


class Dst(unittest.TestCase):
    def test_spring_forward_gap_fires_once_at_first_valid_instant(self):
        # 2026-03-29 Berlin: 02:00 -> 03:00. A 02:30 schedule does not exist; it fires at 03:00 local == 01:00Z
        s = S("02:30")
        self.assertEqual(due_moments(s, utc(2026, 3, 29, 0, 59), 15), [])
        self.assertEqual(due_moments(s, utc(2026, 3, 29, 1, 0), 15), [utc(2026, 3, 29, 1, 0)])
        self.assertEqual(due_moments(s, utc(2026, 3, 29, 1, 10), 15), [utc(2026, 3, 29, 1, 0)])
        self.assertEqual(due_moments(s, utc(2026, 3, 29, 1, 20), 15), [])

    def test_gap_moment_function(self):
        from zoneinfo import ZoneInfo
        z = ZoneInfo("Europe/Berlin")
        self.assertEqual(scheduled_moment(z, datetime(2026, 3, 29).date(), 2, 30), utc(2026, 3, 29, 1, 0))

    def test_fall_back_overlap_fires_only_once(self):
        # 2026-10-25 Berlin: 03:00 CEST -> 02:00 CET. 02:30 happens twice; only the first (00:30Z) counts.
        s = S("02:30")
        self.assertEqual(due_moments(s, utc(2026, 10, 25, 0, 30), 15), [utc(2026, 10, 25, 0, 30)])
        self.assertEqual(due_moments(s, utc(2026, 10, 25, 1, 30), 15), [])   # second 02:30 does not fire again

    def test_normal_day_before_and_after_dst_is_unaffected(self):
        self.assertEqual(due_moments(S("08:00"), utc(2026, 3, 28, 7, 0), 15), [utc(2026, 3, 28, 7, 0)])   # CET
        self.assertEqual(due_moments(S("08:00"), utc(2026, 3, 30, 6, 0), 15), [utc(2026, 3, 30, 6, 0)])   # CEST


class Malformed(unittest.TestCase):
    def test_unknown_timezone(self):
        with self.assertRaisesRegex(ScheduleError, "unknown timezone"):
            parse_schedule(S(tz="Mars/Olympus"))

    def test_empty_timezone(self):
        with self.assertRaises(ScheduleError):
            parse_schedule(S(tz=""))

    def test_bad_time_formats(self):
        for bad in ("25:00", "8:00", "08:60", "noon", "", None):
            with self.subTest(bad=bad), self.assertRaises(ScheduleError):
                parse_schedule({**S(), "delivery_time": bad})

    def test_bad_weekdays(self):
        for bad in ("[]", "[7]", "[-1]", "null", "not json", '["Mon"]', "{}"):
            with self.subTest(bad=bad), self.assertRaises(ScheduleError):
                parse_schedule({**S(), "weekdays_json": bad})

    def test_valid_parses(self):
        zone, hh, mm, days = parse_schedule(S("07:05", days=(1, 3)))
        self.assertEqual((hh, mm, days), (7, 5, {1, 3}))


if __name__ == "__main__":
    unittest.main()
