import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from typing import Any
from datetime import date, datetime, timedelta

import httpx

logger = logging.getLogger(__name__)


@dataclass
class Task:
    id: str
    content: str
    is_completed: bool
    url: str
    day_order: int = 0
    due_date: str | None = None
    due_time: str | None = None
    comment_count: int = 0
    description: str = ""


def _get_token() -> str | None:
    return os.environ.get("TODOIST_API_TOKEN")


def _slugify(text: str) -> str:
    """Convert text to URL slug."""
    # Remove markdown links, keep just the text
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    # Lowercase and replace non-alphanumeric with hyphens
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower())
    # Remove leading/trailing hyphens and collapse multiple hyphens
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug[:50]  # Limit length


def _extract_local_time(due_date_str: str) -> str | None:
    """Extract time in local timezone from Todoist due date string.

    Todoist returns dates in one of two formats:
    - "2024-01-15" for all-day tasks (no time)
    - "2024-01-15T14:30:00Z" for tasks with a specific time (always UTC)

    Returns time as "HH:MM" in system timezone, or None if no time set.
    """
    if "T" not in due_date_str:
        return None

    utc_dt = datetime.fromisoformat(due_date_str.replace("Z", "+00:00"))
    local_dt = utc_dt.astimezone()
    return local_dt.strftime("%H:%M")


def get_today_tasks(api_token: str | None = None) -> list[Task]:
    """Get Todoist tasks due today, sorted by day_order (Today view order)."""
    return get_tasks_for_date(date.today(), api_token)


def get_tasks_for_date(target_date: date, api_token: str | None = None) -> list[Task]:
    """Get Todoist tasks due on the target date, sorted by day_order.

    For today's date, also includes overdue tasks (due before today).
    For future dates, only returns tasks due exactly on that date.
    """
    token = api_token or _get_token()
    if not token:
        logger.warning("TODOIST_API_TOKEN not set, skipping Todoist tasks")
        return []

    try:
        response = httpx.post(
            "https://api.todoist.com/api/v1/sync",
            headers={"Authorization": f"Bearer {token}"},
            data={
                "sync_token": "*",
                "resource_types": '["items"]',
            },
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
    except httpx.TimeoutException:
        logger.error("Todoist API request timed out")
        return []
    except httpx.HTTPStatusError as e:
        logger.error("Todoist API returned error: %s", e.response.status_code)
        return []
    except httpx.RequestError as e:
        logger.error("Todoist API request failed: %s", e)
        return []
    except json.JSONDecodeError as e:
        logger.error("Failed to parse Todoist response: %s", e)
        return []

    target_date_str = target_date.isoformat()
    today_str = date.today().isoformat()
    is_today = target_date_str == today_str
    tasks = []

    for item in data.get("items", []):
        if item.get("checked") or item.get("is_deleted"):
            continue

        due = item.get("due")
        if not due:
            continue

        due_date_raw = due.get("date", "")
        due_date = due_date_raw[:10]
        if is_today:
            if due_date > target_date_str:
                continue
        else:
            if due_date != target_date_str:
                continue

        due_time = _extract_local_time(due_date_raw)

        v2_id = item.get("v2_id", item["id"])
        slug = _slugify(item["content"])
        url = f"https://app.todoist.com/app/task/{slug}-{v2_id}"

        tasks.append(
            Task(
                id=item["id"],
                content=item["content"],
                is_completed=item.get("checked", False),
                url=url,
                day_order=item.get("day_order", 0),
                due_date=due_date,
                due_time=due_time,
                comment_count=item.get("comment_count", 0),
                description=item.get("description", ""),
            )
        )

    tasks.sort(key=lambda t: t.day_order)

    return tasks


def complete_task(task_id: str, api_token: str | None = None) -> bool:
    """Mark a Todoist task as complete. Returns True on success."""
    token = api_token or _get_token()
    if not token:
        logger.error("TODOIST_API_TOKEN not set")
        return False

    try:
        response = httpx.post(
            f"https://api.todoist.com/api/v1/tasks/{task_id}/close",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        response.raise_for_status()
        return True
    except httpx.HTTPStatusError as e:
        logger.error("Failed to complete task: %s", e.response.status_code)
        return False
    except httpx.RequestError as e:
        logger.error("Failed to complete task: %s", e)
        return False


def _next_working_day(from_date: date | None = None) -> date:
    """Get the next working day (Monday-Friday) after the given date."""
    if from_date is None:
        from_date = date.today()

    next_day = from_date + timedelta(days=1)
    # weekday(): Monday=0, Sunday=6
    while next_day.weekday() >= 5:  # Saturday=5, Sunday=6
        next_day += timedelta(days=1)
    return next_day


def defer_task(task_id: str, api_token: str | None = None) -> bool:
    """Defer a Todoist task to the next working day. Returns True on success."""
    token = api_token or _get_token()
    if not token:
        logger.error("TODOIST_API_TOKEN not set")
        return False

    next_day = _next_working_day()

    try:
        response = httpx.post(
            f"https://api.todoist.com/api/v1/tasks/{task_id}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={"due_date": next_day.isoformat()},
            timeout=10,
        )
        response.raise_for_status()
        return True
    except httpx.HTTPStatusError as e:
        logger.error("Failed to defer task: %s", e.response.status_code)
        return False
    except httpx.RequestError as e:
        logger.error("Failed to defer task: %s", e)
        return False


def create_task(
    content: str, due_string: str = "today", api_token: str | None = None
) -> str | None:
    """Create a new Todoist task. Returns the created task ID on success, None on failure."""
    token = api_token or _get_token()
    if not token:
        logger.error("TODOIST_API_TOKEN not set")
        return None

    try:
        response = httpx.post(
            "https://api.todoist.com/api/v1/tasks",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={
                "content": content,
                "due_string": due_string,
            },
            timeout=10,
        )
        response.raise_for_status()
        return response.json().get("id")
    except httpx.HTTPStatusError as e:
        logger.error(
            "Failed to create task: %s - %s", e.response.status_code, e.response.text
        )
        return None
    except httpx.RequestError as e:
        logger.error("Failed to create task: %s", e)
        return None


def delete_task(task_id: str, api_token: str | None = None) -> bool:
    """Delete a Todoist task. Returns True on success."""
    token = api_token or _get_token()
    if not token:
        logger.error("TODOIST_API_TOKEN not set")
        return False

    try:
        response = httpx.delete(
            f"https://api.todoist.com/api/v1/tasks/{task_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        response.raise_for_status()
        return True
    except httpx.HTTPStatusError as e:
        logger.error("Failed to delete task: %s", e.response.status_code)
        return False
    except httpx.RequestError as e:
        logger.error("Failed to delete task: %s", e)
        return False


def reopen_task(task_id: str, api_token: str | None = None) -> bool:
    """Reopen a completed Todoist task. Returns True on success."""
    token = api_token or _get_token()
    if not token:
        logger.error("TODOIST_API_TOKEN not set")
        return False

    try:
        response = httpx.post(
            f"https://api.todoist.com/api/v1/tasks/{task_id}/reopen",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        response.raise_for_status()
        return True
    except httpx.HTTPStatusError as e:
        logger.error("Failed to reopen task: %s", e.response.status_code)
        return False
    except httpx.RequestError as e:
        logger.error("Failed to reopen task: %s", e)
        return False


def get_task(task_id: str, api_token: str | None = None) -> dict[str, Any] | None:
    """Get a Todoist task by ID. Returns task dict or None on error."""
    token = api_token or _get_token()
    if not token:
        logger.error("TODOIST_API_TOKEN not set")
        return None

    try:
        response = httpx.get(
            f"https://api.todoist.com/api/v1/tasks/{task_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as e:
        logger.error("Failed to get task: %s", e.response.status_code)
        return None
    except httpx.RequestError as e:
        logger.error("Failed to get task: %s", e)
        return None


def set_due_date(
    task_id: str, due_date: str | None, api_token: str | None = None
) -> bool:
    """Set a task's due date. Pass None to clear the due date. Returns True on success."""
    token = api_token or _get_token()
    if not token:
        logger.error("TODOIST_API_TOKEN not set")
        return False

    try:
        payload = {"due_date": due_date} if due_date else {"due_string": "no date"}
        response = httpx.post(
            f"https://api.todoist.com/api/v1/tasks/{task_id}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=10,
        )
        response.raise_for_status()
        return True
    except httpx.HTTPStatusError as e:
        logger.error("Failed to set due date: %s", e.response.status_code)
        return False
    except httpx.RequestError as e:
        logger.error("Failed to set due date: %s", e)
        return False


def reschedule_to_today(task_id: str, api_token: str | None = None) -> bool:
    """Reschedule a task to today. Returns True on success."""
    token = api_token or _get_token()
    if not token:
        logger.error("TODOIST_API_TOKEN not set")
        return False

    today = date.today().isoformat()

    try:
        response = httpx.post(
            f"https://api.todoist.com/api/v1/tasks/{task_id}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={"due_date": today},
            timeout=10,
        )
        response.raise_for_status()
        return True
    except httpx.HTTPStatusError as e:
        logger.error("Failed to reschedule task: %s", e.response.status_code)
        return False
    except httpx.RequestError as e:
        logger.error("Failed to reschedule task: %s", e)
        return False


def update_day_orders(
    ids_to_orders: dict[str, int], api_token: str | None = None
) -> bool:
    """Update day_order for multiple tasks in the Today view. Returns True on success."""
    token = api_token or _get_token()
    if not token:
        logger.error("TODOIST_API_TOKEN not set")
        return False

    command = {
        "type": "item_update_day_orders",
        "uuid": str(uuid.uuid4()),
        "args": {"ids_to_orders": ids_to_orders},
    }

    try:
        response = httpx.post(
            "https://api.todoist.com/api/v1/sync",
            headers={"Authorization": f"Bearer {token}"},
            data={"commands": json.dumps([command])},
            timeout=10,
        )
        response.raise_for_status()
        result = response.json()
        sync_status = result.get("sync_status", {})
        if sync_status.get(command["uuid"]) == "ok":
            return True
        logger.error("Todoist sync command failed: %s", sync_status)
        return False
    except httpx.HTTPStatusError as e:
        logger.error("Failed to update day orders: %s", e.response.status_code)
        return False
    except httpx.RequestError as e:
        logger.error("Failed to update day orders: %s", e)
        return False


@dataclass
class Project:
    id: str
    name: str


def get_projects(api_token: str | None = None) -> list[Project]:
    """Get all Todoist projects. Returns empty list on error."""
    token = api_token or _get_token()
    if not token:
        logger.error("TODOIST_API_TOKEN not set")
        return []

    try:
        response = httpx.get(
            "https://api.todoist.com/api/v1/projects",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        _ = response.raise_for_status()
        data = response.json()
        projects = data.get("results", data) if isinstance(data, dict) else data
        return [Project(id=p["id"], name=p["name"]) for p in projects]
    except httpx.HTTPStatusError as e:
        logger.error("Failed to get projects: %s", e.response.status_code)
        return []
    except httpx.RequestError as e:
        logger.error("Failed to get projects: %s", e)
        return []


def update_task(
    task_id: str,
    content: str | None = None,
    description: str | None = None,
    project_id: str | None = None,
    due_string: str | None = None,
    api_token: str | None = None,
) -> bool:
    """Update a Todoist task. Only provided fields will be updated. Returns True on success."""
    token = api_token or _get_token()
    if not token:
        logger.error("TODOIST_API_TOKEN not set")
        return False

    payload: dict[str, Any] = {}
    if content is not None:
        payload["content"] = content
    if description is not None:
        payload["description"] = description
    if project_id is not None:
        payload["project_id"] = project_id
    if due_string is not None:
        payload["due_string"] = due_string

    if not payload:
        return True

    try:
        response = httpx.post(
            f"https://api.todoist.com/api/v1/tasks/{task_id}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=10,
        )
        _ = response.raise_for_status()
        return True
    except httpx.HTTPStatusError as e:
        logger.error("Failed to update task: %s", e.response.status_code)
        return False
    except httpx.RequestError as e:
        logger.error("Failed to update task: %s", e)
        return False
