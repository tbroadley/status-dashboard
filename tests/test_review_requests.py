import os
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from typing import cast
from unittest.mock import patch

_ = os.environ.setdefault("TASKS_SPREADSHEET_ID", "fake-sheet")
_ = os.environ.setdefault("LINEAR_API_KEY", "fake-key")
_ = os.environ.setdefault("LINEAR_PROJECT", "Fake Project")

from rich.text import Text  # noqa: E402

from status_dashboard import app as app_module  # noqa: E402
from status_dashboard.app import ReviewRequestsDataTable, StatusDashboard  # noqa: E402
from tests.fake_data import fake_review_requests  # noqa: E402


@contextmanager
def _patched() -> Iterator[None]:
    with (
        patch.object(StatusDashboard, "refresh_all"),
        patch.object(StatusDashboard, "_check_for_updates"),
        patch.object(app_module, "TODOIST_DUE_NOTIFICATIONS", False),
        patch.object(app_module, "HIDDEN_REVIEW_REQUESTS", set[tuple[str, int]]()),
        patch.object(app_module, "BLOCKED_REVIEW_TEAMS", set[str]()),
    ):
        yield


class ReviewRequestsTableTests(unittest.IsolatedAsyncioTestCase):
    async def test_line_counts_appear_before_reviewed_with_colors(self) -> None:
        with _patched():
            app = StatusDashboard()
            async with app.run_test(size=(120, 40)) as pilot:
                app._review_requests = fake_review_requests()  # pyright: ignore[reportPrivateUsage]
                app._render_review_requests_table()  # pyright: ignore[reportPrivateUsage]
                await pilot.pause()

                table = app.query_one("#review-requests-table", ReviewRequestsDataTable)
                self.assertEqual(table.cursor_foreground_priority, "renderable")
                self.assertEqual(
                    [column.label.plain for column in table.columns.values()],
                    [
                        "#",
                        "PR",
                        "Title",
                        "Repo",
                        "Author",
                        "Age",
                        "Changes",
                        "Reviewed",
                    ],
                )
                for index, (added, removed, reviewed) in enumerate(
                    [("+128", "−42", ""), ("+1050", "−0", "✓"), ("+0", "−73", "")]
                ):
                    with self.subTest(row=index):
                        row = table.get_row_at(index)
                        self.assertIsInstance(row[6], Text)
                        changes = cast(Text, row[6])
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
                        self.assertEqual(row[7], reviewed)

                table.move_cursor(row=1)
                app._review_requests[1] = replace(  # pyright: ignore[reportPrivateUsage]
                    app._review_requests[1],  # pyright: ignore[reportPrivateUsage]
                    additions=0,
                    deletions=0,
                )
                app._render_review_requests_table()  # pyright: ignore[reportPrivateUsage]
                await pilot.pause()
                self.assertEqual(table.cursor_row, 1)
                self.assertEqual(cast(Text, table.get_row_at(1)[6]).plain, "+0 −0")
                self.assertEqual(table.get_row_at(1)[7], "✓")

    async def test_empty_state_has_blank_changes_column(self) -> None:
        with _patched():
            app = StatusDashboard()
            async with app.run_test(size=(120, 40)) as pilot:
                app._render_review_requests_table()  # pyright: ignore[reportPrivateUsage]
                await pilot.pause()

                table = app.query_one("#review-requests-table", ReviewRequestsDataTable)
                self.assertEqual(table.row_count, 1)
                row = table.get_row_at(0)
                self.assertEqual(len(row), 8)
                self.assertEqual(cast(Text, row[2]).plain, "No review requests")
                self.assertEqual(row[6:], ["", ""])


if __name__ == "__main__":
    _ = unittest.main()
