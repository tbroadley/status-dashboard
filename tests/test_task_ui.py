import asyncio
import contextlib
import threading
import time
import unittest
from collections.abc import Iterator
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import cast, final
from unittest.mock import patch

from textual.worker import Worker

from status_dashboard.app import StatusDashboard, TodoistDataTable
from status_dashboard.clients import tasks
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
    app._todoist_server_tasks = tasks.copy()  # pyright: ignore[reportPrivateUsage]
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
                    self.assertEqual(app._todoist_tasks, tasks[2:])  # pyright: ignore[reportPrivateUsage]
                    await app._refresh_todoist().wait()  # pyright: ignore[reportPrivateUsage]
                    self.assertEqual(app._todoist_tasks, tasks[2:])  # pyright: ignore[reportPrivateUsage]
                    gates["a"].released.set()
                    await first.wait()
                    # Writes are serialized: the second starts after the first.
                    await gates["b"].wait_started()
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
                _ = load_tasks(app, [task])
                await app._refresh_todoist().wait()  # pyright: ignore[reportPrivateUsage]
                await pilot.pause()
                self.assertEqual(app._todoist_tasks, [task])  # pyright: ignore[reportPrivateUsage]

    async def test_day_navigation_does_not_mislabel_stale_rows(self):
        with offline_app():
            app = StatusDashboard()
            async with app.run_test(size=(120, 55)) as pilot:
                app._todoist_server_tasks = [Task("a", "Old task", False)]  # pyright: ignore[reportPrivateUsage]
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


@final
class FakeTaskStore:
    """In-memory stand-in for the task client, with optional per-call gates.

    Reads return copies, as real reads build fresh objects from S3.
    """

    def __init__(self, tasks: list[Task]) -> None:
        self.tasks: list[Task] = [replace(task) for task in tasks]
        self.gates: dict[str, CallGate] = {}
        self.created: list[dict[str, object]] = []

    def gate(self, name: str) -> CallGate:
        self.gates[name] = CallGate()
        return self.gates[name]

    def _pause(self, name: str) -> None:
        if gate := self.gates.pop(name, None):
            gate.pause()

    def read(self, day: date) -> list[Task]:
        snapshot = [
            replace(task)
            for task in sorted(self.tasks, key=lambda task: task.day_order)
            if tasks.is_due_on(task.due_date, day)
        ]
        self._pause("read")
        return snapshot

    def create(
        self,
        content: str,
        due_string: str,
        description: str,
        *,
        task_id: str,
        day_orders: dict[str, int] | None,
        now: datetime,
    ) -> str:
        self._pause("create")
        self.created.append({"task_id": task_id, "day_orders": day_orders})
        orders = day_orders or {}
        self.tasks.append(
            tasks.new_task(
                task_id,
                content,
                due_string,
                description,
                day_order=orders.get(task_id, 0),
                now=now,
            )
        )
        self._apply_orders(orders)
        return task_id

    def _apply_orders(self, orders: dict[str, int]) -> None:
        for task in self.tasks:
            if task.id in orders:
                task.day_order = orders[task.id]

    def update_orders(self, orders: dict[str, int]) -> bool:
        self._pause("orders")
        self._apply_orders(orders)
        return True

    def remove(self, task_id: str) -> bool:
        self._pause("remove")
        self.tasks = [task for task in self.tasks if task.id != task_id]
        return True

    def defer(self, task_id: str) -> bool:
        self._pause("remove")
        for task in self.tasks:
            if task.id == task_id:
                task.due_date = (date.today() + timedelta(days=3)).isoformat()
        return True

    @contextlib.contextmanager
    def patched(self) -> Iterator[None]:
        client = "status_dashboard.clients.tasks"
        with (
            offline_app(),
            patch(f"{client}.get_tasks_for_date", side_effect=self.read),
            patch(f"{client}.create_task", side_effect=self.create),
            patch(f"{client}.update_day_orders", side_effect=self.update_orders),
            patch(f"{client}.delete_task", side_effect=self.remove),
            patch(f"{client}.complete_task", side_effect=self.remove),
            patch(f"{client}.defer_task", side_effect=self.defer),
            patch(f"{client}.get_task", return_value=None),
        ):
            yield


def today_tasks(*names: str) -> list[Task]:
    return [
        Task(
            name.lower(),
            name,
            False,
            day_order=index,
            due_date=date.today().isoformat(),
        )
        for index, name in enumerate(names)
    ]


def shown(table: TodoistDataTable) -> list[str]:
    return [str(table.get_row_at(row)[-1]) for row in range(table.row_count)]


def cursor_task(table: TodoistDataTable) -> str:
    return str(table.get_row_at(table.cursor_row)[-1])


def worker(app: StatusDashboard, name: str) -> Worker[None]:
    return next(
        cast(Worker[None], w) for w in reversed(list(app.workers)) if w.name == name
    )


class TestOptimisticTaskUpdates(unittest.IsolatedAsyncioTestCase):
    async def test_created_task_keeps_place_through_refresh_and_save(self):
        store = FakeTaskStore(today_tasks("A", "B"))
        with store.patched():
            app = StatusDashboard()
            async with app.run_test(size=(120, 55)) as pilot:
                table = load_tasks(app, store.read(date.today()))
                table.move_cursor(row=1)
                await pilot.pause()
                create_gate = store.gate("create")
                app._handle_todoist_task_created(  # pyright: ignore[reportPrivateUsage]
                    {"content": "New", "due_string": "today"}, "todoist:b:", 1
                )
                await create_gate.wait_started()
                self.assertEqual(shown(table), ["A", "New", "B"])
                self.assertEqual(cursor_task(table), "New")

                # A periodic refresh lands before the save does.
                await app._refresh_todoist().wait()  # pyright: ignore[reportPrivateUsage]
                self.assertEqual(shown(table), ["A", "New", "B"])
                self.assertEqual(cursor_task(table), "New")

                create_gate.released.set()
                await app.workers.wait_for_complete()  # pyright: ignore[reportUnknownMemberType]
                self.assertEqual(shown(table), ["A", "New", "B"])
                self.assertEqual(cursor_task(table), "New")

                # The task and its position were saved in one write.
                created = store.created[0]
                self.assertEqual(
                    created["day_orders"], {"a": 0, created["task_id"]: 1, "b": 2}
                )
                await app._refresh_todoist().wait()  # pyright: ignore[reportPrivateUsage]
                self.assertEqual(shown(table), ["A", "New", "B"])
                self.assertEqual(cursor_task(table), "New")

    async def test_created_task_with_time_is_shown_immediately(self):
        store = FakeTaskStore(today_tasks("A"))
        with store.patched():
            app = StatusDashboard()
            async with app.run_test(size=(120, 55)) as pilot:
                table = load_tasks(app, store.read(date.today()))
                await pilot.pause()
                create_gate = store.gate("create")
                app._handle_todoist_task_created(  # pyright: ignore[reportPrivateUsage]
                    {"content": "Timed", "due_string": "today 11:59pm"},
                    "todoist:a:",
                    0,
                )
                await create_gate.wait_started()
                self.assertEqual(shown(table), ["Timed", "A"])
                self.assertEqual(str(table.get_row_at(0)[3]), "23:59")
                create_gate.released.set()
                await app.workers.wait_for_complete()  # pyright: ignore[reportUnknownMemberType]
                self.assertEqual(shown(table), ["Timed", "A"])

    async def test_task_for_another_day_is_not_shown_but_is_confirmed(self):
        store = FakeTaskStore(today_tasks("A"))
        with store.patched():
            app = StatusDashboard()
            async with app.run_test(size=(120, 55)) as pilot:
                table = load_tasks(app, store.read(date.today()))
                await pilot.pause()
                with patch.object(app, "notify") as notify:
                    app._handle_todoist_task_created(  # pyright: ignore[reportPrivateUsage]
                        {"content": "Later", "due_string": "in 3 days"},
                        "todoist:a:",
                        0,
                    )
                    await app.workers.wait_for_complete()  # pyright: ignore[reportUnknownMemberType]
                self.assertEqual(shown(table), ["A"])
                later = date.today() + timedelta(days=3)
                notify.assert_called_with(
                    f"Task created for {later.strftime('%a %b %d')}"
                )

    async def test_stale_refresh_does_not_resurrect_removed_task(self):
        for action in ("complete", "defer", "delete"):
            for read_finishes_first in (True, False):
                with self.subTest(
                    action=action, read_finishes_first=read_finishes_first
                ):
                    await self.check_stale_refresh(action, read_finishes_first)

    async def check_stale_refresh(self, action: str, read_finishes_first: bool) -> None:
        store = FakeTaskStore(today_tasks("A", "B", "C"))
        with store.patched():
            app = StatusDashboard()
            async with app.run_test(size=(120, 55)) as pilot:
                table = load_tasks(app, store.read(date.today()))
                table.move_cursor(row=1)
                await pilot.pause()
                read_gate = store.gate("read")
                refresh = app._refresh_todoist()  # pyright: ignore[reportPrivateUsage]
                await read_gate.wait_started()  # reads the list while B exists

                remove_gate = store.gate("remove")
                if action == "complete":
                    app.action_complete_task()
                elif action == "defer":
                    app.action_defer_task()
                else:
                    app.action_delete_task()
                    await pilot.press("y")
                await remove_gate.wait_started()
                self.assertEqual(shown(table), ["A", "C"])
                self.assertEqual(cursor_task(table), "C")

                if read_finishes_first:
                    read_gate.released.set()
                    await refresh.wait()
                    self.assertEqual(shown(table), ["A", "C"])
                    remove_gate.released.set()
                else:
                    remove_gate.released.set()
                    await worker(app, f"_do_{action}_todoist_task").wait()
                    self.assertEqual(shown(table), ["A", "C"])
                    read_gate.released.set()
                await app.workers.wait_for_complete()  # pyright: ignore[reportUnknownMemberType]
                self.assertEqual(shown(table), ["A", "C"])
                self.assertEqual(cursor_task(table), "C")

    async def test_cursor_keeps_its_row_when_its_task_disappears(self):
        for row, expected in ((1, "C"), (3, "C")):
            with self.subTest(row=row):
                store = FakeTaskStore(today_tasks("A", "B", "C", "D"))
                with store.patched():
                    app = StatusDashboard()
                    async with app.run_test(size=(120, 55)) as pilot:
                        table = load_tasks(app, store.read(date.today()))
                        table.move_cursor(row=row)
                        await pilot.pause()
                        # Removed by another client.
                        removed = shown(table)[row].lower()
                        store.tasks = [t for t in store.tasks if t.id != removed]
                        await app._refresh_todoist().wait()  # pyright: ignore[reportPrivateUsage]
                        self.assertEqual(table.cursor_row, min(row, 2))
                        self.assertEqual(cursor_task(table), expected)

    async def test_completing_keeps_cursor_on_same_row(self):
        store = FakeTaskStore(today_tasks("A", "B", "C"))
        with store.patched():
            app = StatusDashboard()
            async with app.run_test(size=(120, 55)) as pilot:
                table = load_tasks(app, store.read(date.today()))
                await pilot.pause()
                app.action_complete_task()
                self.assertEqual((table.cursor_row, cursor_task(table)), (0, "B"))
                await app.workers.wait_for_complete()  # pyright: ignore[reportUnknownMemberType]
                self.assertEqual((table.cursor_row, cursor_task(table)), (0, "B"))

    async def test_reorder_survives_refresh_before_it_is_saved(self):
        store = FakeTaskStore(today_tasks("A", "B", "C"))
        with store.patched():
            app = StatusDashboard()
            async with app.run_test(size=(120, 55)) as pilot:
                table = load_tasks(app, store.read(date.today()))
                table.move_cursor(row=2)
                await pilot.pause()
                orders_gate = store.gate("orders")
                app.action_move_task_up()
                self.assertEqual(shown(table), ["A", "C", "B"])
                await app._refresh_todoist().wait()  # pyright: ignore[reportPrivateUsage]
                self.assertEqual(shown(table), ["A", "C", "B"])
                self.assertEqual(cursor_task(table), "C")

                await orders_gate.wait_started()  # debounced save in flight
                await app._refresh_todoist().wait()  # pyright: ignore[reportPrivateUsage]
                self.assertEqual(shown(table), ["A", "C", "B"])
                orders_gate.released.set()
                await app.workers.wait_for_complete()  # pyright: ignore[reportUnknownMemberType]
                await app._refresh_todoist().wait()  # pyright: ignore[reportPrivateUsage]
                self.assertEqual(shown(table), ["A", "C", "B"])
                self.assertEqual(cursor_task(table), "C")

    async def test_older_read_never_replaces_newer_one(self):
        store = FakeTaskStore(today_tasks("A", "B"))
        with store.patched():
            app = StatusDashboard()
            async with app.run_test(size=(120, 55)) as pilot:
                table = load_tasks(app, store.read(date.today()))
                await pilot.pause()
                read_gate = store.gate("read")
                slow = app._refresh_todoist()  # pyright: ignore[reportPrivateUsage]
                await read_gate.wait_started()
                store.tasks = today_tasks("A")  # B removed by another client
                await app._refresh_todoist().wait()  # pyright: ignore[reportPrivateUsage]
                self.assertEqual(shown(table), ["A"])
                read_gate.released.set()
                await slow.wait()
                self.assertEqual(shown(table), ["A"])

    async def test_failed_create_removes_optimistic_task(self):
        store = FakeTaskStore(today_tasks("A"))
        with (
            store.patched(),
            patch(
                "status_dashboard.clients.tasks.create_task",
                side_effect=StoreError("Offline"),
            ),
        ):
            app = StatusDashboard()
            async with app.run_test(size=(120, 55)) as pilot:
                table = load_tasks(app, store.read(date.today()))
                await pilot.pause()
                app._handle_todoist_task_created(  # pyright: ignore[reportPrivateUsage]
                    {"content": "New", "due_string": "today"}, "todoist:a:", 0
                )
                await app.workers.wait_for_complete()  # pyright: ignore[reportUnknownMemberType]
                self.assertEqual(shown(table), ["A"])
                self.assertIsNone(app._todoist_order_overlay)  # pyright: ignore[reportPrivateUsage]
