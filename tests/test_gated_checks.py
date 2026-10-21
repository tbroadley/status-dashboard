import os
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import cast
from unittest.mock import MagicMock, patch

_ = os.environ.setdefault("TASKS_S3_URI", "s3://example-bucket/tasks.json")
_ = os.environ.setdefault("LINEAR_API_KEY", "fake-key")
_ = os.environ.setdefault("LINEAR_PROJECT", "Fake Project")

from status_dashboard import app as app_module  # noqa: E402
from status_dashboard.app import MyPRsDataTable, StatusDashboard  # noqa: E402
from status_dashboard.clients import github  # noqa: E402
from status_dashboard.widgets.create_modals import ConfirmationModal  # noqa: E402

_CHECK = github.GatedCheck(
    name="Preview (prd)",
    environment="prd-preview",
    environment_id="EN_prd",
    workflow_run_id="WFR_1",
)


def _context(
    name: str,
    status: str = "COMPLETED",
    can_approve: bool | None = None,
    environment_id: str = "EN_prd",
    run_id: str = "WFR_1",
) -> dict[str, object]:
    context: dict[str, object] = {
        "__typename": "CheckRun",
        "name": name,
        "status": status,
        "checkSuite": {"workflowRun": {"id": run_id}},
        "pendingDeploymentRequest": None,
    }
    if can_approve is not None:
        context["pendingDeploymentRequest"] = {
            "currentUserCanApprove": can_approve,
            "environment": {"id": environment_id, "name": f"{name}-env"},
        }
    return context


def _contexts_response(
    *contexts_per_pr: list[dict[str, object]], truncated: bool = False
) -> dict[str, object]:
    return {
        "data": {
            f"pr{i}": {
                "pullRequest": {
                    "commits": {
                        "nodes": [
                            {
                                "commit": {
                                    "statusCheckRollup": {
                                        "contexts": {
                                            "pageInfo": {"hasNextPage": truncated},
                                            "nodes": contexts,
                                        }
                                    }
                                }
                            }
                        ]
                    }
                }
            }
            for i, contexts in enumerate(contexts_per_pr)
        }
    }


def _pr(
    number: int = 1,
    ci_status: str = "PENDING",
    viewer_did_author: bool = False,
    gated_checks: list[github.GatedCheck] | None = None,
) -> github.PullRequest:
    return github.PullRequest(
        number=number,
        title=f"PR {number}",
        repository="acme/repo",
        url=f"https://github.com/acme/repo/pull/{number}",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ci_status=ci_status,
        viewer_did_author=viewer_did_author,
        gated_checks=list(gated_checks or []),
    )


class AddGatedChecksTests(unittest.TestCase):
    def test_classifies_pending_prs(self) -> None:
        cases: list[tuple[str, list[dict[str, object]], bool, str, list[str]]] = [
            (
                "only gated checks unfinished",
                [_context("lint"), _context("deploy", "WAITING", True)],
                False,
                github.AWAITING_APPROVAL,
                ["deploy"],
            ),
            (
                "another check still running",
                [
                    _context("lint", "IN_PROGRESS"),
                    _context("deploy", "WAITING", True),
                ],
                False,
                "PENDING",
                ["deploy"],
            ),
            (
                "pending status context",
                [
                    {"__typename": "StatusContext", "state": "PENDING"},
                    _context("deploy", "WAITING", True),
                ],
                False,
                "PENDING",
                ["deploy"],
            ),
            (
                "waiting on someone else's approval",
                [_context("deploy", "WAITING", False)],
                False,
                "PENDING",
                [],
            ),
            (
                "checks truncated",
                [_context("deploy", "WAITING", True)],
                True,
                "PENDING",
                ["deploy"],
            ),
        ]
        for label, contexts, truncated, status, names in cases:
            with self.subTest(label):
                pr = _pr()
                with patch.object(
                    github,
                    "_run_gh_graphql",
                    return_value=_contexts_response(contexts, truncated=truncated),
                ):
                    github._add_gated_checks([pr])  # pyright: ignore[reportPrivateUsage]
                self.assertEqual(pr.ci_status, status)
                self.assertEqual([c.name for c in pr.gated_checks], names)

    def test_leaves_prs_unchanged_on_failure(self) -> None:
        pr = _pr()
        with patch.object(github, "_run_gh_graphql", return_value=None):
            github._add_gated_checks([pr])  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(pr.ci_status, "PENDING")
        self.assertEqual(pr.gated_checks, [])

    def test_get_my_prs_only_inspects_pending_prs(self) -> None:
        pending = _pr(1)
        passing = _pr(2, ci_status="SUCCESS")
        with (
            patch.object(github, "_run_my_prs_query", return_value=[pending, passing]),
            patch.object(github, "_add_gated_checks") as add_gated,
        ):
            _ = github.get_my_prs(["acme"])
        add_gated.assert_called_once_with([pending])


class ApproveGatedChecksTests(unittest.TestCase):
    def test_approves_each_workflow_run_once(self) -> None:
        checks = [
            _CHECK,
            github.GatedCheck("Preview (tst)", "tst-preview", "EN_tst", "WFR_1"),
            github.GatedCheck("Deploy", "prd-preview", "EN_prd", "WFR_2"),
        ]
        response = {
            "data": {
                "run0": {"deployments": [{"id": "D1"}]},
                "run1": {"deployments": [{"id": "D1"}]},
            }
        }
        with patch.object(
            github, "_run_gh_graphql", return_value=response
        ) as run_graphql:
            self.assertTrue(github.approve_gated_checks(checks))
        mutation = cast(str, run_graphql.call_args.args[0])
        self.assertIn(
            'run0: approveDeployments(input: {workflowRunId: "WFR_1", '
            + 'environmentIds: ["EN_prd", "EN_tst"]})',
            mutation,
        )
        self.assertIn(
            'run1: approveDeployments(input: {workflowRunId: "WFR_2", '
            + 'environmentIds: ["EN_prd"]})',
            mutation,
        )

    def test_fails_when_any_approval_fails(self) -> None:
        for response in [
            None,
            {"data": {"run0": None}},
            {
                "data": {"run0": {"deployments": [{"id": "D1"}]}},
                "errors": [{"message": "no"}],
            },
        ]:
            with self.subTest(response=response):
                with patch.object(github, "_run_gh_graphql", return_value=response):
                    self.assertFalse(github.approve_gated_checks([_CHECK]))

    def test_fails_without_checks(self) -> None:
        with patch.object(github, "_run_gh_graphql") as run_graphql:
            self.assertFalse(github.approve_gated_checks([]))
        run_graphql.assert_not_called()


@contextmanager
def _patched(approve: MagicMock) -> Iterator[None]:
    with (
        patch.object(StatusDashboard, "refresh_all"),
        patch.object(StatusDashboard, "_check_for_updates"),
        patch.object(app_module, "TODOIST_DUE_NOTIFICATIONS", False),
        patch.object(app_module, "HIDDEN_PRS", set[tuple[str, int]]()),
        patch.object(github, "approve_gated_checks", approve),
    ):
        yield


class RunGatedChecksActionTests(unittest.IsolatedAsyncioTestCase):
    async def _press_run(
        self, pr: github.PullRequest, approve: MagicMock, *keys: str
    ) -> tuple[bool, list[str]]:
        notifications: list[str] = []
        modal_opened = False
        with _patched(approve):
            app = StatusDashboard()
            async with app.run_test(size=(120, 40)) as pilot:
                original_notify = app.notify

                def record(message: str, **kwargs: object) -> None:
                    notifications.append(message)
                    original_notify(message, **kwargs)  # pyright: ignore[reportArgumentType]

                app.notify = record
                app._my_prs = [pr]  # pyright: ignore[reportPrivateUsage]
                app._render_my_prs_table()  # pyright: ignore[reportPrivateUsage]
                table = app.query_one("#my-prs-table", MyPRsDataTable)
                _ = table.focus()
                await pilot.pause()
                self.assertEqual(table.get_row_at(0)[5], "pass*")
                await pilot.press("a")
                await pilot.pause()
                modal_opened = isinstance(app.screen, ConfirmationModal)
                for key in keys:
                    if key == "?modal":
                        modal = app.screen
                        self.assertIsInstance(modal, ConfirmationModal)
                        message = str(
                            cast(ConfirmationModal, modal)._message  # pyright: ignore[reportPrivateUsage]
                        )
                        self.assertIn("• Preview (prd)", message)
                        continue
                    await pilot.press(key)
                    await pilot.pause()
                await app.workers.wait_for_complete()  # pyright: ignore[reportUnknownMemberType]
                await pilot.pause()
        return modal_opened, notifications

    async def test_confirm_runs_checks_optimistically(self) -> None:
        approve = MagicMock(return_value=True)
        pr = _pr(
            ci_status=github.AWAITING_APPROVAL,
            viewer_did_author=True,
            gated_checks=[_CHECK],
        )
        modal_opened, notifications = await self._press_run(pr, approve, "?modal", "y")
        self.assertTrue(modal_opened)
        approve.assert_called_once_with([_CHECK])
        self.assertEqual(pr.ci_status, "PENDING")
        self.assertEqual(pr.gated_checks, [])
        self.assertIn("Started 1 checks on PR #1", notifications)

    async def test_failure_rolls_back(self) -> None:
        approve = MagicMock(return_value=False)
        pr = _pr(
            ci_status=github.AWAITING_APPROVAL,
            viewer_did_author=True,
            gated_checks=[_CHECK],
        )
        _, notifications = await self._press_run(pr, approve, "y")
        self.assertEqual(pr.ci_status, github.AWAITING_APPROVAL)
        self.assertEqual(pr.gated_checks, [_CHECK])
        self.assertIn("Failed to run checks", notifications)

    async def test_cancel_does_nothing(self) -> None:
        approve = MagicMock(return_value=True)
        pr = _pr(
            ci_status=github.AWAITING_APPROVAL,
            viewer_did_author=True,
            gated_checks=[_CHECK],
        )
        _ = await self._press_run(pr, approve, "n")
        approve.assert_not_called()
        self.assertEqual(pr.ci_status, github.AWAITING_APPROVAL)

    async def test_refuses_prs_i_did_not_author(self) -> None:
        approve = MagicMock(return_value=True)
        pr = _pr(ci_status=github.AWAITING_APPROVAL, gated_checks=[_CHECK])
        modal_opened, notifications = await self._press_run(pr, approve)
        approve.assert_not_called()
        self.assertFalse(modal_opened)
        self.assertIn("Can only run checks on PRs you authored", notifications)


if __name__ == "__main__":
    _ = unittest.main()
