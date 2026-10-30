import asyncio
import contextlib
import os
import threading
import unittest
from collections.abc import Iterator
from datetime import date, timedelta
from typing import final, override
from unittest.mock import patch

from textual.widgets import Static

from status_dashboard import task_store
from status_dashboard.app import StatusDashboard, TodoistDataTable
from status_dashboard.task_store import decode_document, encode_document
from tests.test_task_store import FakeS3, client_error

TODAY = date.today()
YESTERDAY = TODAY - timedelta(days=1)
TOMORROW = TODAY + timedelta(days=1)


@final
class CallGate:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.released = threading.Event()

    def pause(self) -> None:
        self.started.set()
        if not self.released.wait(timeout=5):
            raise TimeoutError("Client call was not released")

    async def wait_started(self) -> None:
        if not await asyncio.to_thread(self.started.wait, 5):
            raise TimeoutError("Client call did not start")


@final
class GatedS3(FakeS3):
    """FakeS3 whose next read or write of the task list can be held open.

    Writes are gated at the main document, after the backup, so a gated
    write hasn't landed until the gate is released.
    """

    def __init__(self, rows: list[list[str]]) -> None:
        super().__init__(rows)
        self.gates: dict[str, CallGate] = {}
        self.fail_writes: bool = False
        self.lock: threading.Lock = threading.Lock()
        self.active_writes: int = 0
        self.max_active_writes: int = 0

    def gate(self, op: str) -> CallGate:
        self.gates[op] = CallGate()
        return self.gates[op]

    def _pause(self, op: str) -> None:
        if gate := self.gates.pop(op, None):
            gate.pause()

    @override
    def get_object(self, **kwargs: object) -> dict[str, object]:
        # Read before pausing: a gated read sees the list as it was.
        try:
            response = super().get_object(**kwargs)
        except Exception:
            self._pause("get")
            raise
        self._pause("get")
        return response

    @override
    def put_object(self, **kwargs: object) -> dict[str, object]:
        if kwargs["Key"] != "tasks.json":
            return super().put_object(**kwargs)
        with self.lock:
            self.active_writes += 1
            self.max_active_writes = max(self.max_active_writes, self.active_writes)
        try:
            self._pause("put")
            if self.fail_writes:
                raise client_error("AccessDenied", 403)
            return super().put_object(**kwargs)
        finally:
            with self.lock:
                self.active_writes -= 1

    def rows(self) -> list[list[str]]:
        return decode_document(self.objects["tasks.json"])

    def set_rows(self, rows: list[list[str]]) -> None:
        self.objects["tasks.json"] = encode_document(rows)

    def main_writes(self) -> int:
        return sum(
            1 for op, key, _ in self.calls if op == "put" and key == "tasks.json"
        )


def task_row(
    task_id: str,
    content: str | None = None,
    due: str | date = TODAY,
    order: int = 0,
    recurrence: str = "",
    description: str = "",
) -> list[str]:
    due_text = due.isoformat() if isinstance(due, date) else due
    return [
        task_id,
        content or task_id.upper(),
        "",
        description,
        due_text,
        recurrence,
        str(order),
        "FALSE",
        "",
    ]


def today_rows(*names: str) -> list[list[str]]:
    return [task_row(name.lower(), name, order=i) for i, name in enumerate(names)]


@contextlib.contextmanager
def dashboard_env(s3: GatedS3) -> Iterator[None]:
    with (
        patch.dict(os.environ, {"TASKS_S3_URI": "s3://example-bucket/tasks.json"}),
        s3.installed(),
        patch.object(StatusDashboard, "refresh_all"),
        patch.object(StatusDashboard, "_check_for_updates"),
        patch.object(StatusDashboard, "_warm_task_store"),
        patch("status_dashboard.app.TODOIST_DUE_NOTIFICATIONS", False),
    ):
        yield


def shown(table: TodoistDataTable) -> list[str]:
    return [str(table.get_row_at(row)[-1]) for row in range(table.row_count)]


def cursor_task(table: TodoistDataTable) -> str:
    return str(table.get_row_at(table.cursor_row)[-1])


def stored(s3: GatedS3) -> dict[str, list[str]]:
    return {row[0]: row for row in s3.rows()}


class TaskUITest(unittest.IsolatedAsyncioTestCase):
    """Runs the dashboard against a fake S3, through the real task client."""

    s3: GatedS3  # pyright: ignore[reportUninitializedInstanceVariable]
    app: StatusDashboard  # pyright: ignore[reportUninitializedInstanceVariable]
    table: TodoistDataTable  # pyright: ignore[reportUninitializedInstanceVariable]

    @contextlib.asynccontextmanager
    async def running(self, rows: list[list[str]]):
        self.s3 = GatedS3(rows)
        with dashboard_env(self.s3):
            self.app = StatusDashboard()
            async with self.app.run_test(size=(120, 55)) as pilot:
                await self.refresh()
                self.table = self.app.query_one("#todoist-table", TodoistDataTable)
                _ = self.table.focus()
                await pilot.pause()
                try:
                    yield pilot
                finally:
                    for gate in self.s3.gates.values():
                        gate.released.set()
                    await self.settle()

    async def refresh(self) -> None:
        await self.app._refresh_todoist().wait()  # pyright: ignore[reportPrivateUsage]

    async def settle(self) -> None:
        await self.app.workers.wait_for_complete()  # pyright: ignore[reportUnknownMemberType]

    def shown(self) -> list[str]:
        return shown(self.table)


class TestOptimisticTaskUpdates(TaskUITest):
    async def test_actions_show_immediately_and_survive_refreshes(self):
        for action in ("complete", "defer", "delete"):
            for read_finishes_first in (True, False):
                with self.subTest(
                    action=action, read_finishes_first=read_finishes_first
                ):
                    await self.check_stale_refresh(action, read_finishes_first)

    async def check_stale_refresh(self, action: str, read_finishes_first: bool):
        async with self.running(today_rows("A", "B", "C")) as pilot:
            self.table.move_cursor(row=1)
            await pilot.pause()
            read_gate = self.s3.gate("get")
            refresh = self.app._refresh_todoist()  # pyright: ignore[reportPrivateUsage]
            await read_gate.wait_started()  # reads the list while B is open

            write_gate = self.s3.gate("put")
            if action == "complete":
                await pilot.press("c")
            elif action == "defer":
                await pilot.press("n")
            else:
                await pilot.press("d")
                await pilot.press("y")
            self.assertEqual(self.shown(), ["A", "C"])
            self.assertEqual(cursor_task(self.table), "C")
            await write_gate.wait_started()

            if read_finishes_first:
                read_gate.released.set()
                await refresh.wait()
                self.assertEqual(self.shown(), ["A", "C"])
                write_gate.released.set()
            else:
                write_gate.released.set()
                await asyncio.sleep(0.05)
                self.assertEqual(self.shown(), ["A", "C"])
                read_gate.released.set()
            await self.settle()
            self.assertEqual(self.shown(), ["A", "C"])
            await self.refresh()
            self.assertEqual(self.shown(), ["A", "C"])
            self.assertEqual(cursor_task(self.table), "C")
            b = stored(self.s3).get("b")
            if action == "complete":
                assert b is not None
                self.assertEqual(b[7], "TRUE")
            elif action == "defer":
                assert b is not None
                self.assertGreater(b[4], TODAY.isoformat())
            else:
                self.assertIsNone(b)

    async def test_failed_save_rolls_back(self):
        for navigate in (False, True):
            with self.subTest(navigate=navigate):
                rows = [task_row("a", "Complete me"), task_row("b", due=TOMORROW)]
                async with self.running(rows) as pilot:
                    self.s3.fail_writes = True
                    gate = self.s3.gate("put")
                    with patch.object(self.app, "notify") as notify:
                        await pilot.press("c")
                        self.assertEqual(self.shown(), ["No tasks for today"])
                        await gate.wait_started()
                        if navigate:
                            await pilot.press("l")
                            self.assertEqual(self.shown(), ["B"])
                        gate.released.set()
                        await self.settle()
                    self.assertEqual(
                        self.shown(), ["B"] if navigate else ["Complete me"]
                    )
                    self.assertIn(
                        "Task storage unavailable",
                        [c.kwargs.get("title") for c in notify.call_args_list],
                    )
                    self.assertTrue(self.app._undo_stack.is_empty())  # pyright: ignore[reportPrivateUsage]

    async def test_edit_of_a_task_deleted_elsewhere_reports_failure(self):
        async with self.running(today_rows("A", "B")) as pilot:
            self.s3.set_rows(today_rows("B"))  # another client deletes A
            with patch.object(self.app, "notify") as notify:
                await pilot.press("c")
                await self.settle()
            notify.assert_any_call("Failed to complete task", severity="error")
            self.assertEqual(self.shown(), ["B"])

    async def test_quick_actions_are_batched_into_one_save(self):
        async with self.running(today_rows("A", "B", "C", "D")) as pilot:
            gate = self.s3.gate("put")
            await pilot.press("c")
            await gate.wait_started()
            await pilot.press("c")
            await pilot.press("n")
            self.assertEqual(self.shown(), ["D"])
            await self.refresh()  # overlaps a save, so it is dropped
            self.assertEqual(self.shown(), ["D"])
            gate.released.set()
            await self.settle()
            self.assertEqual(self.shown(), ["D"])
            self.assertEqual(self.s3.main_writes(), 2)
            self.assertEqual(self.s3.max_active_writes, 1)
            rows = stored(self.s3)
            self.assertEqual([rows[i][7] for i in "abc"], ["TRUE", "TRUE", "FALSE"])
            self.assertGreater(rows["c"][4], TODAY.isoformat())

    async def test_saves_need_no_extra_reads(self):
        async with self.running(today_rows("A", "B")) as pilot:
            before = len(self.s3.calls)
            await pilot.press("c")
            await self.settle()
            # A backup and the conditional write; the cached version is current.
            self.assertEqual(
                [op for op, _, _ in self.s3.calls[before:]], ["put", "put"]
            )

    async def test_undo_restores_immediately_even_before_the_save(self):
        async with self.running(today_rows("A", "B")) as pilot:
            gate = self.s3.gate("put")
            await pilot.press("c")
            self.assertEqual(self.shown(), ["B"])
            await gate.wait_started()
            await pilot.press("z")
            self.assertEqual(self.shown(), ["A", "B"])
            gate.released.set()
            await self.settle()
            self.assertEqual(self.shown(), ["A", "B"])
            self.assertEqual(stored(self.s3)["a"][7:], ["FALSE", ""])

    async def test_undo_delete_restores_the_task(self):
        async with self.running(today_rows("A", "B")) as pilot:
            await pilot.press("d")
            await pilot.press("y")
            await self.settle()
            self.assertNotIn("a", stored(self.s3))
            await pilot.press("z")
            self.assertEqual(self.shown(), ["A", "B"])
            await self.settle()
            self.assertEqual(stored(self.s3)["a"], today_rows("A")[0])

    async def test_recurring_completion_shows_next_occurrence(self):
        rows = [task_row("a", "Daily", recurrence="every day")]
        async with self.running(rows) as pilot:
            gate = self.s3.gate("put")
            await pilot.press("c")
            self.assertEqual(self.shown(), ["No tasks for today"])
            await pilot.press("l")
            self.assertEqual(self.shown(), ["Daily"])
            gate.released.set()
            await self.settle()
            self.assertEqual(self.shown(), ["Daily"])

    async def test_reschedule_overdue_to_today_in_one_save(self):
        rows = [
            task_row("a", "Old", due=YESTERDAY, order=0),
            task_row("b", "Today", order=1),
            task_row("c", "Older", due=TODAY - timedelta(days=3), order=2),
            task_row(
                "d",
                "Standup",
                due=f"{YESTERDAY.isoformat()}T10:00:00",
                recurrence="every day at 10am",
                order=3,
            ),
        ]
        async with self.running(rows) as pilot:
            overdue = [str(self.table.get_row_at(r)[1]) for r in range(4)]
            self.assertEqual(overdue, ["!", "", "!", "!"])
            gate = self.s3.gate("put")
            await pilot.press("T")
            marks = [str(self.table.get_row_at(r)[1]) for r in range(4)]
            self.assertEqual(marks, ["", "", "", ""])
            self.assertEqual(str(self.table.get_row_at(3)[3]), "10:00")
            gate.released.set()
            await self.settle()
            self.assertEqual(self.s3.main_writes(), 1)
            saved = stored(self.s3)
            self.assertEqual(saved["a"][4], TODAY.isoformat())
            self.assertEqual(saved["c"][4], TODAY.isoformat())
            self.assertTrue(saved["d"][4].startswith(f"{TODAY.isoformat()}T10:00"))

            await pilot.press("z")
            marks = [str(self.table.get_row_at(r)[1]) for r in range(4)]
            self.assertEqual(marks, ["!", "", "!", "!"])
            await self.settle()
            self.assertEqual(stored(self.s3)["a"][4], YESTERDAY.isoformat())

    async def test_reschedule_from_an_earlier_day(self):
        rows = [task_row("a", "Old", due=YESTERDAY), task_row("b", "Today")]
        async with self.running(rows) as pilot:
            await pilot.press("h")
            self.assertEqual(self.shown(), ["Old"])
            await pilot.press("T")
            self.assertEqual(
                self.shown(), ["No tasks on " + YESTERDAY.strftime("%a %b %d")]
            )
            await pilot.press("t")
            self.assertEqual(self.shown(), ["Old", "Today"])
            await self.settle()
            self.assertEqual(stored(self.s3)["a"][4], TODAY.isoformat())

    async def test_day_navigation_is_instant_from_loaded_rows(self):
        rows = [task_row("a", "Today"), task_row("b", "Tomorrow", due=TOMORROW)]
        async with self.running(rows) as pilot:
            reads = len(self.s3.calls)
            await pilot.press("l")
            self.assertEqual(self.shown(), ["Tomorrow"])
            await pilot.press("h")
            self.assertEqual(self.shown(), ["Today"])
            self.assertEqual(len(self.s3.calls), reads)
            await self.settle()

    async def test_day_navigation_before_any_load_says_so(self):
        s3 = GatedS3([])
        s3.fail = client_error("AccessDenied", 403)
        with dashboard_env(s3):
            app = StatusDashboard()
            async with app.run_test(size=(120, 55)) as pilot:
                await app._refresh_todoist().wait()  # pyright: ignore[reportPrivateUsage]
                table = app.query_one("#todoist-table", TodoistDataTable)
                _ = table.focus()
                await pilot.press("l")
                self.assertIn("Tasks not loaded", shown(table)[0])
                await app.workers.wait_for_complete()  # pyright: ignore[reportUnknownMemberType]

    async def test_failed_refresh_keeps_loaded_tasks(self):
        async with self.running(today_rows("A")):
            self.s3.fail = client_error("AccessDenied", 403)
            await self.refresh()
            self.assertEqual(self.shown(), ["A"])
            self.s3.fail = None

    async def test_open_link_needs_no_request(self):
        rows = [task_row("a", "Read", description="see https://example.com/x")]
        async with self.running(rows) as pilot:
            before = len(self.s3.calls)
            with patch("webbrowser.open") as browser:
                await pilot.press("o")
            browser.assert_called_once_with("https://example.com/x")
            self.assertEqual(len(self.s3.calls), before)

    async def test_edit_shows_immediately(self):
        async with self.running(today_rows("A", "B")):
            gate = self.s3.gate("put")
            self.app._handle_todoist_task_edited(  # pyright: ignore[reportPrivateUsage]
                {"task_id": "a", "content": "Renamed", "due_string": "tomorrow"}
            )
            self.assertEqual(self.shown(), ["B"])
            gate.released.set()
            await self.settle()
            self.assertEqual(stored(self.s3)["a"][1], "Renamed")
            self.assertEqual(stored(self.s3)["a"][4], TOMORROW.isoformat())

    async def test_created_task_keeps_place_through_refresh_and_save(self):
        async with self.running(today_rows("A", "B")) as pilot:
            self.table.move_cursor(row=1)
            await pilot.pause()
            gate = self.s3.gate("put")
            self.app._handle_todoist_task_created(  # pyright: ignore[reportPrivateUsage]
                {"content": "New", "due_string": "today"}, "todoist:b:", 1
            )
            self.assertEqual(self.shown(), ["A", "New", "B"])
            self.assertEqual(cursor_task(self.table), "New")
            await gate.wait_started()

            await self.refresh()  # lands before the save does
            self.assertEqual(self.shown(), ["A", "New", "B"])
            self.assertEqual(cursor_task(self.table), "New")

            gate.released.set()
            await self.settle()
            self.assertEqual(self.shown(), ["A", "New", "B"])
            self.assertEqual(cursor_task(self.table), "New")
            await self.refresh()
            self.assertEqual(self.shown(), ["A", "New", "B"])
            self.assertEqual(self.s3.main_writes(), 1)

    async def test_created_task_with_time_is_shown_immediately(self):
        async with self.running(today_rows("A")):
            self.app._handle_todoist_task_created(  # pyright: ignore[reportPrivateUsage]
                {"content": "Timed", "due_string": "today 11:59pm"}, "todoist:a:", 0
            )
            self.assertEqual(self.shown(), ["Timed", "A"])
            self.assertEqual(str(self.table.get_row_at(0)[3]), "23:59")
            await self.settle()
            self.assertEqual(self.shown(), ["Timed", "A"])

    async def test_task_for_another_day_is_not_shown_but_is_confirmed(self):
        async with self.running(today_rows("A")):
            with patch.object(self.app, "notify") as notify:
                self.app._handle_todoist_task_created(  # pyright: ignore[reportPrivateUsage]
                    {"content": "Later", "due_string": "in 3 days"}, "todoist:a:", 0
                )
                await self.settle()
            self.assertEqual(self.shown(), ["A"])
            later = TODAY + timedelta(days=3)
            notify.assert_called_with(f"Task created for {later.strftime('%a %b %d')}")

    async def test_failed_create_removes_optimistic_task(self):
        async with self.running(today_rows("A")):
            self.s3.fail_writes = True
            self.app._handle_todoist_task_created(  # pyright: ignore[reportPrivateUsage]
                {"content": "New", "due_string": "today"}, "todoist:a:", 0
            )
            self.assertEqual(self.shown(), ["New", "A"])
            await self.settle()
            self.assertEqual(self.shown(), ["A"])

    async def test_cursor_keeps_its_row_when_its_task_disappears(self):
        for row, expected in ((1, "C"), (3, "C")):
            with self.subTest(row=row):
                async with self.running(today_rows("A", "B", "C", "D")) as pilot:
                    self.table.move_cursor(row=row)
                    await pilot.pause()
                    removed = self.shown()[row].lower()
                    self.s3.set_rows([r for r in self.s3.rows() if r[0] != removed])
                    await self.refresh()
                    self.assertEqual(self.table.cursor_row, min(row, 2))
                    self.assertEqual(cursor_task(self.table), expected)

    async def test_completing_keeps_cursor_on_same_row(self):
        async with self.running(today_rows("A", "B", "C")) as pilot:
            await pilot.press("c")
            self.assertEqual((self.table.cursor_row, cursor_task(self.table)), (0, "B"))
            await self.settle()
            self.assertEqual((self.table.cursor_row, cursor_task(self.table)), (0, "B"))

    async def test_reorder_shows_immediately_and_survives_refresh(self):
        async with self.running(today_rows("A", "B", "C")) as pilot:
            self.table.move_cursor(row=2)
            await pilot.pause()
            gate = self.s3.gate("put")
            await pilot.press("K")
            self.assertEqual(self.shown(), ["A", "C", "B"])
            await gate.wait_started()
            await pilot.press("K")
            self.assertEqual(self.shown(), ["C", "A", "B"])
            await self.refresh()
            self.assertEqual(self.shown(), ["C", "A", "B"])
            self.assertEqual(cursor_task(self.table), "C")
            gate.released.set()
            await self.settle()
            await self.refresh()
            self.assertEqual(self.shown(), ["C", "A", "B"])
            self.assertEqual(cursor_task(self.table), "C")

    async def test_older_read_never_replaces_newer_one(self):
        async with self.running(today_rows("A", "B")):
            gate = self.s3.gate("get")
            slow = self.app._refresh_todoist()  # pyright: ignore[reportPrivateUsage]
            await gate.wait_started()
            self.s3.set_rows(today_rows("A"))  # B removed by another client
            await self.refresh()
            self.assertEqual(self.shown(), ["A"])
            gate.released.set()
            await slow.wait()
            self.assertEqual(self.shown(), ["A"])

    async def test_changes_from_other_clients_appear_on_refresh(self):
        async with self.running(today_rows("A")):
            self.s3.set_rows(today_rows("A", "B"))
            await self.refresh()
            self.assertEqual(self.shown(), ["A", "B"])

    async def test_save_merges_with_changes_from_other_clients(self):
        async with self.running(today_rows("A", "B")) as pilot:
            self.s3.set_rows(today_rows("A", "B", "X"))  # not yet seen here
            await pilot.press("c")
            await self.settle()
            # The save returns the merged list, so X shows without a refresh.
            self.assertEqual(self.shown(), ["B", "X"])
            self.assertEqual(sorted(stored(self.s3)), ["a", "b", "x"])


class TestSavedCopy(unittest.IsolatedAsyncioTestCase):
    async def test_startup_shows_saved_copy_until_s3_answers(self):
        s3 = GatedS3(today_rows("A"))
        with dashboard_env(s3):
            _ = task_store.shared_store().read()  # an earlier run saved A
            s3.set_rows(today_rows("A", "B"))
            task_store.reset_shared_store()
            gate = s3.gate("get")
            app = StatusDashboard()
            async with app.run_test(size=(120, 55)) as pilot:
                table = app.query_one("#todoist-table", TodoistDataTable)
                title = app.query_one("#todoist .panel-title", Static)
                self.assertEqual(shown(table), ["A"])
                self.assertIn("saved copy", str(title.render()))
                refresh = app._refresh_todoist()  # pyright: ignore[reportPrivateUsage]
                await gate.wait_started()
                gate.released.set()
                await refresh.wait()
                await pilot.pause()
                self.assertEqual(shown(table), ["A", "B"])
                self.assertEqual(str(title.render()), "Tasks (Today)")


class TestAwsSignIn(TaskUITest):
    """Expired AWS credentials trigger one `aws sso login`, then a retry."""

    @contextlib.contextmanager
    def sso_login(self, succeeds: bool) -> Iterator[list[int]]:
        calls: list[int] = []

        async def fake_login() -> bool:
            calls.append(1)
            await asyncio.sleep(0.05)  # concurrent requests join this login
            if succeeds:
                self.s3.fail = None
            return succeeds

        with (
            patch("status_dashboard.app.aws_login.sso_login", fake_login),
            patch(
                "status_dashboard.app.notifications.send_desktop_notification"
            ) as desktop,
        ):
            yield calls
        if calls:
            desktop.assert_called_once()
            self.assertEqual(len(desktop.call_args.args), 2, "should be silent")

    async def test_expired_credentials_sign_in_and_retry_the_save(self):
        async with self.running(today_rows("A", "B")) as pilot:
            self.s3.fail = client_error("ExpiredToken")
            with (
                self.sso_login(succeeds=True) as logins,
                patch.object(self.app, "notify") as notify,
            ):
                await pilot.press("c")
                refresh = self.app._refresh_todoist()  # pyright: ignore[reportPrivateUsage]
                await refresh.wait()
                await self.settle()
            self.assertEqual(logins, [1])
            self.assertEqual(self.shown(), ["B"])
            self.assertEqual(stored(self.s3)["a"][7], "TRUE")
            titles = [c.kwargs.get("title") for c in notify.call_args_list]
            self.assertIn("AWS sign-in expired", titles)
            self.assertNotIn("Task storage unavailable", titles)

    async def test_failed_sign_in_reports_error_and_is_not_retried_at_once(self):
        async with self.running(today_rows("A")) as pilot:
            self.s3.fail = client_error("ExpiredToken")
            with (
                self.sso_login(succeeds=False) as logins,
                patch.object(self.app, "notify") as notify,
            ):
                await pilot.press("c")
                await self.settle()
                self.assertEqual(self.shown(), ["A"])
                await self.refresh()
            self.assertEqual(logins, [1])
            titles = [c.kwargs.get("title") for c in notify.call_args_list]
            self.assertIn("Task storage unavailable", titles)
            self.assertEqual(titles.count("AWS sign-in expired"), 1)
            self.s3.fail = None

    async def test_disabled_auto_sign_in_only_reports_the_error(self):
        async with self.running(today_rows("A")):
            self.s3.fail = client_error("ExpiredToken")
            with (
                patch.dict(os.environ, {"AWS_AUTO_SSO_LOGIN": "false"}),
                self.sso_login(succeeds=True) as logins,
            ):
                await self.refresh()
            self.assertEqual(logins, [])
            self.s3.fail = None
