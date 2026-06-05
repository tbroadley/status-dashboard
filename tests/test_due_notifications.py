import contextlib
import os
import unittest
from collections.abc import AsyncIterator, Iterator
from datetime import date, datetime
from unittest.mock import MagicMock, patch

_ = os.environ.setdefault("TODOIST_API_TOKEN", "fake-token")
_ = os.environ.setdefault("LINEAR_API_KEY", "fake-key")
_ = os.environ.setdefault("LINEAR_PROJECT", "Fake Project")

from textual.pilot import Pilot  # noqa: E402

from status_dashboard.app import StatusDashboard  # noqa: E402
from status_dashboard.clients import todoist  # noqa: E402

TODAY = date.today()


def _task(task_id: str, due_time: str | None) -> todoist.Task:
    return todoist.Task(
        id=task_id,
        content=f"Task {task_id}",
        is_completed=False,
        url="https://example.com",
        due_date=TODAY.isoformat(),
        due_time=due_time,
    )


def _at(hour: int, minute: int) -> datetime:
    return datetime(TODAY.year, TODAY.month, TODAY.day, hour, minute)


@contextlib.contextmanager
def _patched(
    tasks: list[todoist.Task], clock: dict[str, datetime]
) -> Iterator[MagicMock]:
    mock_datetime = MagicMock(wraps=datetime)
    mock_datetime.now = lambda: clock["now"]
    send = MagicMock(return_value=True)
    with (
        patch(
            "status_dashboard.clients.todoist.get_tasks_for_date", return_value=tasks
        ),
        patch("status_dashboard.clients.github.get_my_prs", return_value=[]),
        patch("status_dashboard.clients.github.get_review_requests", return_value=[]),
        patch("status_dashboard.clients.github.get_notifications", return_value=[]),
        patch("status_dashboard.db.goals.get_goals_for_week", return_value=[]),
        patch("status_dashboard.db.goals.get_week_metrics", return_value=None),
        patch("status_dashboard.app.StatusDashboard._check_for_updates"),
        patch("status_dashboard.app.datetime", mock_datetime),
        patch("status_dashboard.notifications.send_desktop_notification", send),
    ):
        yield send


@contextlib.asynccontextmanager
async def _running_app(app: StatusDashboard) -> AsyncIterator[Pilot[None]]:
    async with app.run_test(size=(120, 55)) as pilot:
        await pilot.pause()
        await pilot.pause()
        yield pilot


def _sent_contents(send: MagicMock) -> list[str]:
    return [c.args[1] for c in send.call_args_list]


class DueNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_overdue_at_startup_is_seeded_silently(self) -> None:
        clock = {"now": _at(12, 0)}
        with _patched([_task("1", "09:00")], clock) as send:
            async with _running_app(StatusDashboard()):
                pass
            send.assert_not_called()

    async def test_notifies_once_when_due_time_arrives(self) -> None:
        clock = {"now": _at(8, 0)}
        with _patched([_task("1", "09:00")], clock) as send:
            app = StatusDashboard()
            async with _running_app(app) as pilot:
                send.assert_not_called()

                clock["now"] = _at(9, 1)
                _ = app._check_todoist_due_times()  # pyright: ignore[reportPrivateUsage]
                await pilot.pause()
                await pilot.pause()

            self.assertEqual(_sent_contents(send), ["Task 1"])

    async def test_does_not_notify_twice_for_same_task(self) -> None:
        clock = {"now": _at(8, 0)}
        with _patched([_task("1", "09:00")], clock) as send:
            app = StatusDashboard()
            async with _running_app(app) as pilot:
                clock["now"] = _at(9, 1)
                _ = app._check_todoist_due_times()  # pyright: ignore[reportPrivateUsage]
                await pilot.pause()
                await pilot.pause()
                _ = app._check_todoist_due_times()  # pyright: ignore[reportPrivateUsage]
                await pilot.pause()
                await pilot.pause()

            self.assertEqual(send.call_count, 1)

    async def test_tasks_without_due_time_never_notify(self) -> None:
        clock = {"now": _at(23, 59)}
        with _patched([_task("1", None)], clock) as send:
            app = StatusDashboard()
            async with _running_app(app) as pilot:
                _ = app._check_todoist_due_times()  # pyright: ignore[reportPrivateUsage]
                await pilot.pause()
                await pilot.pause()
            send.assert_not_called()


if __name__ == "__main__":
    _ = unittest.main()
