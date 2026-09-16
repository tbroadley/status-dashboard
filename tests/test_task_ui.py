import asyncio
import contextlib
import threading
import time
import unittest
from collections.abc import Iterator
from datetime import date, timedelta
from unittest.mock import patch

from status_dashboard.app import StatusDashboard, TodoistDataTable
from status_dashboard.clients.tasks import Task
from status_dashboard.task_store import StoreError


@contextlib.contextmanager
def offline_app() -> Iterator[None]:
    with (
        patch.object(StatusDashboard, "refresh_all"),
        patch.object(StatusDashboard, "_check_for_updates"),
        patch("status_dashboard.app.TODOIST_DUE_NOTIFICATIONS", False),
        patch(
            "status_dashboard.clients.tasks.get_tasks_for_date",
            side_effect=StoreError("Offline"),
        ),
    ):
        yield


class TestTaskUI(unittest.IsolatedAsyncioTestCase):
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
