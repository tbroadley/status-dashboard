from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import cast
import unittest
from unittest.mock import call, patch

from status_dashboard.clients import github


def _make_pr(
    number: int,
    url: str,
    created_at: datetime,
    assignees: list[str] | None = None,
) -> github.PullRequest:
    return github.PullRequest(
        number=number,
        title=f"PR {number}",
        repository="acme/repo",
        url=url,
        created_at=created_at,
        assignees=assignees or [],
    )


def _make_notification(
    reason: str, subject_type: str = "PullRequest", repository: str = "acme/repo"
) -> dict[str, object]:
    return {
        "id": "123",
        "reason": reason,
        "unread": True,
        "updated_at": "2026-01-01T00:00:00Z",
        "repository": {"full_name": repository},
        "subject": {
            "type": subject_type,
            "title": "Update tests",
            "url": f"https://api.github.com/repos/{repository}/pulls/1",
            "latest_comment_url": (
                f"https://api.github.com/repos/{repository}/pulls/comments/456"
            ),
        },
    }


class GetMyPRsTests(unittest.TestCase):
    def test_includes_authored_and_assigned_prs(self) -> None:
        now = datetime.now(timezone.utc)
        authored_pr = _make_pr(
            1,
            "https://github.com/acme/repo/pull/1",
            now - timedelta(hours=2),
        )
        assigned_pr = _make_pr(
            2,
            "https://github.com/acme/repo/pull/2",
            now - timedelta(hours=1),
        )
        extra_repo_pr = _make_pr(
            3,
            "https://github.com/outside/repo/pull/3",
            now - timedelta(hours=3),
        )

        responses = {
            "author:@me state:open type:pr org:METR repo:outside/repo": [authored_pr],
            "assignee:@me state:open type:pr org:METR repo:outside/repo": [
                assigned_pr,
                authored_pr,
                extra_repo_pr,
            ],
        }

        def run_my_prs_query(query: str) -> list[github.PullRequest]:
            return responses[query]

        with (
            patch.object(github, "_get_extra_pr_repos", return_value=["outside/repo"]),
            patch.object(
                github,
                "_run_my_prs_query",
                side_effect=run_my_prs_query,
            ) as run_query,
        ):
            prs = github.get_my_prs(["METR"])

        self.assertEqual(
            [pr.url for pr in prs],
            [assigned_pr.url, authored_pr.url, extra_repo_pr.url],
        )
        self.assertCountEqual(
            run_query.call_args_list,
            [
                call("author:@me state:open type:pr org:METR repo:outside/repo"),
                call("assignee:@me state:open type:pr org:METR repo:outside/repo"),
            ],
        )

    def test_includes_authored_pr_assigned_to_someone_else(self) -> None:
        now = datetime.now(timezone.utc)
        handed_off_pr = _make_pr(
            4,
            "https://github.com/acme/repo/pull/4",
            now,
            assignees=["someone-else"],
        )

        responses = {
            "author:@me state:open type:pr org:METR": [handed_off_pr],
            "assignee:@me state:open type:pr org:METR": [],
        }

        def run_my_prs_query(query: str) -> list[github.PullRequest]:
            return responses[query]

        with (
            patch.object(github, "_get_extra_pr_repos", return_value=[]),
            patch.object(
                github,
                "_run_my_prs_query",
                side_effect=run_my_prs_query,
            ),
        ):
            prs = github.get_my_prs(["METR"])

        self.assertEqual([pr.url for pr in prs], [handed_off_pr.url])


class GetReviewRequestsTests(unittest.TestCase):
    def test_fetches_line_counts(self) -> None:
        for additions, deletions in [(128, 42), (1050, 0), (0, 73), (0, 0)]:
            with self.subTest(additions=additions, deletions=deletions):
                self._assert_line_counts(
                    {"additions": additions, "deletions": deletions},
                    additions,
                    deletions,
                )

    def test_defaults_missing_line_counts_to_zero(self) -> None:
        for counts in [{}, {"additions": None, "deletions": None}]:
            with self.subTest(counts=counts):
                self._assert_line_counts(counts, 0, 0)

    def _assert_line_counts(
        self, counts: Mapping[str, int | None], additions: int, deletions: int
    ) -> None:
        node = {
            "number": 1,
            "title": "Update tests",
            "url": "https://github.com/acme/repo/pull/1",
            "repository": {"nameWithOwner": "acme/repo"},
            "author": {"login": "alice"},
            "createdAt": "2026-01-01T00:00:00Z",
            "reviewRequests": {"nodes": [{"requestedReviewer": {"login": "reviewer"}}]},
            **counts,
        }
        with (
            patch.object(github, "get_my_username", return_value="reviewer"),
            patch.object(
                github,
                "_run_gh_graphql",
                return_value={"data": {"search": {"nodes": [node]}}},
            ) as run_query,
        ):
            prs = github.get_review_requests(["acme"])

        run_query.assert_called_once()
        query = cast(str, run_query.call_args.args[0])
        self.assertIn("\n        additions\n", query)
        self.assertIn("\n        deletions\n", query)
        self.assertEqual(len(prs), 1)
        self.assertEqual(prs[0].additions, additions)
        self.assertEqual(prs[0].deletions, deletions)


class GetNotificationsTests(unittest.TestCase):
    def test_includes_review_requested_and_comment_notifications(self) -> None:
        for reason in ("review_requested", "comment"):
            with self.subTest(reason=reason):
                with patch.object(
                    github, "_run_gh_api", return_value=[_make_notification(reason)]
                ) as run_api:
                    notifications = github.get_notifications(["acme"])

                run_api.assert_called_once_with("notifications?all=false&per_page=50")
                self.assertEqual(
                    notifications,
                    [
                        github.Notification(
                            id="123",
                            reason=reason,
                            title="Update tests",
                            repository="acme/repo",
                            url="https://github.com/acme/repo/pull/1",
                            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                            pr_number=1,
                        )
                    ],
                )

    def test_still_excludes_authored_non_pr_and_other_org_notifications(self) -> None:
        for reason, subject_type, repository in (
            ("author", "PullRequest", "acme/repo"),
            ("review_requested", "Issue", "acme/repo"),
            ("review_requested", "PullRequest", "outside/repo"),
        ):
            with self.subTest(
                reason=reason, subject_type=subject_type, repository=repository
            ):
                with patch.object(
                    github,
                    "_run_gh_api",
                    return_value=[_make_notification(reason, subject_type, repository)],
                ):
                    self.assertEqual(github.get_notifications(["acme"]), [])


if __name__ == "__main__":
    _ = unittest.main()
