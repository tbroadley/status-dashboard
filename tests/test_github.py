from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import call, patch

from status_dashboard.clients import github


def _make_pr(number: int, url: str, created_at: datetime) -> github.PullRequest:
    return github.PullRequest(
        number=number,
        title=f"PR {number}",
        repository="acme/repo",
        url=url,
        created_at=created_at,
    )


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
            "author:@me state:open org:METR type:pr": [authored_pr],
            "assignee:@me state:open org:METR type:pr": [assigned_pr, authored_pr],
            "author:@me state:open repo:outside/repo type:pr": [],
            "assignee:@me state:open repo:outside/repo type:pr": [extra_repo_pr],
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
        self.assertEqual(
            run_query.call_args_list,
            [
                call("author:@me state:open org:METR type:pr"),
                call("assignee:@me state:open org:METR type:pr"),
                call("author:@me state:open repo:outside/repo type:pr"),
                call("assignee:@me state:open repo:outside/repo type:pr"),
            ],
        )


if __name__ == "__main__":
    _ = unittest.main()
