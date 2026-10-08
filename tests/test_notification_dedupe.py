import os
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

_ = os.environ.setdefault("TASKS_S3_URI", "s3://example-bucket/tasks.json")
_ = os.environ.setdefault("LINEAR_API_KEY", "fake-key")
_ = os.environ.setdefault("LINEAR_PROJECT", "Fake Project")

from status_dashboard import app as app_module  # noqa: E402
from status_dashboard.app import NotificationsDataTable, StatusDashboard  # noqa: E402
from status_dashboard.clients import github  # noqa: E402
from tests.fake_data import fake_prs, fake_review_requests  # noqa: E402


def _notification(repository: str, number: int) -> github.Notification:
    return github.Notification(
        id=f"{repository}#{number}",
        reason="review_requested",
        title=f"PR {number}",
        repository=repository,
        url=f"https://github.com/{repository}/pull/{number}",
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        pr_number=number,
    )


@contextmanager
def _patched(
    hidden_review_requests: set[tuple[str, int]] | None = None,
) -> Iterator[None]:
    with (
        patch.object(StatusDashboard, "refresh_all"),
        patch.object(StatusDashboard, "_check_for_updates"),
        patch.object(app_module, "TODOIST_DUE_NOTIFICATIONS", False),
        patch.object(
            app_module, "HIDDEN_REVIEW_REQUESTS", hidden_review_requests or set()
        ),
        patch.object(app_module, "HIDDEN_PRS", set[tuple[str, int]]()),
        patch.object(app_module, "BLOCKED_REVIEW_TEAMS", set[str]()),
    ):
        yield


def _notification_ids(app: StatusDashboard) -> list[str]:
    table = app.query_one("#notifications-table", NotificationsDataTable)
    return [
        str(row.value).split(":")[1]
        for row in table.rows
        if row.value and str(row.value).startswith("notif:")
    ]


class NotificationDedupeTests(unittest.IsolatedAsyncioTestCase):
    async def test_hides_notifications_for_prs_shown_in_other_tables(self) -> None:
        review_request = fake_review_requests()[0]
        my_pr = fake_prs()[0]
        with _patched():
            app = StatusDashboard()
            async with app.run_test(size=(120, 40)) as pilot:
                app._gh_notifications = [  # pyright: ignore[reportPrivateUsage]
                    _notification(
                        review_request.repository.upper(), review_request.number
                    ),
                    _notification(my_pr.repository, my_pr.number),
                    _notification("acme/other", 9),
                ]
                app._review_requests = [review_request]  # pyright: ignore[reportPrivateUsage]
                app._my_prs = [my_pr]  # pyright: ignore[reportPrivateUsage]
                app._render_review_requests_table()  # pyright: ignore[reportPrivateUsage]
                app._render_my_prs_table()  # pyright: ignore[reportPrivateUsage]
                await pilot.pause()

                self.assertEqual(_notification_ids(app), ["acme/other#9"])

                # Once the review request goes away, its notification shows.
                app._review_requests = []  # pyright: ignore[reportPrivateUsage]
                app._render_review_requests_table()  # pyright: ignore[reportPrivateUsage]
                await pilot.pause()

                self.assertEqual(
                    _notification_ids(app),
                    [
                        f"{review_request.repository.upper()}#{review_request.number}",
                        "acme/other#9",
                    ],
                )

    async def test_shows_notifications_for_manually_hidden_review_requests(
        self,
    ) -> None:
        review_request = fake_review_requests()[0]
        with _patched({(review_request.repository, review_request.number)}):
            app = StatusDashboard()
            async with app.run_test(size=(120, 40)) as pilot:
                app._gh_notifications = [  # pyright: ignore[reportPrivateUsage]
                    _notification(review_request.repository, review_request.number)
                ]
                app._review_requests = [review_request]  # pyright: ignore[reportPrivateUsage]
                app._render_review_requests_table()  # pyright: ignore[reportPrivateUsage]
                await pilot.pause()

                self.assertEqual(
                    _notification_ids(app),
                    [f"{review_request.repository}#{review_request.number}"],
                )

    async def test_removing_self_as_reviewer_marks_pr_notifications_read(
        self,
    ) -> None:
        review_request = fake_review_requests()[0]
        notification = _notification(review_request.repository, review_request.number)
        mark_read = MagicMock(return_value=True)
        with (
            _patched(),
            patch.object(github, "remove_self_as_reviewer", return_value=True),
            patch.object(github, "mark_notification_read", mark_read),
        ):
            app = StatusDashboard()
            async with app.run_test(size=(120, 40)) as pilot:
                app._gh_notifications = [notification]  # pyright: ignore[reportPrivateUsage]
                app._review_requests = [review_request]  # pyright: ignore[reportPrivateUsage]
                app._render_review_requests_table()  # pyright: ignore[reportPrivateUsage]
                _ = app.query_one("#review-requests-table").focus()
                await pilot.pause()

                app.action_remove_self_as_reviewer()
                await pilot.pause()
                await pilot.press("y")
                await app.workers.wait_for_complete()  # pyright: ignore[reportUnknownMemberType]
                await pilot.pause()

                mark_read.assert_called_once_with(notification.id)
                self.assertEqual(_notification_ids(app), [])
                self.assertEqual(app._gh_notifications, [])  # pyright: ignore[reportPrivateUsage]


if __name__ == "__main__":
    _ = unittest.main()


class IssueNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_renders_issue_notifications(self) -> None:
        with _patched():
            app = StatusDashboard()
            async with app.run_test(size=(120, 40)) as pilot:
                app._gh_notifications = [  # pyright: ignore[reportPrivateUsage]
                    github.Notification(
                        id="issue",
                        reason="mention",
                        title="Question about scope",
                        repository="acme/repo",
                        url="https://github.com/acme/repo/issues/7",
                        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                        issue_number=7,
                    )
                ]
                app._render_notifications_table()  # pyright: ignore[reportPrivateUsage]
                await pilot.pause()

                table = app.query_one("#notifications-table", NotificationsDataTable)
                row_key = next(iter(table.rows))
                self.assertEqual(
                    row_key.value,
                    "notif:issue:acme/repo::https://github.com/acme/repo/issues/7",
                )
                row = table.get_row(row_key)
                self.assertEqual(row[1], "#7")
                self.assertEqual(row[4], "mention (issue)")
