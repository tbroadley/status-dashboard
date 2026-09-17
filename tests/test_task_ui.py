import asyncio
import contextlib
import threading
import time
import unittest
from collections.abc import Iterator
from dataclasses import replace
from datetime import date, timedelta
from typing import cast, final
from unittest.mock import patch

from textual.worker import Worker

from status_dashboard.app import StatusDashboard, TodoistDataTable
from status_dashboard.clients.tasks import Task
from status_dashboard.task_store import StoreError


@contextlib.contextmanager
def offline_app() -> Iterator[None]:
    with (
        patch.object(StatusDashboard, "refresh_all"),
        patch.object(StatusDashboard, "_check_for_updates"),
        patch("status_dashboard.app.TODOIST_DUE_NOTIFICATIONS", False),
        patch("status_dashboard.clients.github.get_my_prs", return_value=[]),
        patch("status_dashboard.clients.github.get_review_requests", return_value=[]),
        patch("status_dashboard.clients.github.get_notifications", return_value=[]),
        patch("status_dashboard.clients.linear.get_my_issues", return_value=[]),
        patch("status_dashboard.clients.tasks.get_projects", return_value=[]),
        patch(
            "status_dashboard.clients.tasks.get_tasks_for_date",
            side_effect=StoreError("Offline"),
        ),
    ):
        yield


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


def load_tasks(app: StatusDashboard, tasks: list[Task]) -> TodoistDataTable:
    app._todoist_tasks = tasks.copy()  # pyright: ignore[reportPrivateUsage]
    app._todoist_loaded_date = date.today()  # pyright: ignore[reportPrivateUsage]
    app._render_todoist_table()  # pyright: ignore[reportPrivateUsage]
    table = app.query_one("#todoist-table", TodoistDataTable)
    _ = table.focus()
    return table


def complete_selected_task(app: StatusDashboard) -> Worker[None]:
    app.action_complete_task()
    return next(
        cast(Worker[None], worker)
        for worker in reversed(list(app.workers))
        if worker.name == "_do_complete_todoist_task"
    )


class TestTaskUI(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_does_not_resurrect_completed_task(self):
        for read_before_completion in (True, False):
            for read_finishes_first in (True, False):
                with self.subTest(
                    read_before_completion=read_before_completion,
                    read_finishes_first=read_finishes_first,
                ):
                    await self.check_completion_refresh_race(
                        read_before_completion, read_finishes_first
                    )

    async def test_failed_retry_retains_optimistic_completion(self):
        await self.check_completion_refresh_race(False, False, retry_fails=True)

    async def check_completion_refresh_race(
        self,
        read_before_completion: bool,
        read_finishes_first: bool,
        retry_fails: bool = False,
    ) -> None:
        task = Task("a", "Complete me", False, due_date=date.today().isoformat())
        other = Task("b", "Keep me", False, due_date=date.today().isoformat())
        read_gate = CallGate()
        completion_gate = CallGate()
        completed = False
        reads = 0

        def read_tasks(_day: date) -> list[Task]:
            nonlocal reads
            reads += 1
            snapshot = [other] if completed else [task, other]
            if reads == 1:
                read_gate.pause()
            elif retry_fails:
                raise StoreError("Offline")
            return snapshot

        def complete_task(_task_id: str) -> bool:
            nonlocal completed
            completion_gate.pause()
            completed = True
            return True

        with (
            offline_app(),
            patch(
                "status_dashboard.clients.tasks.get_tasks_for_date",
                side_effect=read_tasks,
            ),
            patch(
                "status_dashboard.clients.tasks.complete_task",
                side_effect=complete_task,
            ),
        ):
            app = StatusDashboard()
            async with app.run_test(size=(120, 55)) as pilot:
                table = load_tasks(app, [task, other])
                await pilot.pause()
                try:
                    refresh: Worker[None] | None = None
                    if read_before_completion:
                        refresh = app._refresh_todoist()  # pyright: ignore[reportPrivateUsage]
                        await read_gate.wait_started()
                    completion = complete_selected_task(app)
                    await completion_gate.wait_started()
                    self.assertEqual(app._todoist_tasks, [other])  # pyright: ignore[reportPrivateUsage]
                    if not read_before_completion:
                        refresh = app._refresh_todoist()  # pyright: ignore[reportPrivateUsage]
                        await read_gate.wait_started()

                    assert refresh is not None
                    if read_finishes_first:
                        read_gate.released.set()
                        await refresh.wait()
                        self.assertEqual(app._todoist_tasks, [other])  # pyright: ignore[reportPrivateUsage]
                        completion_gate.released.set()
                        await completion.wait()
                    else:
                        completion_gate.released.set()
                        await completion.wait()
                        read_gate.released.set()
                        await refresh.wait()

                    self.assertEqual(app._todoist_tasks, [other])  # pyright: ignore[reportPrivateUsage]
                    self.assertEqual(table.row_count, 1)
                    self.assertEqual(str(table.get_row_at(0)[-1]), other.content)
                    await app._refresh_todoist().wait()  # pyright: ignore[reportPrivateUsage]
                    self.assertEqual(app._todoist_tasks, [other])  # pyright: ignore[reportPrivateUsage]
                finally:
                    read_gate.released.set()
                    completion_gate.released.set()
                    await app.workers.wait_for_complete()  # pyright: ignore[reportUnknownMemberType]

    async def test_failed_completion_rolls_back_only_on_original_day(self):
        for navigate in (False, True):
            for raises_error in (False, True):
                with self.subTest(navigate=navigate, raises_error=raises_error):
                    await self.check_failed_completion(navigate, raises_error)

    async def check_failed_completion(self, navigate: bool, raises_error: bool) -> None:
        today = date.today()
        task = Task("a", "Complete me", False, due_date=today.isoformat())
        tomorrow_task = Task(
            "b", "Tomorrow", False, due_date=(today + timedelta(days=1)).isoformat()
        )
        gate = CallGate()

        def complete_task(_task_id: str) -> bool:
            gate.pause()
            if raises_error:
                raise StoreError("Offline")
            return False

        def read_tasks(day: date) -> list[Task]:
            return [task] if day == today else [tomorrow_task]

        with (
            offline_app(),
            patch(
                "status_dashboard.clients.tasks.get_tasks_for_date",
                side_effect=read_tasks,
            ),
            patch(
                "status_dashboard.clients.tasks.complete_task",
                side_effect=complete_task,
            ),
        ):
            app = StatusDashboard()
            async with app.run_test(size=(120, 55)) as pilot:
                table = load_tasks(app, [task])
                await pilot.pause()
                try:
                    completion = complete_selected_task(app)
                    await gate.wait_started()
                    if navigate:
                        app.action_todoist_next_day()
                    await app._refresh_todoist().wait()  # pyright: ignore[reportPrivateUsage]
                    self.assertEqual(
                        app._todoist_tasks,  # pyright: ignore[reportPrivateUsage]
                        [tomorrow_task] if navigate else [],
                    )
                    gate.released.set()
                    await completion.wait()
                    expected = [tomorrow_task] if navigate else [task]
                    self.assertEqual(app._todoist_tasks, expected)  # pyright: ignore[reportPrivateUsage]
                    self.assertEqual(table.row_count, 1)
                    self.assertEqual(str(table.get_row_at(0)[-1]), expected[0].content)
                    await app._refresh_todoist().wait()  # pyright: ignore[reportPrivateUsage]
                    self.assertEqual(app._todoist_tasks, expected)  # pyright: ignore[reportPrivateUsage]
                finally:
                    gate.released.set()
                    await app.workers.wait_for_complete()  # pyright: ignore[reportUnknownMemberType]

    async def test_concurrent_completions_stay_hidden_independently(self):
        tasks = [Task(task_id, task_id, False) for task_id in ("a", "b", "c")]
        stored = tasks.copy()
        gates = {task.id: CallGate() for task in tasks[:2]}

        def read_tasks(_day: date) -> list[Task]:
            return stored.copy()

        def complete_task(task_id: str) -> bool:
            gates[task_id].pause()
            stored[:] = [task for task in stored if task.id != task_id]
            return True

        with (
            offline_app(),
            patch(
                "status_dashboard.clients.tasks.get_tasks_for_date",
                side_effect=read_tasks,
            ),
            patch(
                "status_dashboard.clients.tasks.complete_task",
                side_effect=complete_task,
            ),
        ):
            app = StatusDashboard()
            async with app.run_test(size=(120, 55)) as pilot:
                _ = load_tasks(app, tasks)
                await pilot.pause()
                try:
                    first = complete_selected_task(app)
                    await gates["a"].wait_started()
                    second = complete_selected_task(app)
                    await gates["b"].wait_started()
                    gates["a"].released.set()
                    await first.wait()
                    await app._refresh_todoist().wait()  # pyright: ignore[reportPrivateUsage]
                    self.assertEqual(app._todoist_tasks, tasks[2:])  # pyright: ignore[reportPrivateUsage]
                    gates["b"].released.set()
                    await second.wait()
                    await app._refresh_todoist().wait()  # pyright: ignore[reportPrivateUsage]
                    self.assertEqual(app._todoist_tasks, tasks[2:])  # pyright: ignore[reportPrivateUsage]
                finally:
                    for gate in gates.values():
                        gate.released.set()
                    await app.workers.wait_for_complete()  # pyright: ignore[reportUnknownMemberType]

    async def test_undo_can_show_completed_task_again(self):
        task = Task("a", "Complete me", False)
        with (
            offline_app(),
            patch("status_dashboard.clients.tasks.complete_task", return_value=True),
            patch("status_dashboard.clients.tasks.reopen_task", return_value=True),
            patch(
                "status_dashboard.clients.tasks.get_tasks_for_date",
                return_value=[task],
            ),
        ):
            app = StatusDashboard()
            async with app.run_test(size=(120, 55)) as pilot:
                _ = load_tasks(app, [task])
                await pilot.pause()
                await complete_selected_task(app).wait()
                self.assertEqual(app._todoist_tasks, [])  # pyright: ignore[reportPrivateUsage]
                action = app._undo_stack.pop()  # pyright: ignore[reportPrivateUsage]
                assert action is not None
                await app._execute_undo(action).wait()  # pyright: ignore[reportPrivateUsage]
                await app.workers.wait_for_complete()  # pyright: ignore[reportUnknownMemberType]
                self.assertEqual(app._todoist_tasks, [task])  # pyright: ignore[reportPrivateUsage]

    async def test_recurring_task_can_show_next_occurrence(self):
        task = Task(
            "a",
            "Daily task",
            False,
            due_date=date.today().isoformat(),
            is_recurring=True,
        )
        next_task = replace(
            task, due_date=(date.today() + timedelta(days=1)).isoformat()
        )
        with (
            offline_app(),
            patch("status_dashboard.clients.tasks.complete_task", return_value=True),
            patch(
                "status_dashboard.clients.tasks.get_tasks_for_date",
                return_value=[next_task],
            ),
        ):
            app = StatusDashboard()
            async with app.run_test(size=(120, 55)) as pilot:
                _ = load_tasks(app, [task])
                await pilot.pause()
                await complete_selected_task(app).wait()
                app.action_todoist_next_day()
                await app._refresh_todoist().wait()  # pyright: ignore[reportPrivateUsage]
                self.assertEqual(app._todoist_tasks, [next_task])  # pyright: ignore[reportPrivateUsage]

    async def test_same_day_failure_retains_loaded_tasks(self):
        with offline_app():
            app = StatusDashboard()
            async with app.run_test(size=(120, 55)) as pilot:
                task = Task(
                    "a", "Loaded task", False, due_date=date.today().isoformat()
                )
                app._todoist_tasks = [task]  # pyright: ignore[reportPrivateUsage]
                app._todoist_loaded_date = date.today()  # pyright: ignore[reportPrivateUsage]
                await app._refresh_todoist().wait()  # pyright: ignore[reportPrivateUsage]
                await pilot.pause()
                self.assertEqual(app._todoist_tasks, [task])  # pyright: ignore[reportPrivateUsage]

    async def test_day_navigation_does_not_mislabel_stale_rows(self):
        with offline_app():
            app = StatusDashboard()
            async with app.run_test(size=(120, 55)) as pilot:
                app._todoist_tasks = [Task("a", "Old task", False)]  # pyright: ignore[reportPrivateUsage]
                app._todoist_loaded_date = date.today()  # pyright: ignore[reportPrivateUsage]
                app._render_todoist_table()  # pyright: ignore[reportPrivateUsage]
                table = app.query_one("#todoist-table", TodoistDataTable)
                _ = table.focus()
                await pilot.pause()
                app.action_todoist_next_day()
                await app._refresh_todoist().wait()  # pyright: ignore[reportPrivateUsage]
                await pilot.pause()
                selected = app._todoist_selected_date  # pyright: ignore[reportPrivateUsage]
                self.assertEqual(selected, date.today() + timedelta(days=1))
                self.assertEqual(app._todoist_tasks, [])  # pyright: ignore[reportPrivateUsage]
                self.assertIn("Tasks not loaded", str(table.get_row_at(0)[-1]))

    async def test_reorder_writes_are_serialized(self):
        active = 0
        maximum = 0
        lock = threading.Lock()
        calls: list[dict[str, int]] = []

        def save(orders: dict[str, int]) -> bool:
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
                calls.append(orders)
            time.sleep(0.02)
            with lock:
                active -= 1
            return True

        with patch(
            "status_dashboard.clients.tasks.update_day_orders", side_effect=save
        ):
            app = StatusDashboard()
            results = await asyncio.gather(
                app._save_task_order({"a": 1}),  # pyright: ignore[reportPrivateUsage]
                app._save_task_order({"a": 2}),  # pyright: ignore[reportPrivateUsage]
            )
        self.assertEqual(results, [True, True])
        self.assertEqual(maximum, 1)
        self.assertEqual(calls, [{"a": 1}, {"a": 2}])
