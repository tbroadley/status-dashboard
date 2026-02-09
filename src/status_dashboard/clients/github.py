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
    "cursor[bot]",
}

_JsonDict = dict[str, Any]  # pyright: ignore[reportExplicitAny]
_JsonList = list[_JsonDict]


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
    unresolved_comment_count: int = 0


@dataclass
class ReviewRequest:
    number: int
    title: str
    repository: str
    url: str
    author: str
    created_at: datetime
    requested_teams: list[str]
    has_other_review: bool  # True if someone else has already submitted a review


@dataclass
class Notification:
    id: str
    reason: str
    title: str
    repository: str
    url: str
    updated_at: datetime
    pr_number: int | None = None


def _run_gh_graphql(query: str) -> _JsonDict | None:
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


def _relative_time(dt: datetime) -> str:  # pyright: ignore[reportUnusedFunction]
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
        reviewThreads(first: 100) {{
          nodes {{
            isResolved
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
        reviewRequests(first: 20) {{
          nodes {{
            requestedReviewer {{
              ... on Team {{
                slug
              }}
              ... on User {{
                login
              }}
            }}
          }}
        }}
        latestReviews(first: 20) {{
          nodes {{
            author {{
              login
            }}
            state
          }}
        }}
      }}
    }}
  }}
}}
"""


def _get_orgs() -> list[str]:
    """Get list of GitHub organizations from environment."""
    orgs_str = os.environ.get("GITHUB_ORGS", "")
    if orgs_str:
        return [org.strip() for org in orgs_str.split(",") if org.strip()]
    # Fall back to single GITHUB_ORG for backward compatibility
    return [os.environ.get("GITHUB_ORG", "METR")]


def _get_str(d: _JsonDict, key: str, default: str = "") -> str:
    """Get a string value from a JSON dict."""
    val = d.get(key, default)  # pyright: ignore[reportAny]
    return str(val) if val is not None else default  # pyright: ignore[reportAny]


def _get_int(d: _JsonDict, key: str, default: int = 0) -> int:
    """Get an int value from a JSON dict."""
    val = d.get(key, default)  # pyright: ignore[reportAny]
    return int(val) if val is not None else default  # pyright: ignore[reportAny]


def _get_bool(d: _JsonDict, key: str, default: bool = False) -> bool:
    """Get a bool value from a JSON dict."""
    val = d.get(key, default)  # pyright: ignore[reportAny]
    return bool(val)  # pyright: ignore[reportAny]


def _get_dict(d: _JsonDict, key: str) -> _JsonDict:
    """Get a nested dict from a JSON dict, returning empty dict if missing."""
    val = d.get(key)
    if isinstance(val, dict):
        return val  # pyright: ignore[reportUnknownVariableType]
    return {}


def _get_list(d: _JsonDict, key: str) -> _JsonList:
    """Get a list of dicts from a JSON dict, returning empty list if missing."""
    val = d.get(key)
    if isinstance(val, list):
        return val  # pyright: ignore[reportUnknownVariableType]
    return []


def get_my_prs(orgs: list[str] | None = None) -> list[PullRequest]:
    """Get open PRs created by the current user with review status."""
    owners = orgs or _get_orgs()
    all_prs: list[PullRequest] = []

    for owner in owners:
        query = MY_PRS_QUERY.format(org=owner)
        result = _run_gh_graphql(query)

        if not result:
            continue

        data = _get_dict(result, "data")
        search = _get_dict(data, "search")
        nodes = _get_list(search, "nodes")

        for pr in nodes:
            if not pr:  # Can be null for non-PR results
                continue

            latest_reviews = _get_dict(pr, "latestReviews")
            reviews = _get_list(latest_reviews, "nodes")

            # Filter out bot reviews
            human_reviews = [
                r
                for r in reviews
                if _get_str(_get_dict(r, "author"), "login").lower()
                not in BOT_REVIEWERS
            ]

            # Check review states
            review_decision = _get_str(pr, "reviewDecision")
            is_approved = review_decision == "APPROVED"
            has_changes_requested = any(
                _get_str(r, "state") == "CHANGES_REQUESTED" for r in human_reviews
            )
            has_comments = any(
                _get_str(r, "state") == "COMMENTED" for r in human_reviews
            )

            commits_wrapper = _get_dict(pr, "commits")
            commits = _get_list(commits_wrapper, "nodes")
            ci_state: str | None = None
            if commits:
                commit_node = _get_dict(commits[0], "commit")
                rollup = _get_dict(commit_node, "statusCheckRollup")
                if rollup:
                    ci_state = _get_str(rollup, "state") or None

            threads_wrapper = _get_dict(pr, "reviewThreads")
            review_threads = _get_list(threads_wrapper, "nodes")
            unresolved_count = sum(
                1
                for thread in review_threads
                if not _get_bool(thread, "isResolved", default=True)
            )

            repo_info = _get_dict(pr, "repository")
            all_prs.append(
                PullRequest(
                    number=_get_int(pr, "number"),
                    title=_get_str(pr, "title"),
                    repository=_get_str(repo_info, "nameWithOwner", "unknown"),
                    url=_get_str(pr, "url"),
                    is_draft=_get_bool(pr, "isDraft"),
                    is_approved=is_approved,
                    needs_response=has_changes_requested or has_comments,
                    has_review=len(human_reviews) > 0,
                    ci_status=ci_state,
                    unresolved_comment_count=unresolved_count,
                )
            )

    return all_prs


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


def close_pr(repo: str, pr_number: int) -> bool:
    """Close a PR without merging.

    Args:
        repo: Repository in 'owner/name' format
        pr_number: PR number

    Returns:
        True if successful, False otherwise
    """
    try:
        result = subprocess.run(
            ["gh", "pr", "close", str(pr_number), "--repo", repo],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
        )
        if result.returncode != 0:
            logger.warning("Failed to close PR: %s", result.stderr.strip())
            return False
        return True
    except subprocess.TimeoutExpired:
        logger.error("gh command timed out after %d seconds", SUBPROCESS_TIMEOUT)
        return False
    except FileNotFoundError:
        logger.error("gh CLI not found. Install it from https://cli.github.com/")
        return False


def get_review_requests(orgs: list[str] | None = None) -> list[ReviewRequest]:
    """Get open PRs where review is requested from current user."""
    owners = orgs or _get_orgs()
    all_prs: list[ReviewRequest] = []

    for owner in owners:
        query = REVIEW_REQUESTS_QUERY.format(org=owner)
        result = _run_gh_graphql(query)

        if not result:
            continue

        data = _get_dict(result, "data")
        search = _get_dict(data, "search")
        nodes = _get_list(search, "nodes")

        for pr in nodes:
            if not pr:
                continue

            # Extract requested teams from reviewRequests
            requested_teams: list[str] = []
            rr_wrapper = _get_dict(pr, "reviewRequests")
            review_requests = _get_list(rr_wrapper, "nodes")
            for req in review_requests:
                reviewer = _get_dict(req, "requestedReviewer")
                if reviewer and "slug" in reviewer:
                    requested_teams.append(_get_str(reviewer, "slug"))

            # Check if someone else has already submitted a review
            lr_wrapper = _get_dict(pr, "latestReviews")
            reviews = _get_list(lr_wrapper, "nodes")
            human_reviews = [
                r
                for r in reviews
                if _get_str(_get_dict(r, "author"), "login").lower()
                not in BOT_REVIEWERS
            ]
            has_other_review = len(human_reviews) > 0

            repo_info = _get_dict(pr, "repository")
            author_info = _get_dict(pr, "author")
            all_prs.append(
                ReviewRequest(
                    number=_get_int(pr, "number"),
                    title=_get_str(pr, "title"),
                    repository=_get_str(repo_info, "nameWithOwner", "unknown"),
                    url=_get_str(pr, "url"),
                    author=_get_str(author_info, "login", "unknown"),
                    created_at=_parse_datetime(_get_str(pr, "createdAt")),
                    requested_teams=requested_teams,
                    has_other_review=has_other_review,
                )
            )

    return all_prs


def _run_gh_api(
    endpoint: str, method: str = "GET"
) -> _JsonDict | list[_JsonDict] | None:
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


def get_notifications(orgs: list[str] | None = None) -> list[Notification]:
    """Get unread GitHub notifications for pull requests.

    Filters to only PR-related notifications and optionally by organization.
    """
    owners = orgs or _get_orgs()
    result = _run_gh_api("notifications?all=false&per_page=50")

    if not result or not isinstance(result, list):
        return []

    notifications: list[Notification] = []
    for item in result:
        if _get_str(item, "reason") in ("review_requested", "author"):
            continue

        subject = _get_dict(item, "subject")
        if _get_str(subject, "type") != "PullRequest":
            continue

        repo_dict = _get_dict(item, "repository")
        repo_full_name = _get_str(repo_dict, "full_name")
        if owners and not any(
            repo_full_name.startswith(f"{owner}/") for owner in owners
        ):
            continue

        subject_url = _get_str(subject, "url")
        pr_number: int | None = None
        html_url = ""
        if subject_url:
            parts = subject_url.split("/")
            if len(parts) >= 2 and parts[-2] == "pulls":
                pr_number = int(parts[-1])
                html_url = f"https://github.com/{repo_full_name}/pull/{pr_number}"

        notifications.append(
            Notification(
                id=_get_str(item, "id"),
                reason=_get_str(item, "reason", "unknown"),
                title=_get_str(subject, "title"),
                repository=repo_full_name,
                url=html_url,
                updated_at=_parse_datetime(_get_str(item, "updated_at")),
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
