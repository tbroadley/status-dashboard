import os
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from typing import cast
from unittest.mock import patch

_ = os.environ.setdefault("TASKS_S3_URI", "s3://example-bucket/tasks.json")
_ = os.environ.setdefault("LINEAR_API_KEY", "fake-key")
_ = os.environ.setdefault("LINEAR_PROJECT", "Fake Project")

from rich.text import Text  # noqa: E402

from status_dashboard import app as app_module  # noqa: E402
from status_dashboard.app import MyPRsDataTable, StatusDashboard  # noqa: E402
from tests.fake_data import fake_prs  # noqa: E402


@contextmanager
def _patched() -> Iterator[None]:
    with (
        patch.object(StatusDashboard, "refresh_all"),
        patch.object(StatusDashboard, "_check_for_updates"),
        patch.object(app_module, "TODOIST_DUE_NOTIFICATIONS", False),
        patch.object(app_module, "HIDDEN_PRS", set[tuple[str, int]]()),
    ):
        yield


class MyPRsTableTests(unittest.IsolatedAsyncioTestCase):
    async def test_line_counts_appear_before_reviewers_with_colors(self) -> None:
        with _patched():
            app = StatusDashboard()
            async with app.run_test(size=(120, 40)) as pilot:
                app._my_prs = fake_prs()  # pyright: ignore[reportPrivateUsage]
                app._render_my_prs_table()  # pyright: ignore[reportPrivateUsage]
                await pilot.pause()

                table = app.query_one("#my-prs-table", MyPRsDataTable)
                self.assertEqual(table.cursor_foreground_priority, "renderable")
                self.assertEqual(
                    [column.label.plain for column in table.columns.values()],
                    [
                        "#",
                        "PR",
                        "Title",
                        "Repo",
                        "Status",
                        "CI",
                        "Cmt",
                        "Changes",
                        "Rvw",
                    ],
                )
                for index, (added, removed, reviewers) in enumerate(
                    [
                        ("+212", "−35", "alice"),
                        ("+940", "−610", ""),
                        ("+18", "−4", "bob, carol"),
                        ("+0", "−0", "dave"),
                    ]
                ):
                    with self.subTest(row=index):
                        row = table.get_row_at(index)
                        self.assertIsInstance(row[7], Text)
                        changes = cast(Text, row[7])
                        self.assertEqual(changes.plain, f"{added} {removed}")
                        self.assertEqual(
                            [
                                (span.start, span.end, span.style)
                                for span in changes.spans
                            ],
                            [
                                (0, len(added), "green"),
                                (len(added) + 1, len(changes.plain), "red"),
                            ],
                        )
                        self.assertEqual(row[8], reviewers)

    async def test_empty_state_has_blank_changes_column(self) -> None:
        with _patched():
            app = StatusDashboard()
            async with app.run_test(size=(120, 40)) as pilot:
                app._render_my_prs_table()  # pyright: ignore[reportPrivateUsage]
                await pilot.pause()

                table = app.query_one("#my-prs-table", MyPRsDataTable)
                self.assertEqual(table.row_count, 1)
                row = table.get_row_at(0)
                self.assertEqual(len(row), 9)
                self.assertEqual(cast(Text, row[2]).plain, "No open PRs")
                self.assertEqual(row[7:], ["", ""])


if __name__ == "__main__":
    _ = unittest.main()
