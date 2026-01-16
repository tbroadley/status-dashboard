import json
import logging
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

SUBPROCESS_TIMEOUT = 30  # seconds

BOT_REVIEWERS = {
    "copilot-pull-request-reviewer",
    "copilot",
    "github-actions",
    "chatgpt-codex-connector",
}


@dataclass
class PullRequest:
    number: int
    title: str
    repository: str
    url: str
    is_draft: bool = False
    is_approved: bool = False
    needs_response: bool = False
    has_review: bool = False
    ci_status: str | None = None


@dataclass
class ReviewRequest:
    number: int
    title: str
    repository: str
    url: str
    author: str
    created_at: datetime


@dataclass
class Notification:
    id: str
    reason: str
    title: str
    repository: str
    url: str
    updated_at: datetime
    pr_number: int | None = None


def _run_gh_graphql(query: str) -> dict | None:
    """Run a gh api graphql command and return parsed JSON output."""
    try:
        result = subprocess.run(
            ["gh", "api", "graphql", "-f", f"query={query}"],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
        )
        if result.returncode != 0:
            logger.warning("gh graphql failed: %s", result.stderr.strip())
            return None
        return json.loads(result.stdout) if result.stdout.strip() else None
    except subprocess.TimeoutExpired:
        logger.error("gh command timed out after %d seconds", SUBPROCESS_TIMEOUT)
        return None
    except json.JSONDecodeError as e:
        logger.error("Failed to parse gh output: %s", e)
        return None
    except FileNotFoundError:
        logger.error("gh CLI not found. Install it from https://cli.github.com/")
        return None


def _parse_datetime(dt_str: str) -> datetime:
    """Parse ISO datetime string to datetime object."""
    return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))


def _relative_time(dt: datetime) -> str:
    """Convert datetime to relative time string like '2h ago'."""
    now = datetime.now(timezone.utc)
    diff = now - dt
    seconds = int(diff.total_seconds())

    if seconds < 60:
        return "now"
    elif seconds < 3600:
        mins = seconds // 60
        return f"{mins}m"
    elif seconds < 86400:
        hours = seconds // 3600
        return f"{hours}h"
    else:
        days = seconds // 86400
        return f"{days}d"


MY_PRS_QUERY = """
query {{
  search(query: "author:@me state:open org:{org} type:pr", type: ISSUE, first: 50) {{
    nodes {{
      ... on PullRequest {{
        number
        title
        url
        isDraft
        repository {{
          nameWithOwner
        }}
        reviewDecision
        latestReviews(first: 20) {{
          nodes {{
            author {{
              login
            }}
            state
          }}
        }}
        commits(last: 1) {{
          nodes {{
            commit {{
              statusCheckRollup {{
                state
              }}
            }}
          }}
        }}
      }}
    }}
  }}
}}
"""


REVIEW_REQUESTS_QUERY = """
query {{
  search(query: "review-requested:@me state:open org:{org} type:pr", type: ISSUE, first: 50) {{
    nodes {{
      ... on PullRequest {{
        number
        title
        url
        repository {{
          nameWithOwner
        }}
        author {{
          login
        }}
        createdAt
      }}
    }}
  }}
}}
"""


def get_my_prs(org: str | None = None) -> list[PullRequest]:
    """Get open PRs created by the current user with review status."""
    owner = org or os.environ.get("GITHUB_ORG", "METR")
    query = MY_PRS_QUERY.format(org=owner)
    result = _run_gh_graphql(query)

    if not result:
        return []

    prs = []
    nodes = result.get("data", {}).get("search", {}).get("nodes", [])

    for pr in nodes:
        if not pr:  # Can be null for non-PR results
            continue

        reviews = pr.get("latestReviews", {}).get("nodes", [])

        # Filter out bot reviews
        human_reviews = [
            r
            for r in reviews
            if r.get("author", {}).get("login", "").lower() not in BOT_REVIEWERS
        ]

        # Check review states
        review_decision = pr.get("reviewDecision")
        is_approved = review_decision == "APPROVED"
        has_changes_requested = any(
            r.get("state") == "CHANGES_REQUESTED" for r in human_reviews
        )
        has_comments = any(r.get("state") == "COMMENTED" for r in human_reviews)

        commits = pr.get("commits", {}).get("nodes", [])
        ci_state = None
        if commits:
            rollup = commits[0].get("commit", {}).get("statusCheckRollup")
            if rollup:
                ci_state = rollup.get("state")

        prs.append(
            PullRequest(
                number=pr["number"],
                title=pr["title"],
                repository=pr.get("repository", {}).get("nameWithOwner", "unknown"),
                url=pr["url"],
                is_draft=pr.get("isDraft", False),
                is_approved=is_approved,
                needs_response=has_changes_requested or has_comments,
                has_review=len(human_reviews) > 0,
                ci_status=ci_state,
            )
        )

    return prs


def remove_self_as_reviewer(repo: str, pr_number: int) -> bool:
    """Remove the current user as a reviewer from a PR.

    Args:
        repo: Repository in 'owner/name' format
        pr_number: PR number

    Returns:
        True if successful, False otherwise
    """
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "edit",
                str(pr_number),
                "--repo",
                repo,
                "--remove-reviewer",
                "@me",
            ],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
        )
        if result.returncode != 0:
            logger.warning("Failed to remove reviewer: %s", result.stderr.strip())
            return False
        return True
    except subprocess.TimeoutExpired:
        logger.error("gh command timed out after %d seconds", SUBPROCESS_TIMEOUT)
        return False
    except FileNotFoundError:
        logger.error("gh CLI not found. Install it from https://cli.github.com/")
        return False


def squash_merge_pr(repo: str, pr_number: int) -> bool:
    """Squash merge a PR.

    Args:
        repo: Repository in 'owner/name' format
        pr_number: PR number

    Returns:
        True if successful, False otherwise
    """
    try:
        result = subprocess.run(
            ["gh", "pr", "merge", str(pr_number), "--repo", repo, "--squash"],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
        )
        if result.returncode != 0:
            logger.warning("Failed to merge PR: %s", result.stderr.strip())
            return False
        return True
    except subprocess.TimeoutExpired:
        logger.error("gh command timed out after %d seconds", SUBPROCESS_TIMEOUT)
        return False
    except FileNotFoundError:
        logger.error("gh CLI not found. Install it from https://cli.github.com/")
        return False


def get_review_requests(org: str | None = None) -> list[ReviewRequest]:
    """Get open PRs where review is requested from current user."""
    owner = org or os.environ.get("GITHUB_ORG", "METR")
    query = REVIEW_REQUESTS_QUERY.format(org=owner)
    result = _run_gh_graphql(query)

    if not result:
        return []

    prs = []
    nodes = result.get("data", {}).get("search", {}).get("nodes", [])

    for pr in nodes:
        if not pr:
            continue

        prs.append(
            ReviewRequest(
                number=pr["number"],
                title=pr["title"],
                repository=pr.get("repository", {}).get("nameWithOwner", "unknown"),
                url=pr["url"],
                author=pr.get("author", {}).get("login", "unknown"),
                created_at=_parse_datetime(pr["createdAt"]),
            )
        )

    return prs


def _run_gh_api(endpoint: str, method: str = "GET") -> Any:
    """Run a gh api command and return parsed JSON output."""
    try:
        cmd = ["gh", "api", endpoint]
        if method != "GET":
            cmd.extend(["-X", method])
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
        )
        if result.returncode != 0:
            logger.warning("gh api failed: %s", result.stderr.strip())
            return None
        return json.loads(result.stdout) if result.stdout.strip() else None
    except subprocess.TimeoutExpired:
        logger.error("gh command timed out after %d seconds", SUBPROCESS_TIMEOUT)
        return None
    except json.JSONDecodeError as e:
        logger.error("Failed to parse gh output: %s", e)
        return None
    except FileNotFoundError:
        logger.error("gh CLI not found. Install it from https://cli.github.com/")
        return None


def get_notifications(org: str | None = None) -> list[Notification]:
    """Get unread GitHub notifications for pull requests.

    Filters to only PR-related notifications and optionally by organization.
    """
    owner = org or os.environ.get("GITHUB_ORG", "METR")
    result = _run_gh_api("notifications?all=false&per_page=50")

    if not result or not isinstance(result, list):
        return []

    notifications = []
    for item in result:
        if item.get("reason") == "review_requested":
            continue

        subject = item.get("subject", {})
        if subject.get("type") != "PullRequest":
            continue

        repo_full_name = item.get("repository", {}).get("full_name", "")
        if owner and not repo_full_name.startswith(f"{owner}/"):
            continue

        subject_url = subject.get("url", "")
        pr_number = None
        html_url = ""
        if subject_url:
            parts = subject_url.split("/")
            if len(parts) >= 2 and parts[-2] == "pulls":
                pr_number = int(parts[-1])
                html_url = f"https://github.com/{repo_full_name}/pull/{pr_number}"

        notifications.append(
            Notification(
                id=item["id"],
                reason=item.get("reason", "unknown"),
                title=subject.get("title", ""),
                repository=repo_full_name,
                url=html_url,
                updated_at=_parse_datetime(item["updated_at"]),
                pr_number=pr_number,
            )
        )

    return notifications


def mark_notification_read(thread_id: str) -> bool:
    """Mark a notification thread as read.

    Args:
        thread_id: The notification thread ID

    Returns:
        True if successful, False otherwise
    """
    try:
        result = subprocess.run(
            ["gh", "api", f"notifications/threads/{thread_id}", "-X", "PATCH"],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
        )
        if result.returncode != 0:
            logger.warning(
                "Failed to mark notification read: %s", result.stderr.strip()
            )
            return False
        return True
    except subprocess.TimeoutExpired:
        logger.error("gh command timed out after %d seconds", SUBPROCESS_TIMEOUT)
        return False
    except FileNotFoundError:
        logger.error("gh CLI not found. Install it from https://cli.github.com/")
        return False
