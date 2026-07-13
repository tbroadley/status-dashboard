import contextlib
import json
import os
import unittest
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from unittest.mock import patch

_ = os.environ.setdefault("TODOIST_API_TOKEN", "fake-token")
_ = os.environ.setdefault("LINEAR_API_KEY", "fake-key")
_ = os.environ.setdefault("LINEAR_PROJECT", "Fake Project")

from textual.coordinate import Coordinate  # noqa: E402
from textual.pilot import Pilot  # noqa: E402

from status_dashboard import app as app_module  # noqa: E402
from status_dashboard.app import MyPRsDataTable, StatusDashboard  # noqa: E402
from status_dashboard.clients import github  # noqa: E402


def _prs() -> list[github.PullRequest]:
    now = datetime.now(tz=timezone.utc)
    return [
        github.PullRequest(
            number=142,
            title="Add retry logic",
            repository="acme/backend",
            url="https://github.com/acme/backend/pull/142",
            created_at=now - timedelta(days=1),
        ),
        github.PullRequest(
            number=87,
            title="Migrate auth",
            repository="acme/frontend",
            url="https://github.com/acme/frontend/pull/87",
            created_at=now - timedelta(days=2),
        ),
    ]


@contextlib.contextmanager
def _patched(config_dir: Path) -> Iterator[None]:
    with (
        patch.dict(os.environ, {"XDG_CONFIG_HOME": str(config_dir)}),
        patch.object(app_module, "HIDDEN_PRS", set[tuple[str, int]]()),
        patch("status_dashboard.clients.github.get_my_prs", return_value=_prs()),
        patch("status_dashboard.clients.github.get_review_requests", return_value=[]),
        patch("status_dashboard.clients.github.get_notifications", return_value=[]),
        patch("status_dashboard.clients.todoist.get_tasks_for_date", return_value=[]),
        patch("status_dashboard.clients.todoist.get_projects", return_value=[]),
        patch("status_dashboard.app.StatusDashboard._check_for_updates"),
    ):
        yield


@contextlib.asynccontextmanager
async def _running_app(app: StatusDashboard) -> AsyncIterator[Pilot[None]]:
    async with app.run_test(size=(120, 55)) as pilot:
        await pilot.pause()
        await pilot.pause()
        yield pilot


def _pr_urls(table: MyPRsDataTable) -> list[str]:
    return [
        str(table.coordinate_to_cell_key(Coordinate(row, 0)).row_key.value)
        for row in range(table.row_count)
    ]


class HidePrTests(unittest.IsolatedAsyncioTestCase):
    async def test_hiding_pr_removes_it_and_persists(self) -> None:
        with TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            with _patched(config_dir):
                app = StatusDashboard()
                async with _running_app(app) as pilot:
                    table = app.query_one("#my-prs-table", MyPRsDataTable)
                    self.assertEqual(table.row_count, 2)

                    _ = table.focus()
                    table.move_cursor(row=0)
                    await pilot.press("H")
                    await pilot.pause()

                    self.assertEqual(
                        _pr_urls(table),
                        ["https://github.com/acme/frontend/pull/87"],
                    )

                self.assertIn(("acme/backend", 142), app_module.HIDDEN_PRS)
                saved = cast(
                    object,
                    json.loads(
                        (
                            config_dir / "status-dashboard" / "hidden_prs.json"
                        ).read_text()
                    ),
                )
                self.assertEqual(saved, [["acme/backend", 142]])

    async def test_hidden_pr_stays_hidden_after_refresh(self) -> None:
        with TemporaryDirectory() as tmp:
            with _patched(Path(tmp)):
                app_module.HIDDEN_PRS.add(("acme/backend", 142))
                app = StatusDashboard()
                async with _running_app(app):
                    table = app.query_one("#my-prs-table", MyPRsDataTable)
                    self.assertEqual(
                        _pr_urls(table),
                        ["https://github.com/acme/frontend/pull/87"],
                    )


if __name__ == "__main__":
    _ = unittest.main()
