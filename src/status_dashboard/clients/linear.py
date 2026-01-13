import json
import logging
import os
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

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


ISSUES_QUERY = """
query GetProjectIssues($projectName: String!) {
  projects(filter: { name: { containsIgnoreCase: $projectName } }) {
    nodes {
      issues {
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
mutation CreateIssue($teamId: String!, $title: String!, $stateId: String, $assigneeId: String) {
  issueCreate(input: { teamId: $teamId, title: $title, stateId: $stateId, assigneeId: $assigneeId }) {
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

GET_VIEWER_QUERY = """
query GetViewer {
  viewer {
    id
  }
}
"""

GET_TEAM_QUERY = """
query GetTeam($projectName: String!) {
  projects(filter: { name: { containsIgnoreCase: $projectName } }) {
    nodes {
      teams {
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

# Map state names to their types for lookup
STATE_NAME_MAP = {
    "backlog": "Backlog",
    "todo": "Todo",
    "in_progress": "In Progress",
    "in_review": "In Review",
    "done": "Done",
}


def _get_initials(name: str | None) -> str | None:
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
    """Get Linear issues for a specific project, sorted by status."""
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
        response.raise_for_status()
        data = response.json()
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

    issues = []
    projects = data.get("data", {}).get("projects", {}).get("nodes", [])
    for proj in projects:
        for issue in proj.get("issues", {}).get("nodes", []):
            assignee = issue.get("assignee")
            assignee_name = assignee.get("displayName") or assignee.get("name") if assignee else None

            issues.append(
                Issue(
                    id=issue["id"],
                    identifier=issue["identifier"],
                    title=issue["title"],
                    state=issue.get("state", {}).get("name", "Unknown"),
                    url=issue["url"],
                    team_id=issue.get("team", {}).get("id", ""),
                    assignee_initials=_get_initials(assignee_name),
                )
            )

    # Sort by status order
    issues.sort(key=lambda i: STATUS_ORDER.get(i.state, 99))

    return issues


def set_issue_state(issue_id: str, team_id: str, state_name: str, api_key: str | None = None) -> bool:
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
        data = response.json()

        if "errors" in data:
            logger.error("Linear API error getting workflow states: %s", data["errors"])
            return False

        states = data.get("data", {}).get("workflowStates", {}).get("nodes", [])

        # Find the target state
        state_id = None
        for state in states:
            if state["name"] == target_state:
                state_id = state["id"]
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
        result = response.json()

        if "errors" in result:
            logger.error("Linear API error updating issue: %s", result["errors"])
            return False

        return result.get("data", {}).get("issueUpdate", {}).get("success", False)

    except httpx.HTTPStatusError as e:
        logger.error("Failed to set issue state: %s - %s", e.response.status_code, e.response.text)
        return False
    except httpx.RequestError as e:
        logger.error("Failed to set issue state: %s", e)
        return False


def complete_issue(issue_id: str, team_id: str, api_key: str | None = None) -> bool:
    """Mark a Linear issue as Done. Returns True on success."""
    return set_issue_state(issue_id, team_id, "done", api_key)


def get_team_id(project_name: str | None = None, api_key: str | None = None) -> str | None:
    """Get the team ID for a project. Returns None on error."""
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
        response.raise_for_status()
        data = response.json()

        if "errors" in data:
            logger.error("Linear API error getting team: %s", data["errors"])
            return None

        projects = data.get("data", {}).get("projects", {}).get("nodes", [])
        if not projects:
            logger.error("Project '%s' not found", project)
            return None

        teams = projects[0].get("teams", {}).get("nodes", [])
        if not teams:
            logger.error("No teams found for project '%s'", project)
            return None

        return teams[0]["id"]

    except httpx.RequestError as e:
        logger.error("Failed to get team ID: %s", e)
        return None


def get_team_members(api_key: str | None = None) -> list[dict]:
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
        response.raise_for_status()
        data = response.json()

        if "errors" in data:
            logger.error("Linear API error getting team members: %s", data["errors"])
            return []

        users = data.get("data", {}).get("users", {}).get("nodes", [])
        return users

    except httpx.RequestError as e:
        logger.error("Failed to get team members: %s", e)
        return []


def create_issue(
    title: str,
    team_id: str,
    state_name: str | None = None,
    assignee_id: str | None = None,
    api_key: str | None = None,
) -> bool:
    """Create a new Linear issue. Returns True on success.

    state_name should be one of: backlog, todo, in_progress, in_review, done
    If not provided, uses the team's default state.
    """
    key = api_key or os.environ.get("LINEAR_API_KEY")
    if not key:
        logger.error("LINEAR_API_KEY not set")
        return False

    # Get state ID if state_name is provided
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
            data = response.json()

            if "errors" in data:
                logger.error("Linear API error getting workflow states: %s", data["errors"])
                return False

            states = data.get("data", {}).get("workflowStates", {}).get("nodes", [])
            for state in states:
                if state["name"] == target_state:
                    state_id = state["id"]
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

        response = httpx.post(
            "https://api.linear.app/graphql",
            json={
                "query": CREATE_ISSUE_MUTATION,
                "variables": variables,
            },
            headers={"Authorization": key},
            timeout=10,
        )
        result = response.json()

        if "errors" in result:
            logger.error("Linear API error creating issue: %s", result["errors"])
            return False

        return result.get("data", {}).get("issueCreate", {}).get("success", False)

    except httpx.HTTPStatusError as e:
        logger.error("Failed to create issue: %s - %s", e.response.status_code, e.response.text)
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
        response.raise_for_status()
        data = response.json()

        if "errors" in data:
            logger.error("Linear API error getting viewer: %s", data["errors"])
            return None

        return data.get("data", {}).get("viewer", {}).get("id")

    except httpx.RequestError as e:
        logger.error("Failed to get viewer ID: %s", e)
        return None


def assign_issue(issue_id: str, assignee_id: str | None, api_key: str | None = None) -> bool:
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
        result = response.json()

        if "errors" in result:
            logger.error("Linear API error assigning issue: %s", result["errors"])
            return False

        return result.get("data", {}).get("issueUpdate", {}).get("success", False)

    except httpx.RequestError as e:
        logger.error("Failed to assign issue: %s", e)
        return False
