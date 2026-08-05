import contextlib
import datetime as dt
import json
import os
import unittest
from collections.abc import Iterator
from typing import Any, cast
from unittest.mock import MagicMock, patch

_ = os.environ.setdefault("TASKS_SPREADSHEET_ID", "fake-sheet")

from status_dashboard.clients import sheets  # noqa: E402

TODAY = dt.date.today()
YESTERDAY = TODAY - dt.timedelta(days=1)
TOMORROW = TODAY + dt.timedelta(days=1)


def _row(
    task_id: str,
    content: str,
    due: str,
    *,
    project: str = "Inbox",
    description: str = "",
    recurrence: str = "",
    order: str = "0",
    done: str = "FALSE",
) -> list[str]:
    return [task_id, content, project, description, due, recurrence, order, done, ""]


@contextlib.contextmanager
def _gws(responses: list[object]) -> Iterator[list[list[str]]]:
    """Patch the gws subprocess, returning each queued response in turn.

    Yields the list of commands issued, so tests can assert on what was sent.
    A response of `None` simulates a non-zero exit.
    """
    commands: list[list[str]] = []
    queue = list(responses)

    def fake_run(command: list[str], **kwargs: object) -> MagicMock:
        del kwargs
        commands.append(command)
        payload = queue.pop(0) if queue else {}
        if payload is None:
            return MagicMock(returncode=1, stdout="", stderr="boom")
        return MagicMock(returncode=0, stdout=json.dumps(payload), stderr="")

    with patch("status_dashboard.clients.sheets.subprocess.run", side_effect=fake_run):
        yield commands


def _sent(command: list[str]) -> dict[str, Any]:
    """The --json body of an issued gws command."""
    return cast(dict[str, Any], json.loads(command[command.index("--json") + 1]))


def _ranges_of(body: dict[str, Any]) -> dict[str, str]:
    """Map of range -> written value from a values.batchUpdate body."""
    data = cast(list[dict[str, Any]], body["data"])
    return {entry["range"]: entry["values"][0][0] for entry in data}


def _values(rows: list[list[str]]) -> dict[str, object]:
    return {"values": rows}


class GetTasksForDate(unittest.TestCase):
    def test_parses_rows_into_tasks(self):
        rows = [
            _row(
                "a",
                "Write tests",
                TODAY.isoformat(),
                project="Personal",
                description="notes",
                order="2",
            ),  # fmt: skip
        ]
        with _gws([_values(rows)]):
            tasks = sheets.get_tasks_for_date(TODAY)

        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        self.assertEqual(task.id, "a")
        self.assertEqual(task.content, "Write tests")
        self.assertEqual(task.project_id, "Personal")
        self.assertEqual(task.description, "notes")
        self.assertEqual(task.day_order, 2)
        self.assertFalse(task.is_completed)
        self.assertFalse(task.is_recurring)

    def test_today_includes_overdue_but_not_future(self):
        rows = [
            _row("overdue", "Overdue", YESTERDAY.isoformat()),
            _row("today", "Today", TODAY.isoformat()),
            _row("future", "Future", TOMORROW.isoformat()),
        ]
        with _gws([_values(rows)]):
            tasks = sheets.get_tasks_for_date(TODAY)

        self.assertEqual([t.id for t in tasks], ["overdue", "today"])

    def test_other_dates_are_exact(self):
        rows = [
            _row("overdue", "Overdue", YESTERDAY.isoformat()),
            _row("future", "Future", TOMORROW.isoformat()),
        ]
        with _gws([_values(rows)]):
            tasks = sheets.get_tasks_for_date(TOMORROW)

        self.assertEqual([t.id for t in tasks], ["future"])

    def test_completed_and_undated_excluded(self):
        rows = [
            _row("done", "Done", TODAY.isoformat(), done="TRUE"),
            _row("undated", "Undated", ""),
            _row("open", "Open", TODAY.isoformat()),
        ]
        with _gws([_values(rows)]):
            tasks = sheets.get_tasks_for_date(TODAY)

        self.assertEqual([t.id for t in tasks], ["open"])

    def test_sorted_by_order(self):
        rows = [
            _row("c", "Third", TODAY.isoformat(), order="7"),
            _row("a", "First", TODAY.isoformat(), order="1"),
            _row("b", "Second", TODAY.isoformat(), order="3"),
        ]
        with _gws([_values(rows)]):
            tasks = sheets.get_tasks_for_date(TODAY)

        self.assertEqual([t.id for t in tasks], ["a", "b", "c"])

    def test_due_time_extracted_from_datetime(self):
        due = dt.datetime.combine(TODAY, dt.time(10, 0)).astimezone().isoformat()
        with _gws([_values([_row("a", "Standup", due)])]):
            tasks = sheets.get_tasks_for_date(TODAY)

        self.assertEqual(tasks[0].due_time, "10:00")
        self.assertEqual(tasks[0].due_date, TODAY.isoformat())

    def test_all_day_task_has_no_time(self):
        with _gws([_values([_row("a", "Read", TODAY.isoformat())])]):
            tasks = sheets.get_tasks_for_date(TODAY)

        self.assertIsNone(tasks[0].due_time)

    def test_short_rows_tolerated(self):
        with _gws([_values([["a", "Sparse", "", "", TODAY.isoformat()]])]):
            tasks = sheets.get_tasks_for_date(TODAY)

        self.assertEqual(tasks[0].content, "Sparse")
        self.assertEqual(tasks[0].day_order, 0)
        self.assertFalse(tasks[0].is_completed)

    def test_gws_failure_returns_empty(self):
        with _gws([None]):
            self.assertEqual(sheets.get_tasks_for_date(TODAY), [])

    def test_api_error_payload_returns_empty(self):
        with _gws([{"error": {"code": 403, "message": "denied"}}]):
            self.assertEqual(sheets.get_tasks_for_date(TODAY), [])


class CompleteTask(unittest.TestCase):
    def test_recurring_task_rolls_forward(self):
        rows = [_row("a", "Standup", TODAY.isoformat(), recurrence="every day at 10am")]
        with _gws([_values(rows), {}]) as commands:
            self.assertTrue(sheets.complete_task("a"))

        body = _sent(commands[1])
        written = body["data"][0]
        self.assertEqual(written["range"], "Tasks!E2")
        self.assertNotIn("TRUE", json.dumps(body))

    def test_non_recurring_task_is_marked_done(self):
        with _gws([_values([_row("a", "One off", TODAY.isoformat())]), {}]) as commands:
            self.assertTrue(sheets.complete_task("a"))

        body = _sent(commands[1])
        ranges = _ranges_of(body)
        self.assertEqual(ranges["Tasks!H2"], "TRUE")
        self.assertTrue(ranges["Tasks!I2"])

    def test_unparseable_recurrence_falls_back_to_done(self):
        rows = [_row("a", "Odd", TODAY.isoformat(), recurrence="every blue moon")]
        with _gws([_values(rows), {}]) as commands:
            self.assertTrue(sheets.complete_task("a"))

        body = _sent(commands[1])
        ranges = _ranges_of(body)
        self.assertEqual(ranges["Tasks!H2"], "TRUE")

    def test_missing_task_fails(self):
        with _gws([_values([_row("other", "Other", TODAY.isoformat())])]):
            self.assertFalse(sheets.complete_task("missing"))

    def test_future_due_advances_past_that_date(self):
        """Completing a not-yet-due recurring task must move it, not recompute it."""
        due = dt.datetime.combine(TOMORROW, dt.time(10, 0)).astimezone().isoformat()
        rows = [_row("a", "Standup", due, recurrence="every workday at 10am")]
        with _gws([_values(rows), {}]) as commands:
            self.assertTrue(sheets.complete_task("a"))

        body = _sent(commands[1])
        written = body["data"][0]["values"][0][0]
        self.assertGreater(written[:10], TOMORROW.isoformat())

    def test_overdue_task_advances_into_the_future(self):
        stale = (TODAY - dt.timedelta(days=10)).isoformat()
        rows = [_row("a", "Standup", stale, recurrence="every day at 10am")]
        with _gws([_values(rows), {}]) as commands:
            self.assertTrue(sheets.complete_task("a"))

        body = _sent(commands[1])
        written = body["data"][0]["values"][0][0]
        self.assertGreaterEqual(written[:10], TODAY.isoformat())


class RecurrenceBase(unittest.TestCase):
    def test_uses_due_date_when_it_is_later_than_now(self):
        now = dt.datetime(2026, 8, 5, 9, 0)
        base = sheets._recurrence_base("2026-08-09T10:00:00", now)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(base, dt.datetime(2026, 8, 9, 10, 0))

    def test_uses_now_when_task_is_overdue(self):
        now = dt.datetime(2026, 8, 5, 9, 0)
        base = sheets._recurrence_base("2026-07-01T10:00:00", now)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(base, now)

    def test_falls_back_to_now_for_blank_or_bad_dates(self):
        now = dt.datetime(2026, 8, 5, 9, 0)
        for due in ("", "not a date"):
            with self.subTest(due=due):
                self.assertEqual(sheets._recurrence_base(due, now), now)  # pyright: ignore[reportPrivateUsage]

    def test_row_number_accounts_for_header(self):
        rows = [
            _row("first", "First", TODAY.isoformat()),
            _row("second", "Second", TODAY.isoformat()),
        ]
        with _gws([_values(rows), {}]) as commands:
            _ = sheets.complete_task("second")

        body = _sent(commands[1])
        self.assertTrue(all(e["range"].endswith("3") for e in body["data"]))


class CreateTask(unittest.TestCase):
    def test_appends_row_and_returns_id(self):
        with _gws([{"updates": {"updatedRange": "Tasks!A5:I5"}}]) as commands:
            task_id = sheets.create_task("New task", "tomorrow", "details")

        self.assertIsNotNone(task_id)
        body = _sent(commands[0])
        row = body["values"][0]
        self.assertEqual(row[sheets.COL_ID], task_id)
        self.assertEqual(row[sheets.COL_CONTENT], "New task")
        self.assertEqual(row[sheets.COL_DESCRIPTION], "details")
        self.assertEqual(row[sheets.COL_DUE], TOMORROW.isoformat())
        self.assertEqual(row[sheets.COL_DONE], "FALSE")

    def test_recurring_due_string_is_stored(self):
        with _gws([{}]) as commands:
            _ = sheets.create_task("Standup", "every workday at 10am")

        body = _sent(commands[0])
        self.assertEqual(
            body["values"][0][sheets.COL_RECURRENCE], "every workday at 10am"
        )

    def test_failure_returns_none(self):
        with _gws([None]):
            self.assertIsNone(sheets.create_task("Doomed"))


class UpdateDayOrders(unittest.TestCase):
    def test_batches_all_updates_into_one_call(self):
        rows = [
            _row("a", "A", TODAY.isoformat()),
            _row("b", "B", TODAY.isoformat()),
        ]
        with _gws([_values(rows), {}]) as commands:
            self.assertTrue(sheets.update_day_orders({"a": 3, "b": 1}))

        self.assertEqual(len(commands), 2)
        body = _sent(commands[1])
        ranges = _ranges_of(body)
        self.assertEqual(ranges, {"Tasks!G2": "3", "Tasks!G3": "1"})

    def test_empty_input_is_a_noop(self):
        with _gws([]) as commands:
            self.assertTrue(sheets.update_day_orders({}))
        self.assertEqual(commands, [])

    def test_unknown_ids_are_skipped(self):
        with _gws([_values([_row("a", "A", TODAY.isoformat())]), {}]) as commands:
            self.assertTrue(sheets.update_day_orders({"a": 1, "ghost": 2}))

        body = _sent(commands[1])
        self.assertEqual(len(body["data"]), 1)


class GetTask(unittest.TestCase):
    def test_returns_todoist_shaped_payload(self):
        rows = [
            _row(
                "a",
                "Standup",
                TODAY.isoformat(),
                project="Work",
                description="daily",
                recurrence="every workday at 10am",
            ),  # fmt: skip
        ]
        with _gws([_values(rows)]):
            payload = sheets.get_task("a")

        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["content"], "Standup")
        self.assertEqual(payload["description"], "daily")
        self.assertEqual(payload["project_id"], "Work")
        self.assertEqual(payload["due"]["string"], "every workday at 10am")
        self.assertTrue(payload["due"]["is_recurring"])

    def test_undated_task_has_no_due_block(self):
        with _gws([_values([_row("a", "Someday", "")])]):
            payload = sheets.get_task("a")

        assert payload is not None
        self.assertIsNone(payload["due"])


class GetProjects(unittest.TestCase):
    def test_returns_sorted_distinct_names(self):
        rows = [
            _row("a", "A", TODAY.isoformat(), project="Reading"),
            _row("b", "B", TODAY.isoformat(), project="Inbox"),
            _row("c", "C", TODAY.isoformat(), project="Reading"),
            _row("d", "D", TODAY.isoformat(), project=""),
        ]
        with _gws([_values(rows)]):
            projects = sheets.get_projects()

        self.assertEqual([p.name for p in projects], ["Inbox", "Reading"])


class DeleteTask(unittest.TestCase):
    def test_deletes_correct_zero_indexed_row(self):
        rows = [
            _row("a", "A", TODAY.isoformat()),
            _row("b", "B", TODAY.isoformat()),
        ]
        sheets._sheet_id_cache = None  # pyright: ignore[reportPrivateUsage]
        responses: list[object] = [
            _values(rows),
            {"sheets": [{"properties": {"title": "Tasks", "sheetId": 12345}}]},
            {},
        ]
        with _gws(responses) as commands:
            self.assertTrue(sheets.delete_task("b"))

        body = _sent(commands[2])
        target = body["requests"][0]["deleteDimension"]["range"]
        self.assertEqual(target["sheetId"], 12345)
        self.assertEqual((target["startIndex"], target["endIndex"]), (2, 3))


class DeferTask(unittest.TestCase):
    def test_moves_to_next_working_day(self):
        with _gws([_values([_row("a", "A", TODAY.isoformat())]), {}]) as commands:
            self.assertTrue(sheets.defer_task("a"))

        body = _sent(commands[1])
        written = body["data"][0]["values"][0][0]
        expected = sheets.dates.next_working_day().isoformat()
        self.assertEqual(written, expected)


if __name__ == "__main__":
    _ = unittest.main()
