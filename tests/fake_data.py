from datetime import date, datetime, timedelta, timezone

from status_dashboard.clients import github, linear


def fake_prs() -> list[github.PullRequest]:
    now = datetime.now(tz=timezone.utc)
    return [
        github.PullRequest(
            number=142,
            title="Add retry logic for flaky API calls",
            repository="acme/backend",
            url="https://github.com/acme/backend/pull/142",
            created_at=now - timedelta(days=1),
            is_draft=False,
            is_approved=True,
            ci_status="SUCCESS",
            reviewers=["alice"],
            additions=212,
            deletions=35,
        ),
        github.PullRequest(
            number=87,
            title="WIP: Migrate user auth to OAuth2 provider",
            repository="acme/frontend",
            url="https://github.com/acme/frontend/pull/87",
            created_at=now - timedelta(days=3),
            is_draft=True,
            ci_status="PENDING",
            reviewers=[],
            additions=940,
            deletions=610,
        ),
        github.PullRequest(
            number=310,
            title="Fix race condition in task scheduler",
            repository="acme/worker",
            url="https://github.com/acme/worker/pull/310",
            created_at=now - timedelta(hours=6),
            needs_response=True,
            has_review=True,
            ci_status="FAILURE",
            unresolved_comment_count=3,
            reviewers=["bob", "carol"],
            additions=18,
            deletions=4,
        ),
        github.PullRequest(
            number=55,
            title="Bump dependencies and update lockfile",
            repository="acme/infra",
            url="https://github.com/acme/infra/pull/55",
            created_at=now - timedelta(days=5),
            has_review=True,
            ci_status="SUCCESS",
            mergeable="CONFLICTING",
            reviewers=["dave"],
            additions=0,
            deletions=0,
        ),
        github.PullRequest(
            number=61,
            title="Add staging deploy preview",
            repository="acme/infra",
            url="https://github.com/acme/infra/pull/61",
            created_at=now - timedelta(days=6),
            ci_status=github.AWAITING_APPROVAL,
            reviewers=[],
            additions=48,
            deletions=12,
            viewer_did_author=True,
            gated_checks=[
                github.GatedCheck(
                    name="Deploy preview (staging)",
                    environment="staging-preview",
                    environment_id="EN_staging",
                    workflow_run_id="WFR_61",
                ),
                github.GatedCheck(
                    name="Deploy preview (production)",
                    environment="production-preview",
                    environment_id="EN_production",
                    workflow_run_id="WFR_61",
                ),
            ],
        ),
    ]


def fake_review_requests() -> list[github.ReviewRequest]:
    now = datetime.now(tz=timezone.utc)
    return [
        github.ReviewRequest(
            number=201,
            title="Refactor database connection pooling",
            repository="acme/backend",
            url="https://github.com/acme/backend/pull/201",
            author="alice",
            created_at=now - timedelta(hours=3),
            requested_teams=[],
            has_other_review=False,
            additions=128,
            deletions=42,
        ),
        github.ReviewRequest(
            number=44,
            title="Add end-to-end tests for checkout flow",
            repository="acme/frontend",
            url="https://github.com/acme/frontend/pull/44",
            author="bob",
            created_at=now - timedelta(days=1, hours=5),
            requested_teams=["platform"],
            has_other_review=True,
            additions=1050,
            deletions=0,
        ),
        github.ReviewRequest(
            number=98,
            title="Implement rate limiting middleware",
            repository="acme/api-gateway",
            url="https://github.com/acme/api-gateway/pull/98",
            author="carol",
            created_at=now - timedelta(hours=12),
            requested_teams=[],
            has_other_review=False,
            additions=0,
            deletions=73,
        ),
    ]


def fake_notifications() -> list[github.Notification]:
    now = datetime.now(tz=timezone.utc)
    return [
        github.Notification(
            id="n1",
            reason="review_requested",
            title="Add caching layer for search results",
            repository="acme/backend",
            url="https://github.com/acme/backend/pull/188",
            updated_at=now - timedelta(minutes=30),
            pr_number=188,
        ),
        github.Notification(
            id="n2",
            reason="mention",
            title="Deploy pipeline stuck on staging",
            repository="acme/infra",
            url="https://github.com/acme/infra/issues/42",
            updated_at=now - timedelta(hours=2),
            issue_number=42,
        ),
        github.Notification(
            id="n3",
            reason="ci_activity",
            title="Upgrade Node.js to v20 LTS",
            repository="acme/frontend",
            url="https://github.com/acme/frontend/pull/91",
            updated_at=now - timedelta(days=1),
            pr_number=91,
        ),
    ]


def fake_task_rows() -> list[list[str]]:
    """Stored task rows, as `tasks.load` returns them."""
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    tasks = [
        ("t1", "Review Q1 planning document", "", f"{today}T09:00:00"),
        ("t2", "Write unit tests for auth module", "", today),
        (
            "t3",
            "Respond to Sarah's design feedback",
            "She left comments on the Figma file",
            today,
        ),
        (
            "t4",
            "Update API documentation for v2 endpoints",
            "",
            f"{yesterday}T14:00:00",
        ),
        ("t5", "Fix broken CI pipeline for staging", "", today),
        ("t6", "Prepare demo for Friday standup", "", f"{today}T16:30:00"),
    ]
    return [
        [task_id, content, "", description, due, "", str(order), "FALSE", ""]
        for order, (task_id, content, description, due) in enumerate(tasks, 1)
    ]


def fake_linear_issues() -> list[linear.Issue]:
    return [
        linear.Issue(
            id="iss1",
            identifier="ENG-301",
            title="Implement webhook retry with exponential backoff",
            state="In Progress",
            url="https://linear.app/acme/issue/ENG-301",
            team_id="team1",
            assignee_initials="TB",
            sort_order=1.0,
        ),
        linear.Issue(
            id="iss2",
            identifier="ENG-298",
            title="Add Prometheus metrics to API gateway",
            state="In Review",
            url="https://linear.app/acme/issue/ENG-298",
            team_id="team1",
            assignee_initials="TB",
            sort_order=0.5,
        ),
        linear.Issue(
            id="iss3",
            identifier="ENG-315",
            title="Research options for real-time notifications",
            state="Todo",
            url="https://linear.app/acme/issue/ENG-315",
            team_id="team1",
            assignee_initials="TB",
            sort_order=2.0,
        ),
        linear.Issue(
            id="iss4",
            identifier="ENG-320",
            title="Fix memory leak in background job processor",
            state="Todo",
            url="https://linear.app/acme/issue/ENG-320",
            team_id="team1",
            assignee_initials="TB",
            sort_order=3.0,
        ),
        linear.Issue(
            id="iss5",
            identifier="ENG-280",
            title="Upgrade PostgreSQL from 14 to 16",
            state="Backlog",
            url="https://linear.app/acme/issue/ENG-280",
            team_id="team1",
            assignee_initials="TB",
            sort_order=5.0,
        ),
    ]
