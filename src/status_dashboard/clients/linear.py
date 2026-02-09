import json
import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_JsonDict = dict[str, Any]  # pyright: ignore[reportExplicitAny]
_JsonList = list[_JsonDict]

# Order for sorting issues by status
STATUS_ORDER = {
    "In Review": 0,
    "In Progress": 1,
    "Todo": 2,
    "Backlog": 3,
}


@dataclass
class Issue:
    id: str
    identifier: str
    title: str
    state: str
    url: str
    team_id: str = ""
    assignee_initials: str | None = None
    sort_order: float = 0.0


ISSUES_QUERY = """
query GetProjectIssues($projectName: String!) {
  projects(filter: { name: { containsIgnoreCase: $projectName } }, first: 1) {
    nodes {
      issues(first: 100) {
        nodes {
          id
          identifier
          title
          state {
            name
          }
          url
          sortOrder
          assignee {
            name
            displayName
          }
          team {
            id
          }
        }
      }
    }
  }
}
"""

TEAM_ISSUES_QUERY = """
query GetTeamIssues($teamKey: String!) {
  teams(filter: { key: { eq: $teamKey } }) {
    nodes {
      issues(first: 40) {
        nodes {
          id
          identifier
          title
          state {
            name
          }
          url
          assignee {
            name
            displayName
          }
          team {
            id
          }
          project {
            id
          }
        }
      }
    }
  }
}
"""

WORKFLOW_STATES_QUERY = """
query GetWorkflowStates($teamId: ID!) {
  workflowStates(filter: { team: { id: { eq: $teamId } } }) {
    nodes {
      id
      name
      type
    }
  }
}
"""

UPDATE_ISSUE_MUTATION = """
mutation UpdateIssue($issueId: String!, $stateId: String!) {
  issueUpdate(id: $issueId, input: { stateId: $stateId }) {
    success
  }
}
"""

CREATE_ISSUE_MUTATION = """
mutation CreateIssue($teamId: String!, $title: String!, $stateId: String, $assigneeId: String, $projectId: String) {
  issueCreate(input: { teamId: $teamId, title: $title, stateId: $stateId, assigneeId: $assigneeId, projectId: $projectId }) {
    success
    issue {
      id
      identifier
      url
    }
  }
}
"""

ASSIGN_ISSUE_MUTATION = """
mutation AssignIssue($issueId: String!, $assigneeId: String) {
  issueUpdate(id: $issueId, input: { assigneeId: $assigneeId }) {
    success
  }
}
"""

UPDATE_SORT_ORDER_MUTATION = """
mutation UpdateSortOrder($issueId: String!, $sortOrder: Float!) {
  issueUpdate(id: $issueId, input: { sortOrder: $sortOrder }) {
    success
  }
}
"""

GET_VIEWER_QUERY = """
query GetViewer {
  viewer {
    id
  }
}
"""

GET_TEAM_QUERY = """
query GetTeam($projectName: String!) {
  projects(filter: { name: { containsIgnoreCase: $projectName } }, first: 1) {
    nodes {
      teams(first: 1) {
        nodes {
          id
          key
        }
      }
    }
  }
}
"""

GET_TEAM_MEMBERS_QUERY = """
query GetTeamMembers {
  users {
    nodes {
      id
      name
      displayName
      email
    }
  }
}
"""

GET_PROJECT_ID_QUERY = """
query GetProjectId($projectName: String!) {
  projects(filter: { name: { containsIgnoreCase: $projectName } }, first: 1) {
    nodes {
      id
      name
    }
  }
}
"""

GET_ISSUE_QUERY = """
query GetIssue($issueId: String!) {
  issue(id: $issueId) {
    id
    state {
      name
    }
    assignee {
      id
    }
  }
}
"""

# Map state names to their types for lookup
STATE_NAME_MAP = {
    "backlog": "Backlog",
    "todo": "Todo",
    "in_progress": "In Progress",
    "in_review": "In Review",
    "done": "Done",
}

# Reverse mapping from display name to internal key
STATE_DISPLAY_TO_KEY = {v: k for k, v in STATE_NAME_MAP.items()}


def _get_str(d: _JsonDict, key: str, default: str = "") -> str:
    val = d.get(key, default)  # pyright: ignore[reportAny]
    return str(val) if val is not None else default  # pyright: ignore[reportAny]


def _get_float(d: _JsonDict, key: str, default: float = 0.0) -> float:
    val = d.get(key, default)  # pyright: ignore[reportAny]
    return float(val) if val is not None else default  # pyright: ignore[reportAny]


def _get_bool(d: _JsonDict, key: str, default: bool = False) -> bool:
    val = d.get(key, default)  # pyright: ignore[reportAny]
    return bool(val)  # pyright: ignore[reportAny]


def _get_dict(d: _JsonDict, key: str) -> _JsonDict:
    val = d.get(key)
    if isinstance(val, dict):
        return val  # pyright: ignore[reportUnknownVariableType]
    return {}


def _get_list(d: _JsonDict, key: str) -> _JsonList:
    val = d.get(key)
    if isinstance(val, list):
        return val  # pyright: ignore[reportUnknownVariableType]
    return []


def _parse_json(response: httpx.Response) -> _JsonDict:
    data: _JsonDict = response.json()  # pyright: ignore[reportAny]
    return data


def get_initials(name: str | None) -> str | None:
    """Get initials from a name like 'Thomas Broadley' -> 'TB'."""
    if not name:
        return None
    parts = name.split()
    if len(parts) >= 2:
        return (parts[0][0] + parts[-1][0]).upper()
    elif len(parts) == 1:
        return parts[0][0].upper()
    return None


def get_project_issues(
    project_name: str | None = None,
    api_key: str | None = None,
) -> list[Issue]:
    """Get Linear issues for a specific project plus team issues without a project, sorted by status."""
    key = api_key or os.environ.get("LINEAR_API_KEY")
    if not key:
        logger.warning("LINEAR_API_KEY not set, skipping Linear issues")
        return []

    project = project_name or os.environ.get("LINEAR_PROJECT", "")

    try:
        response = httpx.post(
            "https://api.linear.app/graphql",
            json={
                "query": ISSUES_QUERY,
                "variables": {"projectName": project},
            },
            headers={"Authorization": key},
            timeout=10,
        )
        _ = response.raise_for_status()
        data = _parse_json(response)
    except httpx.TimeoutException:
        logger.error("Linear API request timed out")
        return []
    except httpx.HTTPStatusError as e:
        logger.error("Linear API returned error: %s", e.response.status_code)
        return []
    except httpx.RequestError as e:
        logger.error("Linear API request failed: %s", e)
        return []
    except json.JSONDecodeError as e:
        logger.error("Failed to parse Linear response: %s", e)
        return []

    issues_by_id: dict[str, Issue] = {}
    data_payload = _get_dict(data, "data")
    projects = _get_list(_get_dict(data_payload, "projects"), "nodes")
    for proj in projects:
        issue_nodes = _get_list(_get_dict(proj, "issues"), "nodes")
        for issue in issue_nodes:
            assignee = _get_dict(issue, "assignee")
            assignee_name: str | None = (
                _get_str(assignee, "displayName") or _get_str(assignee, "name") or None
                if assignee
                else None
            )

            issues_by_id[_get_str(issue, "id")] = Issue(
                id=_get_str(issue, "id"),
                identifier=_get_str(issue, "identifier"),
                title=_get_str(issue, "title"),
                state=_get_str(_get_dict(issue, "state"), "name", "Unknown"),
                url=_get_str(issue, "url"),
                team_id=_get_str(_get_dict(issue, "team"), "id"),
                assignee_initials=get_initials(assignee_name),
            )

    # Also get team issues without a project
    team_info = get_team_info(project, key)
    if team_info:
        _, team_key = team_info
        team_issues = _get_team_issues_without_project(team_key, key)
        for issue in team_issues:
            if issue.id not in issues_by_id:
                issues_by_id[issue.id] = issue

    issues = list(issues_by_id.values())

    # Sort by status first (In Review, In Progress, Todo, Backlog), then by sort_order within each status
    issues.sort(key=lambda i: (STATUS_ORDER.get(i.state, 999), i.sort_order))

    return issues


def _get_team_issues_without_project(
    team_key: str,
    api_key: str,
) -> list[Issue]:
    """Get Linear issues for a team that have no project assigned."""
    try:
        response = httpx.post(
            "https://api.linear.app/graphql",
            json={
                "query": TEAM_ISSUES_QUERY,
                "variables": {"teamKey": team_key},
            },
            headers={"Authorization": api_key},
            timeout=10,
        )
        _ = response.raise_for_status()
        data = _parse_json(response)
    except httpx.TimeoutException:
        logger.error("Linear API request timed out")
        return []
    except httpx.HTTPStatusError as e:
        logger.error("Linear API returned error: %s", e.response.status_code)
        return []
    except httpx.RequestError as e:
        logger.error("Linear API request failed: %s", e)
        return []
    except json.JSONDecodeError as e:
        logger.error("Failed to parse Linear response: %s", e)
        return []

    issues: list[Issue] = []
    data_payload = _get_dict(data, "data")
    teams = _get_list(_get_dict(data_payload, "teams"), "nodes")
    for team in teams:
        issue_nodes = _get_list(_get_dict(team, "issues"), "nodes")
        for issue in issue_nodes:
            # Skip issues that have a project
            if _get_dict(issue, "project"):
                continue

            assignee = _get_dict(issue, "assignee")
            assignee_name: str | None = (
                _get_str(assignee, "displayName") or _get_str(assignee, "name") or None
                if assignee
                else None
            )

            issues.append(
                Issue(
                    id=_get_str(issue, "id"),
                    identifier=_get_str(issue, "identifier"),
                    title=_get_str(issue, "title"),
                    state=_get_str(_get_dict(issue, "state"), "name", "Unknown"),
                    url=_get_str(issue, "url"),
                    team_id=_get_str(_get_dict(issue, "team"), "id"),
                    assignee_initials=get_initials(assignee_name),
                    sort_order=_get_float(issue, "sortOrder"),
                )
            )

    return issues


def set_issue_state(
    issue_id: str, team_id: str, state_name: str, api_key: str | None = None
) -> bool:
    """Set a Linear issue's state. Returns True on success.

    state_name should be one of: backlog, todo, in_progress, in_review, done
    """
    key = api_key or os.environ.get("LINEAR_API_KEY")
    if not key:
        logger.error("LINEAR_API_KEY not set")
        return False

    target_state = STATE_NAME_MAP.get(state_name)
    if not target_state:
        logger.error("Unknown state: %s", state_name)
        return False

    try:
        # Get all workflow states for this team
        response = httpx.post(
            "https://api.linear.app/graphql",
            json={
                "query": WORKFLOW_STATES_QUERY,
                "variables": {"teamId": team_id},
            },
            headers={"Authorization": key},
            timeout=10,
        )
        data = _parse_json(response)

        if "errors" in data:
            logger.error("Linear API error getting workflow states: %s", data["errors"])  # pyright: ignore[reportAny]
            return False

        data_payload = _get_dict(data, "data")
        states = _get_list(_get_dict(data_payload, "workflowStates"), "nodes")

        # Find the target state
        state_id = None
        for state in states:
            if _get_str(state, "name") == target_state:
                state_id = _get_str(state, "id")
                break

        if not state_id:
            logger.error("State '%s' not found for team %s", target_state, team_id)
            return False

        # Update the issue state
        response = httpx.post(
            "https://api.linear.app/graphql",
            json={
                "query": UPDATE_ISSUE_MUTATION,
                "variables": {"issueId": issue_id, "stateId": state_id},
            },
            headers={"Authorization": key},
            timeout=10,
        )
        result = _parse_json(response)

        if "errors" in result:
            logger.error("Linear API error updating issue: %s", result["errors"])  # pyright: ignore[reportAny]
            return False

        return _get_bool(_get_dict(_get_dict(result, "data"), "issueUpdate"), "success")

    except httpx.HTTPStatusError as e:
        logger.error(
            "Failed to set issue state: %s - %s",
            e.response.status_code,
            e.response.text,
        )
        return False
    except httpx.RequestError as e:
        logger.error("Failed to set issue state: %s", e)
        return False


def complete_issue(issue_id: str, team_id: str, api_key: str | None = None) -> bool:
    """Mark a Linear issue as Done. Returns True on success."""
    return set_issue_state(issue_id, team_id, "done", api_key)


def get_team_info(
    project_name: str | None = None, api_key: str | None = None
) -> tuple[str, str] | None:
    """Get the team ID and key for a project. Returns (team_id, team_key) or None on error."""
    key = api_key or os.environ.get("LINEAR_API_KEY")
    if not key:
        logger.error("LINEAR_API_KEY not set")
        return None

    project = project_name or os.environ.get("LINEAR_PROJECT", "")

    try:
        response = httpx.post(
            "https://api.linear.app/graphql",
            json={
                "query": GET_TEAM_QUERY,
                "variables": {"projectName": project},
            },
            headers={"Authorization": key},
            timeout=10,
        )
        _ = response.raise_for_status()
        data = _parse_json(response)

        if "errors" in data:
            logger.error("Linear API error getting team: %s", data["errors"])  # pyright: ignore[reportAny]
            return None

        data_payload = _get_dict(data, "data")
        projects = _get_list(_get_dict(data_payload, "projects"), "nodes")
        if not projects:
            logger.error("Project '%s' not found", project)
            return None

        teams = _get_list(_get_dict(projects[0], "teams"), "nodes")
        if not teams:
            logger.error("No teams found for project '%s'", project)
            return None

        return _get_str(teams[0], "id"), _get_str(teams[0], "key")

    except httpx.RequestError as e:
        logger.error("Failed to get team info: %s", e)
        return None


def get_team_id(
    project_name: str | None = None, api_key: str | None = None
) -> str | None:
    """Get the team ID for a project. Returns None on error."""
    info = get_team_info(project_name, api_key)
    return info[0] if info else None


def get_team_members(api_key: str | None = None) -> list[dict[str, str]]:
    """Get all users in the workspace. Returns list of dicts with id, name, displayName."""
    key = api_key or os.environ.get("LINEAR_API_KEY")
    if not key:
        logger.error("LINEAR_API_KEY not set")
        return []

    try:
        response = httpx.post(
            "https://api.linear.app/graphql",
            json={"query": GET_TEAM_MEMBERS_QUERY},
            headers={"Authorization": key},
            timeout=10,
        )
        _ = response.raise_for_status()
        data = _parse_json(response)

        if "errors" in data:
            logger.error("Linear API error getting team members: %s", data["errors"])  # pyright: ignore[reportAny]
            return []

        data_payload = _get_dict(data, "data")
        users: list[dict[str, str]] = _get_list(
            _get_dict(data_payload, "users"), "nodes"
        )
        return users

    except httpx.RequestError as e:
        logger.error("Failed to get team members: %s", e)
        return []


def get_project_id(project_name: str, api_key: str | None = None) -> str | None:
    key = api_key or os.environ.get("LINEAR_API_KEY")
    if not key:
        logger.error("LINEAR_API_KEY not set")
        return None

    try:
        response = httpx.post(
            "https://api.linear.app/graphql",
            json={
                "query": GET_PROJECT_ID_QUERY,
                "variables": {"projectName": project_name},
            },
            headers={"Authorization": key},
            timeout=10,
        )
        _ = response.raise_for_status()
        data = _parse_json(response)

        if "errors" in data:
            logger.error("Linear API error getting project ID: %s", data["errors"])  # pyright: ignore[reportAny]
            return None

        data_payload = _get_dict(data, "data")
        projects = _get_list(_get_dict(data_payload, "projects"), "nodes")
        if not projects:
            logger.error("Project '%s' not found", project_name)
            return None

        return _get_str(projects[0], "id") or None

    except httpx.RequestError as e:
        logger.error("Failed to get project ID: %s", e)
        return None


def create_issue(
    title: str,
    team_id: str,
    state_name: str | None = None,
    assignee_id: str | None = None,
    project_name: str | None = None,
    api_key: str | None = None,
) -> bool:
    """Create a new Linear issue. Returns True on success.

    state_name should be one of: backlog, todo, in_progress, in_review, done
    If not provided, uses the team's default state.
    project_name defaults to LINEAR_PROJECT env var if not provided.
    """
    key = api_key or os.environ.get("LINEAR_API_KEY")
    if not key:
        logger.error("LINEAR_API_KEY not set")
        return False

    project = project_name or os.environ.get("LINEAR_PROJECT", "")
    project_id = get_project_id(project, key) if project else None

    state_id = None
    if state_name:
        target_state = STATE_NAME_MAP.get(state_name)
        if not target_state:
            logger.error("Unknown state: %s", state_name)
            return False

        try:
            response = httpx.post(
                "https://api.linear.app/graphql",
                json={
                    "query": WORKFLOW_STATES_QUERY,
                    "variables": {"teamId": team_id},
                },
                headers={"Authorization": key},
                timeout=10,
            )
            data = _parse_json(response)

            if "errors" in data:
                logger.error(
                    "Linear API error getting workflow states: %s",
                    data["errors"],  # pyright: ignore[reportAny]
                )
                return False

            data_payload = _get_dict(data, "data")
            states = _get_list(_get_dict(data_payload, "workflowStates"), "nodes")
            for state in states:
                if _get_str(state, "name") == target_state:
                    state_id = _get_str(state, "id")
                    break

            if not state_id:
                logger.error("State '%s' not found for team %s", target_state, team_id)
                return False

        except httpx.RequestError as e:
            logger.error("Failed to get workflow states: %s", e)
            return False

    # Create the issue
    try:
        variables = {
            "teamId": team_id,
            "title": title,
        }
        if state_id:
            variables["stateId"] = state_id
        if assignee_id:
            variables["assigneeId"] = assignee_id
        if project_id:
            variables["projectId"] = project_id

        response = httpx.post(
            "https://api.linear.app/graphql",
            json={
                "query": CREATE_ISSUE_MUTATION,
                "variables": variables,
            },
            headers={"Authorization": key},
            timeout=10,
        )
        result = _parse_json(response)

        if "errors" in result:
            logger.error("Linear API error creating issue: %s", result["errors"])  # pyright: ignore[reportAny]
            return False

        return _get_bool(_get_dict(_get_dict(result, "data"), "issueCreate"), "success")

    except httpx.HTTPStatusError as e:
        logger.error(
            "Failed to create issue: %s - %s", e.response.status_code, e.response.text
        )
        return False
    except httpx.RequestError as e:
        logger.error("Failed to create issue: %s", e)
        return False


def get_viewer_id(api_key: str | None = None) -> str | None:
    key = api_key or os.environ.get("LINEAR_API_KEY")
    if not key:
        logger.error("LINEAR_API_KEY not set")
        return None

    try:
        response = httpx.post(
            "https://api.linear.app/graphql",
            json={"query": GET_VIEWER_QUERY},
            headers={"Authorization": key},
            timeout=10,
        )
        _ = response.raise_for_status()
        data = _parse_json(response)

        if "errors" in data:
            logger.error("Linear API error getting viewer: %s", data["errors"])  # pyright: ignore[reportAny]
            return None

        data_payload = _get_dict(data, "data")
        viewer = _get_dict(data_payload, "viewer")
        return _get_str(viewer, "id") or None

    except httpx.RequestError as e:
        logger.error("Failed to get viewer ID: %s", e)
        return None


def assign_issue(
    issue_id: str, assignee_id: str | None, api_key: str | None = None
) -> bool:
    key = api_key or os.environ.get("LINEAR_API_KEY")
    if not key:
        logger.error("LINEAR_API_KEY not set")
        return False

    try:
        response = httpx.post(
            "https://api.linear.app/graphql",
            json={
                "query": ASSIGN_ISSUE_MUTATION,
                "variables": {"issueId": issue_id, "assigneeId": assignee_id},
            },
            headers={"Authorization": key},
            timeout=10,
        )
        result = _parse_json(response)

        if "errors" in result:
            logger.error("Linear API error assigning issue: %s", result["errors"])  # pyright: ignore[reportAny]
            return False

        return _get_bool(_get_dict(_get_dict(result, "data"), "issueUpdate"), "success")

    except httpx.RequestError as e:
        logger.error("Failed to assign issue: %s", e)
        return False


def get_issue(issue_id: str, api_key: str | None = None) -> _JsonDict | None:
    """Get a Linear issue by ID. Returns dict with state and assignee info, or None."""
    key = api_key or os.environ.get("LINEAR_API_KEY")
    if not key:
        logger.error("LINEAR_API_KEY not set")
        return None

    try:
        response = httpx.post(
            "https://api.linear.app/graphql",
            json={
                "query": GET_ISSUE_QUERY,
                "variables": {"issueId": issue_id},
            },
            headers={"Authorization": key},
            timeout=10,
        )
        _ = response.raise_for_status()
        data = _parse_json(response)

        if "errors" in data:
            logger.error("Linear API error getting issue: %s", data["errors"])  # pyright: ignore[reportAny]
            return None

        data_payload = _get_dict(data, "data")
        issue = _get_dict(data_payload, "issue")
        return issue or None

    except httpx.RequestError as e:
        logger.error("Failed to get issue: %s", e)
        return None


def set_issue_state_by_name(
    issue_id: str, team_id: str, state_display_name: str, api_key: str | None = None
) -> bool:
    """Set a Linear issue's state by display name (e.g., 'In Progress'). Returns True on success."""
    state_key = STATE_DISPLAY_TO_KEY.get(state_display_name)
    if not state_key:
        logger.error("Unknown state display name: %s", state_display_name)
        return False
    return set_issue_state(issue_id, team_id, state_key, api_key)


def update_sort_order(
    issue_id: str, sort_order: float, api_key: str | None = None
) -> bool:
    """Update a Linear issue's sort order. Returns True on success."""
    key = api_key or os.environ.get("LINEAR_API_KEY")
    if not key:
        logger.error("LINEAR_API_KEY not set")
        return False

    try:
        response = httpx.post(
            "https://api.linear.app/graphql",
            json={
                "query": UPDATE_SORT_ORDER_MUTATION,
                "variables": {"issueId": issue_id, "sortOrder": sort_order},
            },
            headers={"Authorization": key},
            timeout=10,
        )
        result = _parse_json(response)

        if "errors" in result:
            logger.error("Linear API error updating sort order: %s", result["errors"])  # pyright: ignore[reportAny]
            return False

        return _get_bool(_get_dict(_get_dict(result, "data"), "issueUpdate"), "success")

    except httpx.RequestError as e:
        logger.error("Failed to update sort order: %s", e)
        return False
