from datetime import date, datetime, timedelta, timezone

from status_dashboard.clients import github, linear, todoist
from status_dashboard.db import goals as goals_db


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
        ),
        github.PullRequest(
            number=55,
            title="Bump dependencies and update lockfile",
            repository="acme/infra",
            url="https://github.com/acme/infra/pull/55",
            created_at=now - timedelta(days=5),
            has_review=True,
            ci_status="SUCCESS",
            reviewers=["dave"],
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
            pr_number=None,
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


def fake_todoist_tasks() -> list[todoist.Task]:
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    return [
        todoist.Task(
            id="t1",
            content="Review Q1 planning document",
            is_completed=False,
            url="https://todoist.com/app/task/t1",
            day_order=1,
            due_date=today,
            due_time="09:00",
            comment_count=2,
        ),
        todoist.Task(
            id="t2",
            content="Write unit tests for auth module",
            is_completed=False,
            url="https://todoist.com/app/task/t2",
            day_order=2,
            due_date=today,
        ),
        todoist.Task(
            id="t3",
            content="Respond to Sarah's design feedback",
            is_completed=False,
            url="https://todoist.com/app/task/t3",
            day_order=3,
            due_date=today,
            comment_count=5,
            description="She left comments on the Figma file",
        ),
        todoist.Task(
            id="t4",
            content="Update API documentation for v2 endpoints",
            is_completed=False,
            url="https://todoist.com/app/task/t4",
            day_order=4,
            due_date=yesterday,
            due_time="14:00",
        ),
        todoist.Task(
            id="t5",
            content="Fix broken CI pipeline for staging",
            is_completed=False,
            url="https://todoist.com/app/task/t5",
            day_order=5,
            due_date=today,
        ),
        todoist.Task(
            id="t6",
            content="Prepare demo for Friday standup",
            is_completed=False,
            url="https://todoist.com/app/task/t6",
            day_order=6,
            due_date=today,
            due_time="16:30",
            comment_count=1,
        ),
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
            assignee_initials=None,
            sort_order=2.0,
        ),
        linear.Issue(
            id="iss4",
            identifier="ENG-320",
            title="Fix memory leak in background job processor",
            state="Todo",
            url="https://linear.app/acme/issue/ENG-320",
            team_id="team1",
            assignee_initials="AK",
            sort_order=3.0,
        ),
        linear.Issue(
            id="iss5",
            identifier="ENG-280",
            title="Upgrade PostgreSQL from 14 to 16",
            state="Backlog",
            url="https://linear.app/acme/issue/ENG-280",
            team_id="team1",
            sort_order=5.0,
        ),
    ]


def fake_goals() -> list[goals_db.Goal]:
    now = datetime.now()
    week_start = goals_db.get_week_start(date.today())
    return [
        goals_db.Goal(
            id="g1",
            content="Ship webhook retry feature (ENG-301)",
            week_start=week_start,
            is_completed=True,
            is_abandoned=False,
            completed_at=now - timedelta(days=1),
            abandoned_at=None,
            created_at=now - timedelta(days=5),
            sort_order=0,
            predicted_time=8.0,
            actual_time=6.5,
        ),
        goals_db.Goal(
            id="g2",
            content="Review and merge all open PRs",
            week_start=week_start,
            is_completed=False,
            is_abandoned=False,
            completed_at=None,
            abandoned_at=None,
            created_at=now - timedelta(days=5),
            sort_order=1,
            predicted_time=4.0,
        ),
        goals_db.Goal(
            id="g3",
            content="Write design doc for notifications system",
            week_start=week_start,
            is_completed=False,
            is_abandoned=False,
            completed_at=None,
            abandoned_at=None,
            created_at=now - timedelta(days=3),
            sort_order=2,
            predicted_time=6.0,
        ),
        goals_db.Goal(
            id="g4",
            content="Investigate flaky test suite",
            week_start=week_start,
            is_completed=False,
            is_abandoned=True,
            completed_at=None,
            abandoned_at=now - timedelta(days=2),
            created_at=now - timedelta(days=5),
            sort_order=3,
        ),
    ]
