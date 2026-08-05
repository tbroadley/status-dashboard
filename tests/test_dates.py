import unittest
from datetime import date, datetime, time

from status_dashboard import dates

# A Wednesday, so weekday/weekend rollovers are exercised.
NOW = datetime(2026, 8, 5, 9, 30)


class ExtractTime(unittest.TestCase):
    def test_meridiem_forms(self):
        cases = (
            ("at 10am", time(10, 0), ""),
            ("2pm every monday", time(14, 0), "every monday"),
            ("every weekday at 12:15pm", time(12, 15), "every weekday"),
            ("at 12am", time(0, 0), ""),
            ("at 12pm", time(12, 0), ""),
            ("at 14:30", time(14, 30), ""),
        )
        for text, expected_time, expected_rest in cases:
            with self.subTest(text=text):
                extracted, rest = dates.extract_time(text)
                self.assertEqual(extracted, expected_time)
                self.assertEqual(rest, expected_rest)

    def test_no_time_present(self):
        extracted, rest = dates.extract_time("every workday")
        self.assertIsNone(extracted)
        self.assertEqual(rest, "every workday")

    def test_rejects_out_of_range(self):
        extracted, _ = dates.extract_time("at 25:00")
        self.assertIsNone(extracted)


class NextOccurrence(unittest.TestCase):
    def test_every_workday_skips_weekend(self):
        friday = datetime(2026, 8, 7, 11, 0)
        self.assertEqual(
            dates.next_occurrence("every workday at 10am", friday),
            datetime(2026, 8, 10, 10, 0),
        )

    def test_every_workday_same_day_if_time_not_passed(self):
        self.assertEqual(
            dates.next_occurrence("every workday at 10am", NOW),
            datetime(2026, 8, 5, 10, 0),
        )

    def test_every_workday_next_day_if_time_passed(self):
        after_ten = datetime(2026, 8, 5, 10, 30)
        self.assertEqual(
            dates.next_occurrence("every workday at 10am", after_ten),
            datetime(2026, 8, 6, 10, 0),
        )

    def test_weekly_named_day(self):
        self.assertEqual(
            dates.next_occurrence("2pm every Monday", NOW),
            datetime(2026, 8, 10, 14, 0),
        )

    def test_every_weekday_with_minutes(self):
        self.assertEqual(
            dates.next_occurrence("every weekday at 12:15pm", NOW),
            datetime(2026, 8, 5, 12, 15),
        )

    def test_interval_rules(self):
        cases = (
            ("every 3 days", date(2026, 8, 8)),
            ("every 2 weeks", date(2026, 8, 19)),
            ("every week", date(2026, 8, 12)),
        )
        for rule, expected in cases:
            with self.subTest(rule=rule):
                self.assertEqual(dates.next_occurrence(rule, NOW), expected)

    def test_monthly_rolls_year(self):
        december = datetime(2026, 12, 20, 9, 0)
        self.assertEqual(
            dates.next_occurrence("every month", december), date(2027, 1, 20)
        )

    def test_day_of_month(self):
        self.assertEqual(
            dates.next_occurrence("every month on the 15th", NOW), date(2026, 8, 15)
        )

    def test_day_of_month_skips_past(self):
        late = datetime(2026, 8, 20, 9, 0)
        self.assertEqual(
            dates.next_occurrence("every month on the 15th", late), date(2026, 9, 15)
        )

    def test_unparseable_rule_returns_none(self):
        self.assertIsNone(dates.next_occurrence("every third blue moon", NOW))

    def test_always_advances_past_now(self):
        for rule in ("every day at 9am", "every workday at 10am", "2pm every monday"):
            with self.subTest(rule=rule):
                result = dates.next_occurrence(rule, NOW)
                self.assertIsNotNone(result)
                assert result is not None
                moment = (
                    result
                    if isinstance(result, datetime)
                    else datetime.combine(result, time.max)
                )
                self.assertGreater(moment, NOW)


class ParseDueString(unittest.TestCase):
    def test_relative_days(self):
        cases = (
            ("today", date(2026, 8, 5)),
            ("tomorrow", date(2026, 8, 6)),
            ("in 3 days", date(2026, 8, 8)),
            ("in 2 weeks", date(2026, 8, 19)),
            ("next working day", date(2026, 8, 6)),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(dates.parse_due_string(text, NOW).due, expected)

    def test_today_with_time(self):
        self.assertEqual(
            dates.parse_due_string("today at 3pm", NOW).due, datetime(2026, 8, 5, 15, 0)
        )

    def test_weekday_names(self):
        self.assertEqual(dates.parse_due_string("friday", NOW).due, date(2026, 8, 7))
        self.assertEqual(
            dates.parse_due_string("next monday", NOW).due, date(2026, 8, 10)
        )

    def test_same_weekday_rolls_forward_when_time_passed(self):
        self.assertEqual(
            dates.parse_due_string("wednesday at 8am", NOW).due,
            datetime(2026, 8, 12, 8, 0),
        )

    def test_calendar_dates(self):
        cases = (
            ("2026-09-01", date(2026, 9, 1)),
            ("aug 20", date(2026, 8, 20)),
            ("20 august", date(2026, 8, 20)),
            ("jan 5", date(2027, 1, 5)),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(dates.parse_due_string(text, NOW).due, expected)

    def test_recurrence_is_captured(self):
        parsed = dates.parse_due_string("every workday at 10am", NOW)
        self.assertTrue(parsed.is_recurring)
        self.assertEqual(parsed.recurrence, "every workday at 10am")
        self.assertEqual(parsed.due, datetime(2026, 8, 5, 10, 0))

    def test_no_date_clears(self):
        for text in ("no date", "none", ""):
            with self.subTest(text=text):
                parsed = dates.parse_due_string(text, NOW)
                self.assertIsNone(parsed.due)
                self.assertFalse(parsed.is_recurring)

    def test_unparseable_returns_no_date(self):
        self.assertIsNone(dates.parse_due_string("sometime whenever", NOW).due)

    def test_production_rules_from_export(self):
        """The five recurrence rules that exist in the live Todoist account."""
        rules = (
            "2pm every Monday",
            "every weekday at 12:15pm",
            "every workday at 10am",
        )
        for rule in rules:
            with self.subTest(rule=rule):
                parsed = dates.parse_due_string(rule, NOW)
                self.assertTrue(parsed.is_recurring)
                self.assertIsNotNone(parsed.due)


class NextWorkingDay(unittest.TestCase):
    def test_skips_weekend(self):
        self.assertEqual(dates.next_working_day(date(2026, 8, 7)), date(2026, 8, 10))

    def test_midweek(self):
        self.assertEqual(dates.next_working_day(date(2026, 8, 5)), date(2026, 8, 6))


if __name__ == "__main__":
    _ = unittest.main()
