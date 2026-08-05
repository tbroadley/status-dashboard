"""Due-date and recurrence parsing.

Todoist parsed natural-language due strings ("every workday at 10am") on the
server. Notion stores plain dates, so the same grammar is handled here.

Two entry points:
- `parse_due_string` turns user input into a concrete due datetime plus an
  optional recurrence rule to persist alongside it.
- `next_occurrence` advances a recurrence rule past a given moment, which is
  what makes completing a recurring task roll it forward instead of closing it.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

WEEKDAYS = {
    "monday": 0,
    "mon": 0,
    "tuesday": 1,
    "tue": 1,
    "tues": 1,
    "wednesday": 2,
    "wed": 2,
    "thursday": 3,
    "thu": 3,
    "thurs": 3,
    "friday": 4,
    "fri": 4,
    "saturday": 5,
    "sat": 5,
    "sunday": 6,
    "sun": 6,
}

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}  # fmt: skip

_TIME_RE = re.compile(
    r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b|\b(?:at\s+)(\d{1,2}):(\d{2})\b",
    re.IGNORECASE,
)
_NO_DATE = {"no date", "no due date", "none", "someday", ""}


@dataclass
class ParsedDue:
    """Result of parsing a due string.

    `due` is None when the string clears the date. `recurrence` holds the
    normalised rule text when the string describes a repeating task.
    """

    due: datetime | date | None
    recurrence: str | None = None

    @property
    def is_recurring(self) -> bool:
        return self.recurrence is not None


def extract_time(text: str) -> tuple[time | None, str]:
    """Pull a time-of-day out of `text`. Returns the time and the remaining text."""
    match = _TIME_RE.search(text)
    if not match:
        return None, text.strip()

    if match.group(1) is not None:
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        meridiem = match.group(3).lower()
        if meridiem == "pm" and hour != 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
    else:
        hour, minute = int(match.group(4)), int(match.group(5))

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None, text.strip()

    remainder = (text[: match.start()] + " " + text[match.end() :]).strip()
    return time(hour, minute), re.sub(r"\s+", " ", remainder)


def _next_weekday(from_date: date, weekday: int, *, allow_same_day: bool) -> date:
    delta = (weekday - from_date.weekday()) % 7
    if delta == 0 and not allow_same_day:
        delta = 7
    return from_date + timedelta(days=delta)


def next_working_day(from_date: date | None = None) -> date:
    """The next Monday-Friday strictly after `from_date`."""
    day = (from_date or date.today()) + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def _combine(day: date, at: time | None) -> datetime | date:
    return datetime.combine(day, at) if at else day


def parse_recurrence(text: str) -> str | None:
    """Normalise a recurrence rule, or return None if `text` isn't recurring."""
    lowered = text.strip().lower()
    if not lowered:
        return None

    if lowered in {"daily", "weekly", "monthly", "yearly", "annually"}:
        return {"daily": "every day", "weekly": "every week",
                "monthly": "every month", "yearly": "every year",
                "annually": "every year"}[lowered]  # fmt: skip

    return lowered if "every" in lowered else None


def next_occurrence(rule: str, after: datetime) -> datetime | date | None:
    """The first occurrence of `rule` strictly after `after`.

    Returns None when the rule can't be interpreted, which callers treat as
    "not recurring" rather than guessing at a date.
    """
    at, remainder = extract_time(rule.strip().lower())
    remainder = remainder.replace("every", " ").strip()
    remainder = re.sub(r"\s+", " ", remainder)

    # A time-only rule ("every day at 10am") repeats daily.
    if remainder in {"", "day"}:
        return _advance_daily(after, at, step=1)

    if remainder in {"weekday", "workday", "weekdays", "workdays"}:
        return _advance_weekday(after, at)

    if remainder in {"week", "other week"}:
        step = 14 if remainder == "other week" else 7
        return _advance_daily(after, at, step=step)

    if remainder in {"month", "year"}:
        return _advance_months(after, at, 1 if remainder == "month" else 12)

    if match := re.fullmatch(r"(\d+) (day|days|week|weeks)", remainder):
        count = int(match.group(1))
        step = count * (7 if match.group(2).startswith("week") else 1)
        return _advance_daily(after, at, step=step)

    if match := re.fullmatch(r"(\d+) (month|months)", remainder):
        return _advance_months(after, at, int(match.group(1)))

    named = [
        WEEKDAYS[part] for part in re.split(r"[,\s]+", remainder) if part in WEEKDAYS
    ]
    if named:
        return _advance_named_weekdays(after, at, sorted(set(named)))

    if match := re.fullmatch(r"month on the (\d+)(?:st|nd|rd|th)?", remainder):
        return _advance_day_of_month(after, at, int(match.group(1)))

    return None


def _advance_daily(after: datetime, at: time | None, *, step: int) -> datetime | date:
    """Advance by whole days, honouring a time-of-day if the rule has one."""
    if at is None:
        return after.date() + timedelta(days=step)

    candidate = datetime.combine(after.date(), at)
    while candidate <= after:
        candidate += timedelta(days=step)
    return candidate


def _advance_weekday(after: datetime, at: time | None) -> datetime | date:
    candidate = _advance_daily(after, at, step=1)
    day = candidate.date() if isinstance(candidate, datetime) else candidate
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return _combine(day, at)


def _advance_named_weekdays(
    after: datetime, at: time | None, weekdays: list[int]
) -> datetime | date:
    # Same-day is allowed only when a later time today still qualifies.
    allow_today = at is not None and datetime.combine(after.date(), at) > after
    candidates = [
        _next_weekday(after.date(), weekday, allow_same_day=allow_today)
        for weekday in weekdays
    ]
    return _combine(min(candidates), at)


def _add_months(day: date, months: int) -> date:
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    last_day = [31, 29 if _is_leap(year) else 28, 31, 30, 31, 30,
                31, 31, 30, 31, 30, 31][month - 1]  # fmt: skip
    return date(year, month, min(day.day, last_day))


def _is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _advance_months(after: datetime, at: time | None, months: int) -> datetime | date:
    return _combine(_add_months(after.date(), months), at)


def _advance_day_of_month(
    after: datetime, at: time | None, day_of_month: int
) -> datetime | date:
    candidate = after.date().replace(day=1)
    while True:
        try:
            target = candidate.replace(day=day_of_month)
        except ValueError:
            candidate = _add_months(candidate, 1)
            continue
        combined = _combine(target, at)
        moment = (
            combined
            if isinstance(combined, datetime)
            else datetime.combine(target, time.max)
        )
        if moment > after:
            return combined
        candidate = _add_months(candidate, 1)


def parse_due_string(text: str, now: datetime | None = None) -> ParsedDue:
    """Parse a Todoist-style due string into a concrete date and recurrence rule."""
    now = now or datetime.now()
    lowered = re.sub(r"\s+", " ", text.strip().lower())

    if lowered in _NO_DATE:
        return ParsedDue(due=None)

    if recurrence := parse_recurrence(lowered):
        return ParsedDue(due=next_occurrence(recurrence, now), recurrence=recurrence)

    at, remainder = extract_time(lowered)
    remainder = remainder.strip()

    if remainder in {"today", "tod", ""}:
        return ParsedDue(due=_combine(now.date(), at))

    if remainder in {"tomorrow", "tom", "tmr"}:
        return ParsedDue(due=_combine(now.date() + timedelta(days=1), at))

    if remainder in {"next working day", "next workday"}:
        return ParsedDue(due=_combine(next_working_day(now.date()), at))

    if match := re.fullmatch(r"in (\d+) (day|days|week|weeks)", remainder):
        days = int(match.group(1)) * (7 if match.group(2).startswith("week") else 1)
        return ParsedDue(due=_combine(now.date() + timedelta(days=days), at))

    if (
        remainder.startswith("next ")
        and (weekday := WEEKDAYS.get(remainder[5:])) is not None
    ):
        day = _next_weekday(
            now.date() + timedelta(days=1), weekday, allow_same_day=True
        )
        return ParsedDue(due=_combine(day, at))

    if (weekday := WEEKDAYS.get(remainder)) is not None:
        allow_today = at is not None and datetime.combine(now.date(), at) > now
        day = _next_weekday(now.date(), weekday, allow_same_day=allow_today)
        return ParsedDue(due=_combine(day, at))

    if parsed_date := _parse_calendar_date(remainder, now):
        return ParsedDue(due=_combine(parsed_date, at))

    return ParsedDue(due=None)


def _parse_calendar_date(text: str, now: datetime) -> date | None:
    """Parse ISO dates and month-name forms such as 'aug 10' or '10 august'."""
    if match := re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text):
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None

    patterns = (
        (r"([a-z]+) (\d{1,2})(?:st|nd|rd|th)?", 1, 2),
        (r"(\d{1,2})(?:st|nd|rd|th)? (?:of )?([a-z]+)", 2, 1),
    )
    for pattern, month_group, day_group in patterns:
        match = re.fullmatch(pattern, text)
        if not match:
            continue
        month = MONTHS.get(match.group(month_group))
        if month is None:
            continue
        day_of_month = int(match.group(day_group))
        for year in (now.year, now.year + 1):
            try:
                candidate = date(year, month, day_of_month)
            except ValueError:
                return None
            if candidate >= now.date():
                return candidate
        return None

    return None
