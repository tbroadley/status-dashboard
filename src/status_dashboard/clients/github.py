import concurrent.futures
import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class GatedCheck:
    """A check run waiting on an environment approval the viewer can give."""

    name: str
    environment: str
    environment_id: str
    workflow_run_id: str


# CI state for a PR whose checks have all passed except gated ones.
AWAITING_APPROVAL = "AWAITING_APPROVAL"


@dataclass
class PullRequest:
    number: int
    title: str
    repository: str
    url: str
    created_at: datetime
    is_draft: bool = False
    is_approved: bool = False
    needs_response: bool = False
    has_review: bool = False
    ci_status: str | None = None
    mergeable: str | None = None
    unresolved_comment_count: int = 0
    reviewers: list[str] | None = None
    assignees: list[str] | None = None
    additions: int = 0
    deletions: int = 0
    viewer_did_author: bool = False
    gated_checks: list[GatedCheck] = field(default_factory=list)


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
    additions: int = 0
    deletions: int = 0


@dataclass
class Notification:
    id: str
    reason: str
    title: str
    repository: str
    url: str
    updated_at: datetime
    pr_number: int | None = None
    issue_number: int | None = None


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


MY_PRS_QUERY_TEMPLATE = """
query {{
  search(query: "{search_query}", type: ISSUE, first: 100) {{
    nodes {{
      ... on PullRequest {{
        number
        title
        url
        isDraft
        createdAt
        mergeable
        additions
        deletions
        viewerDidAuthor
        repository {{
          nameWithOwner
        }}
        reviewDecision
        latestReviews(first: 20) {{
          nodes {{
            author {{
              __typename
              login
            }}
            state
            viewerDidAuthor
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
        reviewRequests(first: 20) {{
          nodes {{
            requestedReviewer {{
              ... on User {{
                login
              }}
              ... on Team {{
                slug
              }}
            }}
          }}
        }}
        assignees(first: 10) {{
          nodes {{
            login
          }}
        }}
      }}
    }}
  }}
}}
"""


REVIEW_REQUESTS_QUERY = """
query {{
  search(query: "{search_query}", type: ISSUE, first: 100) {{
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
        additions
        deletions
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
              __typename
              login
            }}
            state
            viewerDidAuthor
          }}
        }}
      }}
    }}
  }}
}}
"""


_my_username: str | None = None


def get_my_username() -> str | None:
    """Get the current authenticated GitHub user's login via `gh api user`."""
    global _my_username  # noqa: PLW0603
    if _my_username is not None:
        return _my_username
    try:
        result = subprocess.run(
            ["gh", "api", "user", "--jq", ".login"],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
        )
        if result.returncode != 0:
            logger.warning("Failed to get GitHub username: %s", result.stderr.strip())
            return None
        _my_username = result.stdout.strip()
        return _my_username
    except subprocess.TimeoutExpired:
        logger.error("gh command timed out after %d seconds", SUBPROCESS_TIMEOUT)
        return None
    except FileNotFoundError:
        logger.error("gh CLI not found. Install it from https://cli.github.com/")
        return None


def _get_orgs() -> list[str]:
    """Get list of GitHub organizations from environment."""
    orgs_str = os.environ.get("GITHUB_ORGS", "")
    if orgs_str:
        return [org.strip() for org in orgs_str.split(",") if org.strip()]
    # Fall back to single GITHUB_ORG for backward compatibility
    return [os.environ.get("GITHUB_ORG", "METR")]


_DEFAULT_EXTRA_PR_REPOS = "ukgovernmentbeis/inspect_ai,meridianlabs-ai/inspect_scout"


def _get_extra_pr_repos() -> list[str]:
    """Get list of extra repos to include in My PRs, from environment."""
    repos_str = os.environ.get("GITHUB_EXTRA_PR_REPOS", _DEFAULT_EXTRA_PR_REPOS)
    return [r.strip() for r in repos_str.split(",") if r.strip()]


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


def _is_bot(author: _JsonDict) -> bool:
    login = _get_str(author, "login").lower()
    return (
        _get_str(author, "__typename") == "Bot"
        or login.endswith("[bot]")
        or login in BOT_REVIEWERS
    )


def _reviews_by_others(reviews: _JsonList) -> _JsonList:
    """Reviews submitted by humans other than me."""
    return [
        r
        for r in reviews
        if r
        and not _get_bool(r, "viewerDidAuthor")
        and not _is_bot(_get_dict(r, "author"))
    ]


def _parse_pr_node(pr: _JsonDict) -> PullRequest | None:
    """Parse a PR node from the GraphQL response into a PullRequest."""
    if not pr:
        return None

    latest_reviews = _get_dict(pr, "latestReviews")
    reviews = _get_list(latest_reviews, "nodes")

    human_reviews = _reviews_by_others(reviews)

    review_decision = _get_str(pr, "reviewDecision")
    is_approved = review_decision == "APPROVED"
    has_changes_requested = any(
        _get_str(r, "state") == "CHANGES_REQUESTED" for r in human_reviews
    )
    has_comments = any(_get_str(r, "state") == "COMMENTED" for r in human_reviews)

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

    rr_wrapper = _get_dict(pr, "reviewRequests")
    review_requests = _get_list(rr_wrapper, "nodes")
    reviewers: list[str] = []
    for req in review_requests:
        reviewer = _get_dict(req, "requestedReviewer")
        login = _get_str(reviewer, "login")
        slug = _get_str(reviewer, "slug")
        name = login or slug
        if name and name.lower() not in BOT_REVIEWERS:
            reviewers.append(name)

    assignees_wrapper = _get_dict(pr, "assignees")
    assignee_nodes = _get_list(assignees_wrapper, "nodes")
    assignees: list[str] = [
        _get_str(a, "login") for a in assignee_nodes if _get_str(a, "login")
    ]

    repo_info = _get_dict(pr, "repository")
    return PullRequest(
        number=_get_int(pr, "number"),
        title=_get_str(pr, "title"),
        repository=_get_str(repo_info, "nameWithOwner", "unknown"),
        url=_get_str(pr, "url"),
        created_at=_parse_datetime(_get_str(pr, "createdAt")),
        is_draft=_get_bool(pr, "isDraft"),
        is_approved=is_approved,
        needs_response=has_changes_requested or has_comments,
        has_review=len(human_reviews) > 0,
        ci_status=ci_state,
        mergeable=_get_str(pr, "mergeable") or None,
        unresolved_comment_count=unresolved_count,
        reviewers=reviewers,
        assignees=assignees,
        additions=_get_int(pr, "additions"),
        deletions=_get_int(pr, "deletions"),
        viewer_did_author=_get_bool(pr, "viewerDidAuthor"),
    )


def _run_my_prs_query(search_query: str) -> list[PullRequest]:
    """Run a My PRs query with the given search query and return parsed PRs."""
    query = MY_PRS_QUERY_TEMPLATE.format(search_query=search_query)
    result = _run_gh_graphql(query)
    if not result:
        return []

    data = _get_dict(result, "data")
    search = _get_dict(data, "search")
    nodes = _get_list(search, "nodes")

    prs: list[PullRequest] = []
    for node in nodes:
        parsed = _parse_pr_node(node)
        if parsed:
            prs.append(parsed)
    return prs


def get_my_prs(orgs: list[str] | None = None) -> list[PullRequest]:
    """Get open PRs that I authored or that are assigned to me.

    A PR I opened stays mine even once someone else is assigned to it: changes
    requested and merge conflicts are still mine to fix, so hiding it on the
    grounds that it's been handed off loses exactly the PR that needs work.
    """
    owners = orgs or _get_orgs()
    prs_by_url: dict[str, PullRequest] = {}

    # GitHub search OR's repeated org:/repo: qualifiers, so a single query can
    # span every org and extra repo at once. We only need two queries (author
    # and assignee, which can't be combined since same-name qualifiers AND), and
    # we run them in parallel to keep My PRs loading fast.
    scope = " ".join(
        [f"org:{owner}" for owner in owners]
        + [f"repo:{repo}" for repo in _get_extra_pr_repos()]
    )
    search_queries = [
        f"author:@me state:open type:pr {scope}",
        f"assignee:@me state:open type:pr {scope}",
    ]

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(search_queries)) as ex:
        for prs in ex.map(_run_my_prs_query, search_queries):
            for pr in prs:
                _ = prs_by_url.setdefault(pr.url, pr)

    # Both queries already scope to me, so their union needs no further filtering.
    all_prs = sorted(prs_by_url.values(), key=lambda pr: pr.created_at, reverse=True)
    _add_gated_checks([pr for pr in all_prs if pr.ci_status == "PENDING"])
    return all_prs


_CHECK_CONTEXTS_FIELDS = """
      commits(last: 1) { nodes { commit { statusCheckRollup { contexts(first: 100) {
        pageInfo { hasNextPage }
        nodes {
          __typename
          ... on CheckRun {
            name
            status
            pendingDeploymentRequest {
              currentUserCanApprove
              environment { ... on Environment { id name } }
            }
            checkSuite { workflowRun { id } }
          }
          ... on StatusContext { state }
        }
      } } } } }
"""


def _gated_check(context: _JsonDict) -> GatedCheck | None:
    """The approval a waiting check run needs, if the viewer can give it."""
    if _get_str(context, "status") != "WAITING":
        return None
    request = _get_dict(context, "pendingDeploymentRequest")
    if not _get_bool(request, "currentUserCanApprove"):
        return None
    environment = _get_dict(request, "environment")
    run = _get_dict(_get_dict(context, "checkSuite"), "workflowRun")
    environment_id = _get_str(environment, "id")
    run_id = _get_str(run, "id")
    if not environment_id or not run_id:
        return None
    return GatedCheck(
        name=_get_str(context, "name"),
        environment=_get_str(environment, "name"),
        environment_id=environment_id,
        workflow_run_id=run_id,
    )


def _is_unfinished(context: _JsonDict) -> bool:
    if _get_str(context, "__typename") == "StatusContext":
        return _get_str(context, "state") in ("PENDING", "EXPECTED")
    return _get_str(context, "status") != "COMPLETED"


def _add_gated_checks(prs: list[PullRequest]) -> None:
    """Find checks on pending PRs that are waiting for an approval I can give.

    Fetching every check for every PR in the main search times out, so this
    looks only at pending PRs, in one batched request. A PR whose only
    unfinished checks are gated becomes AWAITING_APPROVAL: everything that ran
    has passed. If the checks are truncated, unseen ones could still be
    running, so the PR stays pending. On failure PRs are left unchanged.
    """
    if not prs:
        return
    parts: list[str] = []
    for i, pr in enumerate(prs):
        owner, _, name = pr.repository.partition("/")
        parts.append(
            f"pr{i}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{"
            + f" pullRequest(number: {pr.number}) {{ {_CHECK_CONTEXTS_FIELDS} }} }}"
        )
    result = _run_gh_graphql("query { " + "\n".join(parts) + " }")
    data = _get_dict(result or {}, "data")
    for i, pr in enumerate(prs):
        commits = _get_list(
            _get_dict(_get_dict(_get_dict(data, f"pr{i}"), "pullRequest"), "commits"),
            "nodes",
        )
        if not commits:
            continue
        rollup = _get_dict(_get_dict(commits[0], "commit"), "statusCheckRollup")
        contexts_wrapper = _get_dict(rollup, "contexts")
        gated: list[GatedCheck] = []
        blocked = False
        for context in _get_list(contexts_wrapper, "nodes"):
            if not context:
                continue
            check = _gated_check(context)
            if check:
                gated.append(check)
            elif _is_unfinished(context):
                blocked = True
        truncated = _get_bool(_get_dict(contexts_wrapper, "pageInfo"), "hasNextPage")
        pr.gated_checks = gated
        if gated and not blocked and not truncated:
            pr.ci_status = AWAITING_APPROVAL


def remove_self_as_reviewer(repo: str, pr_number: int) -> bool:
    """Remove the current user as a requested reviewer from a PR.

    `gh pr edit --remove-reviewer @me` exits 0 without removing anyone, since gh
    does not expand `@me` for reviewers, so this calls the REST API with the login.

    Args:
        repo: Repository in 'owner/name' format
        pr_number: PR number

    Returns:
        True if the user is no longer a requested reviewer, False otherwise
    """
    login = get_my_username()
    if not login:
        return False
    try:
        result = subprocess.run(
            [
                "gh",
                "api",
                "-X",
                "DELETE",
                f"repos/{repo}/pulls/{pr_number}/requested_reviewers",
                "-f",
                f"reviewers[]={login}",
            ],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
        )
        if result.returncode != 0:
            logger.warning("Failed to remove reviewer: %s", result.stderr.strip())
            return False
        pr: _JsonDict = json.loads(result.stdout)  # pyright: ignore[reportAny]
    except subprocess.TimeoutExpired:
        logger.error("gh command timed out after %d seconds", SUBPROCESS_TIMEOUT)
        return False
    except json.JSONDecodeError as e:
        logger.error("Failed to parse gh output: %s", e)
        return False
    except FileNotFoundError:
        logger.error("gh CLI not found. Install it from https://cli.github.com/")
        return False
    remaining = {
        _get_str(r, "login").lower() for r in _get_list(pr, "requested_reviewers")
    }
    if login.lower() in remaining:
        logger.warning("Still a requested reviewer on %s#%d", repo, pr_number)
        return False
    return True


def unsubscribe_from_pr(repo: str, pr_number: int) -> bool:
    """Unsubscribe the current user from a PR's notifications.

    Args:
        repo: Repository in 'owner/name' format
        pr_number: PR number

    Returns:
        True if successful, False otherwise
    """
    pr = _run_gh_api(f"repos/{repo}/pulls/{pr_number}")
    node_id = pr.get("node_id") if isinstance(pr, dict) else None
    if not isinstance(node_id, str) or not node_id:
        logger.warning("Failed to look up node ID for %s#%d", repo, pr_number)
        return False
    mutation = f"""
    mutation {{
      updateSubscription(
        input: {{subscribableId: {json.dumps(node_id)}, state: UNSUBSCRIBED}}
      ) {{
        subscribable {{ viewerSubscription }}
      }}
    }}
    """
    data = _run_gh_graphql(mutation)
    subscription = _get_dict(
        _get_dict(_get_dict(data or {}, "data"), "updateSubscription"),
        "subscribable",
    )
    if _get_str(subscription, "viewerSubscription") != "UNSUBSCRIBED":
        logger.warning("Failed to unsubscribe from %s#%d", repo, pr_number)
        return False
    return True


def approve_gated_checks(checks: list[GatedCheck]) -> bool:
    """Approve the environments that gated checks are waiting on, so they run.

    Approvals are per workflow run, so checks are grouped by run and approved in
    one mutation. Returns True only if every run's approval succeeded.
    """
    environments_by_run: dict[str, list[str]] = {}
    for check in checks:
        env_ids = environments_by_run.setdefault(check.workflow_run_id, [])
        if check.environment_id not in env_ids:
            env_ids.append(check.environment_id)
    if not environments_by_run:
        return False

    parts = [
        f"run{i}: approveDeployments(input: {{"
        + f"workflowRunId: {json.dumps(run_id)}, "
        + f"environmentIds: {json.dumps(env_ids)}"
        + "}) { deployments { id } }"
        for i, (run_id, env_ids) in enumerate(environments_by_run.items())
    ]
    result = _run_gh_graphql("mutation { " + "\n".join(parts) + " }")
    data = _get_dict(result or {}, "data")
    if (
        not result
        or result.get("errors")
        or any(not _get_dict(data, f"run{i}") for i in range(len(parts)))
    ):
        logger.warning("Failed to approve %d gated checks", len(checks))
        return False
    return True


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
    """Get open PRs where review is requested directly from current user.

    Excludes PRs where review is only requested via team membership.
    """
    owners = orgs or _get_orgs()
    all_prs: list[ReviewRequest] = []
    my_username = get_my_username()

    # GitHub search OR's repeated org: qualifiers, so all orgs fit in one query.
    scope = " ".join(f"org:{owner}" for owner in owners)
    query = REVIEW_REQUESTS_QUERY.format(
        search_query=f"review-requested:@me state:open type:pr {scope}"
    )
    result = _run_gh_graphql(query)
    if not result:
        return []

    data = _get_dict(result, "data")
    search = _get_dict(data, "search")
    nodes = _get_list(search, "nodes")

    for pr in nodes:
        if not pr:
            continue

        # Extract requested teams and directly-requested users
        requested_teams: list[str] = []
        directly_requested_users: list[str] = []
        rr_wrapper = _get_dict(pr, "reviewRequests")
        review_requests = _get_list(rr_wrapper, "nodes")
        for req in review_requests:
            reviewer = _get_dict(req, "requestedReviewer")
            if reviewer and "slug" in reviewer:
                requested_teams.append(_get_str(reviewer, "slug"))
            login = _get_str(reviewer, "login")
            if login:
                directly_requested_users.append(login)

        # Skip PRs where review is only requested via team, not directly
        if my_username and my_username not in directly_requested_users:
            continue

        # Check if someone else has already submitted a review
        lr_wrapper = _get_dict(pr, "latestReviews")
        reviews = _get_list(lr_wrapper, "nodes")
        has_other_review = len(_reviews_by_others(reviews)) > 0

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
                additions=_get_int(pr, "additions"),
                deletions=_get_int(pr, "deletions"),
            )
        )

    all_prs.sort(key=lambda pr: pr.created_at, reverse=True)
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


@dataclass
class _PRActivity:
    state: str
    my_last_activity: datetime | None
    others_last_activity: datetime | None
    directly_requested: bool = False
    i_participated: bool = False


_PR_ACTIVITY_FIELDS = """
      state
      comments(last: 30) {
        nodes { viewerDidAuthor createdAt author { __typename login } }
      }
      reviews(last: 30) {
        nodes { viewerDidAuthor submittedAt author { __typename login } }
      }
      reviewRequests(first: 30) {
        nodes { requestedReviewer { ... on User { isViewer } } }
      }
"""


def _get_pr_activity(
    prs: list[tuple[str, int]],
) -> dict[tuple[str, int], _PRActivity]:
    """Fetch state and latest human activity for PRs in one GraphQL request.

    Keys are (lowercased repo, number). Returns an empty dict on failure.
    """
    if not prs:
        return {}

    parts: list[str] = []
    for i, (repo, number) in enumerate(prs):
        owner, _, name = repo.partition("/")
        parts.append(
            f"pr{i}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{"
            + f" pullRequest(number: {number}) {{ {_PR_ACTIVITY_FIELDS} }} }}"
        )
    result = _run_gh_graphql("query { " + "\n".join(parts) + " }")
    if not result:
        return {}
    data = _get_dict(result, "data")

    activity: dict[tuple[str, int], _PRActivity] = {}
    for i, (repo, number) in enumerate(prs):
        pr = _get_dict(_get_dict(data, f"pr{i}"), "pullRequest")
        if not pr:
            continue

        mine: list[datetime] = []
        others: list[datetime] = []
        participated = False
        items = [
            (node, _get_str(node, "createdAt"))
            for node in _get_list(_get_dict(pr, "comments"), "nodes")
        ] + [
            (node, _get_str(node, "submittedAt"))
            for node in _get_list(_get_dict(pr, "reviews"), "nodes")
        ]
        for node, timestamp in items:
            if not node:
                continue
            if _get_bool(node, "viewerDidAuthor"):
                participated = True
            if not timestamp:  # pending reviews have no submittedAt
                continue
            if _get_bool(node, "viewerDidAuthor"):
                mine.append(_parse_datetime(timestamp))
            elif not _is_bot(_get_dict(node, "author")):
                others.append(_parse_datetime(timestamp))

        activity[(repo.lower(), number)] = _PRActivity(
            state=_get_str(pr, "state"),
            my_last_activity=max(mine, default=None),
            others_last_activity=max(others, default=None),
            directly_requested=any(
                _get_bool(_get_dict(req, "requestedReviewer"), "isViewer")
                for req in _get_list(_get_dict(pr, "reviewRequests"), "nodes")
                if req
            ),
            i_participated=participated,
        )
    return activity


def _is_settled(notification: Notification, activity: _PRActivity | None) -> bool:
    """Whether a notification no longer needs attention.

    True for review requests on merged/closed PRs; for review requests that
    never named me directly (e.g. only a team I'm in) on PRs I haven't taken
    part in; and for PRs where my latest review or comment is newer than any
    human activity by others.
    """
    if activity is None:
        return False
    if notification.reason == "review_requested":
        if activity.state in ("MERGED", "CLOSED"):
            return True
        if not activity.directly_requested and not activity.i_participated:
            return True
    if activity.my_last_activity is None:
        return False
    return (
        activity.others_last_activity is None
        or activity.others_last_activity <= activity.my_last_activity
    )


def get_notifications(orgs: list[str] | None = None) -> list[Notification]:
    """Get unread GitHub notifications for pull requests and issues.

    Filters to PR and issue notifications, optionally by organization, and
    drops PR notifications that are settled (see `_is_settled`). Notifications
    on my own PRs are skipped because My PRs already shows them; notifications
    on my own issues are kept, since nothing else surfaces them.
    """
    owners = orgs or _get_orgs()
    result = _run_gh_api("notifications?all=false&per_page=50")

    if not result or not isinstance(result, list):
        return []

    notifications: list[Notification] = []
    for item in result:
        subject = _get_dict(item, "subject")
        subject_type = _get_str(subject, "type")
        if subject_type not in ("PullRequest", "Issue"):
            continue
        if subject_type == "PullRequest" and _get_str(item, "reason") == "author":
            continue

        repo_dict = _get_dict(item, "repository")
        repo_full_name = _get_str(repo_dict, "full_name")
        if owners and not any(
            repo_full_name.startswith(f"{owner}/") for owner in owners
        ):
            continue

        subject_url = _get_str(subject, "url")
        pr_number: int | None = None
        issue_number: int | None = None
        html_url = ""
        if subject_url:
            parts = subject_url.split("/")
            if len(parts) >= 2 and parts[-1].isdigit():
                if parts[-2] == "pulls":
                    pr_number = int(parts[-1])
                    html_url = f"https://github.com/{repo_full_name}/pull/{pr_number}"
                elif parts[-2] == "issues":
                    issue_number = int(parts[-1])
                    html_url = (
                        f"https://github.com/{repo_full_name}/issues/{issue_number}"
                    )

        notifications.append(
            Notification(
                id=_get_str(item, "id"),
                reason=_get_str(item, "reason", "unknown"),
                title=_get_str(subject, "title"),
                repository=repo_full_name,
                url=html_url,
                updated_at=_parse_datetime(_get_str(item, "updated_at")),
                pr_number=pr_number,
                issue_number=issue_number,
            )
        )

    activity = _get_pr_activity(
        [(n.repository, n.pr_number) for n in notifications if n.pr_number]
    )
    return [
        n
        for n in notifications
        if not n.pr_number
        or not _is_settled(n, activity.get((n.repository.lower(), n.pr_number)))
    ]


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
