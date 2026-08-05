"""Google Sheets task client, driven through the `gws` CLI.

Drop-in replacement for the old `clients.todoist` — same dataclasses and
function names, so `app.py` only changes which module it imports.

Auth lives in `gws`, mirroring how `clients.github` shells out to `gh`. The
only configuration here is which spreadsheet to use.

Sheet layout (first row is a header, one task per row):

    A id | B content | C project | D description | E due
    F recurrence | G order | H done | I completed_at

`due` is an ISO date ("2026-08-06") or an ISO datetime with offset
("2026-08-05T10:00:00-07:00") when the task has a time. `recurrence` holds the
original rule text ("every workday at 10am"), advanced locally by
`dates.next_occurrence`, since a spreadsheet has no repeating rows.

Sheets addresses cells by position, not identity, so every mutation first
resolves a task ID to its row. Row numbers are never cached — a delete shifts
every row beneath it.
"""

import datetime as dt
import json
import logging
import os
import subprocess
import uuid
from dataclasses import dataclass
from typing import Any, TypeAlias, cast

from status_dashboard import dates

logger = logging.getLogger(__name__)

JsonDict: TypeAlias = dict[str, Any]  # pyright: ignore[reportExplicitAny]

SUBPROCESS_TIMEOUT = 30  # seconds
SHEET_NAME = "Tasks"
DATA_RANGE = f"{SHEET_NAME}!A2:I"
HEADER = [
    "id",
    "content",
    "project",
    "description",
    "due",
    "recurrence",
    "order",
    "done",
    "completed_at",
]

COL_ID, COL_CONTENT, COL_PROJECT, COL_DESCRIPTION = 0, 1, 2, 3
COL_DUE, COL_RECURRENCE, COL_ORDER, COL_DONE, COL_COMPLETED_AT = 4, 5, 6, 7, 8

_sheet_id_cache: int | None = None


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
    is_recurring: bool = False
    due_string: str | None = None
    project_id: str | None = None


@dataclass
class Project:
    id: str
    name: str


def _spreadsheet_id() -> str | None:
    return os.environ.get("TASKS_SPREADSHEET_ID")


def _gws(args: list[str], payload: JsonDict | None = None) -> JsonDict | None:
    """Run a `gws` command and return parsed JSON. Returns None on any failure."""
    command = ["gws", *args]
    if payload is not None:
        command += ["--json", json.dumps(payload)]

    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        logger.error("gws command timed out after %d seconds", SUBPROCESS_TIMEOUT)
        return None
    except FileNotFoundError:
        logger.error("gws CLI not found; task panel unavailable")
        return None

    if result.returncode != 0:
        logger.warning("gws failed: %s", result.stderr.strip()[:500])
        return None

    try:
        data = (
            cast(JsonDict, json.loads(result.stdout)) if result.stdout.strip() else None
        )
    except json.JSONDecodeError as e:
        logger.error("Failed to parse gws output: %s", e)
        return None

    if data and "error" in data:
        logger.warning("gws API error: %s", str(data["error"])[:500])
        return None
    return data


def _values_get(spreadsheet_id: str, range_: str) -> list[list[str]] | None:
    data = _gws(
        [
            "sheets",
            "spreadsheets",
            "values",
            "get",
            "--params",
            json.dumps({"spreadsheetId": spreadsheet_id, "range": range_}),
        ]
    )
    if data is None:
        return None
    return cast(list[list[str]], data.get("values", []))


def _get_sheet_id(spreadsheet_id: str) -> int | None:
    """Resolve the numeric sheet ID, needed for row deletion. Cached per process."""
    global _sheet_id_cache
    if _sheet_id_cache is not None:
        return _sheet_id_cache

    data = _gws(
        [
            "sheets",
            "spreadsheets",
            "get",
            "--params",
            json.dumps({"spreadsheetId": spreadsheet_id}),
        ]
    )
    if data is None:
        return None

    for sheet in cast(list[JsonDict], data.get("sheets", [])):
        properties = cast(JsonDict, sheet.get("properties", {}))
        if properties.get("title") == SHEET_NAME:
            _sheet_id_cache = cast(int, properties["sheetId"])
            return _sheet_id_cache

    logger.error("No sheet named %r in spreadsheet", SHEET_NAME)
    return None


def _cell(row: list[str], index: int) -> str:
    """Read a cell, tolerating short rows (Sheets omits trailing empties)."""
    return row[index].strip() if index < len(row) else ""


def _is_true(value: str) -> bool:
    return value.strip().upper() == "TRUE"


def _extract_local_time(due: str) -> str | None:
    """Return 'HH:MM' in local time, or None for all-day dates."""
    if "T" not in due:
        return None
    try:
        parsed = dt.datetime.fromisoformat(due.replace("Z", "+00:00"))
    except ValueError:
        return None
    local = parsed.astimezone() if parsed.tzinfo else parsed
    return local.strftime("%H:%M")


def _iso(due: dt.datetime | dt.date | None) -> str:
    if due is None:
        return ""
    if isinstance(due, dt.datetime):
        aware = due.astimezone() if due.tzinfo is None else due
        return aware.isoformat(timespec="seconds")
    return due.isoformat()


def spreadsheet_url() -> str:
    """Link to the spreadsheet itself, for tasks whose row isn't known yet."""
    spreadsheet_id = _spreadsheet_id()
    return (
        f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
        if spreadsheet_id
        else ""
    )


def _task_url(spreadsheet_id: str, row_number: int) -> str:
    gid = _sheet_id_cache if _sheet_id_cache is not None else 0
    return (
        f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"
        f"/edit#gid={gid}&range=A{row_number}"
    )


def _row_to_task(row: list[str], row_number: int, spreadsheet_id: str) -> Task:
    due = _cell(row, COL_DUE)
    recurrence = _cell(row, COL_RECURRENCE)
    order = _cell(row, COL_ORDER)

    return Task(
        id=_cell(row, COL_ID),
        content=_cell(row, COL_CONTENT),
        is_completed=_is_true(_cell(row, COL_DONE)),
        url=_task_url(spreadsheet_id, row_number),
        day_order=int(order) if order.lstrip("-").isdigit() else 0,
        due_date=due[:10] or None,
        due_time=_extract_local_time(due),
        description=_cell(row, COL_DESCRIPTION),
        is_recurring=bool(recurrence),
        due_string=recurrence or None,
        project_id=_cell(row, COL_PROJECT) or None,
    )


def _load_rows(spreadsheet_id: str) -> list[tuple[int, list[str]]] | None:
    """Return (row_number, row) pairs for every populated data row."""
    values = _values_get(spreadsheet_id, DATA_RANGE)
    if values is None:
        return None
    # Data starts at spreadsheet row 2, so index 0 is row 2.
    return [(index + 2, row) for index, row in enumerate(values) if _cell(row, COL_ID)]


def _find_row(spreadsheet_id: str, task_id: str) -> tuple[int, list[str]] | None:
    """Locate a task's current row. Always re-read; deletes shift row numbers."""
    rows = _load_rows(spreadsheet_id)
    if rows is None:
        return None
    for row_number, row in rows:
        if _cell(row, COL_ID) == task_id:
            return row_number, row
    logger.warning("Task %s not found in spreadsheet", task_id)
    return None


def get_today_tasks(api_token: str | None = None) -> list[Task]:
    """Get tasks due today, sorted by order. Includes overdue tasks."""
    return get_tasks_for_date(dt.date.today(), api_token)


def get_tasks_for_date(
    target_date: dt.date, api_token: str | None = None
) -> list[Task]:
    """Get incomplete tasks due on `target_date`, sorted by order.

    For today, also includes anything overdue; for other dates, exact matches only.
    """
    del api_token  # auth is handled by the gws CLI
    spreadsheet_id = _spreadsheet_id()
    if not spreadsheet_id:
        logger.warning("TASKS_SPREADSHEET_ID not set, skipping tasks")
        return []

    rows = _load_rows(spreadsheet_id)
    if rows is None:
        return []

    target = target_date.isoformat()
    is_today = target_date == dt.date.today()

    tasks: list[Task] = []
    for row_number, row in rows:
        if _is_true(_cell(row, COL_DONE)):
            continue
        due = _cell(row, COL_DUE)[:10]
        if not due:
            continue
        if due > target if is_today else due != target:
            continue
        tasks.append(_row_to_task(row, row_number, spreadsheet_id))

    tasks.sort(key=lambda t: t.day_order)
    return tasks


def _update_cells(
    spreadsheet_id: str, row_number: int, updates: dict[int, str]
) -> bool:
    """Write individual cells in one row via a batched multi-range update."""
    data = [
        {
            "range": f"{SHEET_NAME}!{chr(ord('A') + column)}{row_number}",
            "values": [[value]],
        }
        for column, value in sorted(updates.items())
    ]
    return (
        _gws(
            [
                "sheets",
                "spreadsheets",
                "values",
                "batchUpdate",
                "--params",
                json.dumps({"spreadsheetId": spreadsheet_id}),
            ],
            {"valueInputOption": "RAW", "data": data},
        )
        is not None
    )


def _recurrence_base(due: str, now: dt.datetime | None = None) -> dt.datetime:
    """The moment a recurrence should advance from when a task is completed.

    Advancing from `now` alone is wrong: completing a task that isn't due until
    tomorrow would recompute the same tomorrow and never move. Advancing from
    the due date alone is also wrong: a task overdue by a week would land on
    another past date. Take whichever is later.
    """
    now = now or dt.datetime.now()
    if not due:
        return now

    try:
        parsed = dt.datetime.fromisoformat(due.replace("Z", "+00:00"))
    except ValueError:
        return now

    # Compare naive-to-naive; the sheet stores local times.
    local = parsed.astimezone().replace(tzinfo=None) if parsed.tzinfo else parsed
    return max(local, now)


def complete_task(task_id: str, api_token: str | None = None) -> bool:
    """Complete a task. Recurring tasks roll forward instead of closing."""
    del api_token
    spreadsheet_id = _spreadsheet_id()
    if not spreadsheet_id:
        logger.error("TASKS_SPREADSHEET_ID not set")
        return False

    found = _find_row(spreadsheet_id, task_id)
    if found is None:
        return False
    row_number, row = found

    recurrence = _cell(row, COL_RECURRENCE)
    if recurrence:
        following = dates.next_occurrence(
            recurrence, _recurrence_base(_cell(row, COL_DUE))
        )
        if following is not None:
            return _update_cells(spreadsheet_id, row_number, {COL_DUE: _iso(following)})
        logger.warning(
            "Could not advance recurrence %r; completing task instead", recurrence
        )

    return _update_cells(
        spreadsheet_id,
        row_number,
        {COL_DONE: "TRUE", COL_COMPLETED_AT: _iso(dt.datetime.now())},
    )


def defer_task(task_id: str, api_token: str | None = None) -> bool:
    """Defer a task to the next working day."""
    del api_token
    spreadsheet_id = _spreadsheet_id()
    if not spreadsheet_id:
        logger.error("TASKS_SPREADSHEET_ID not set")
        return False

    found = _find_row(spreadsheet_id, task_id)
    if found is None:
        return False
    return _update_cells(
        spreadsheet_id, found[0], {COL_DUE: _iso(dates.next_working_day())}
    )


def create_task(
    content: str,
    due_string: str = "today",
    description: str = "",
    api_token: str | None = None,
) -> str | None:
    """Create a task. Returns the new task ID, or None on failure."""
    del api_token
    spreadsheet_id = _spreadsheet_id()
    if not spreadsheet_id:
        logger.error("TASKS_SPREADSHEET_ID not set")
        return None

    parsed = dates.parse_due_string(due_string)
    task_id = str(uuid.uuid4())
    row = [
        task_id,
        content,
        "",
        description,
        _iso(parsed.due),
        parsed.recurrence or "",
        "0",
        "FALSE",
        "",
    ]

    appended = _gws(
        [
            "sheets",
            "spreadsheets",
            "values",
            "append",
            "--params",
            json.dumps(
                {
                    "spreadsheetId": spreadsheet_id,
                    "range": f"{SHEET_NAME}!A:I",
                    "valueInputOption": "RAW",
                    "insertDataOption": "INSERT_ROWS",
                }
            ),
        ],
        {"values": [row]},
    )
    return task_id if appended is not None else None


def delete_task(task_id: str, api_token: str | None = None) -> bool:
    """Delete a task's row."""
    del api_token
    spreadsheet_id = _spreadsheet_id()
    if not spreadsheet_id:
        logger.error("TASKS_SPREADSHEET_ID not set")
        return False

    found = _find_row(spreadsheet_id, task_id)
    if found is None:
        return False

    sheet_id = _get_sheet_id(spreadsheet_id)
    if sheet_id is None:
        return False

    # deleteDimension is 0-indexed and end-exclusive; row 2 is index 1.
    start = found[0] - 1
    return (
        _gws(
            [
                "sheets",
                "spreadsheets",
                "batchUpdate",
                "--params",
                json.dumps({"spreadsheetId": spreadsheet_id}),
            ],
            {
                "requests": [
                    {
                        "deleteDimension": {
                            "range": {
                                "sheetId": sheet_id,
                                "dimension": "ROWS",
                                "startIndex": start,
                                "endIndex": start + 1,
                            }
                        }
                    }
                ]
            },
        )
        is not None
    )


def reopen_task(task_id: str, api_token: str | None = None) -> bool:
    """Reopen a completed task."""
    del api_token
    spreadsheet_id = _spreadsheet_id()
    if not spreadsheet_id:
        logger.error("TASKS_SPREADSHEET_ID not set")
        return False

    found = _find_row(spreadsheet_id, task_id)
    if found is None:
        return False
    return _update_cells(
        spreadsheet_id, found[0], {COL_DONE: "FALSE", COL_COMPLETED_AT: ""}
    )


def get_task(task_id: str, api_token: str | None = None) -> JsonDict | None:
    """Get a task by ID, shaped like the Todoist payload `app.py` expects."""
    del api_token
    spreadsheet_id = _spreadsheet_id()
    if not spreadsheet_id:
        logger.error("TASKS_SPREADSHEET_ID not set")
        return None

    found = _find_row(spreadsheet_id, task_id)
    if found is None:
        return None
    row_number, row = found
    task = _row_to_task(row, row_number, spreadsheet_id)

    due: JsonDict | None = None
    if raw_due := _cell(row, COL_DUE):
        due = {
            "date": raw_due,
            "string": task.due_string or raw_due[:10],
            "is_recurring": task.is_recurring,
        }

    return {
        "id": task.id,
        "content": task.content,
        "description": task.description,
        "project_id": task.project_id,
        "due": due,
    }


def set_due_date(
    task_id: str, due_date: str | None, api_token: str | None = None
) -> bool:
    """Set a task's due date. Pass None to clear it."""
    del api_token
    spreadsheet_id = _spreadsheet_id()
    if not spreadsheet_id:
        logger.error("TASKS_SPREADSHEET_ID not set")
        return False

    found = _find_row(spreadsheet_id, task_id)
    if found is None:
        return False

    parsed = dates.parse_due_string(due_date) if due_date else dates.ParsedDue(due=None)
    return _update_cells(spreadsheet_id, found[0], {COL_DUE: _iso(parsed.due)})


def reschedule_to_today(
    task_id: str,
    is_recurring: bool = False,
    due_string: str | None = None,
    api_token: str | None = None,
) -> bool:
    """Reschedule a task to today, preserving any recurrence rule."""
    del api_token
    spreadsheet_id = _spreadsheet_id()
    if not spreadsheet_id:
        logger.error("TASKS_SPREADSHEET_ID not set")
        return False

    found = _find_row(spreadsheet_id, task_id)
    if found is None:
        return False

    today = dt.date.today()
    updates: dict[int, str] = {COL_DUE: _iso(today)}

    # Keep the time-of-day when a recurring rule specifies one.
    if is_recurring and due_string:
        at, _ = dates.extract_time(due_string)
        if at:
            updates[COL_DUE] = _iso(dt.datetime.combine(today, at))
        updates[COL_RECURRENCE] = due_string

    return _update_cells(spreadsheet_id, found[0], updates)


def update_day_orders(
    ids_to_orders: dict[str, int], api_token: str | None = None
) -> bool:
    """Update the order column for several tasks in a single batched write."""
    del api_token
    spreadsheet_id = _spreadsheet_id()
    if not spreadsheet_id:
        logger.error("TASKS_SPREADSHEET_ID not set")
        return False
    if not ids_to_orders:
        return True

    rows = _load_rows(spreadsheet_id)
    if rows is None:
        return False

    row_numbers = {_cell(row, COL_ID): number for number, row in rows}
    column = chr(ord("A") + COL_ORDER)
    data = [
        {
            "range": f"{SHEET_NAME}!{column}{row_numbers[task_id]}",
            "values": [[str(order)]],
        }
        for task_id, order in ids_to_orders.items()
        if task_id in row_numbers
    ]
    if missing := len(ids_to_orders) - len(data):
        logger.warning("Skipped %d unknown task(s) while reordering", missing)
    if not data:
        return False

    return (
        _gws(
            [
                "sheets",
                "spreadsheets",
                "values",
                "batchUpdate",
                "--params",
                json.dumps({"spreadsheetId": spreadsheet_id}),
            ],
            {"valueInputOption": "RAW", "data": data},
        )
        is not None
    )


def get_projects(api_token: str | None = None) -> list[Project]:
    """Get the distinct project names in use. Empty list on error."""
    del api_token
    spreadsheet_id = _spreadsheet_id()
    if not spreadsheet_id:
        logger.error("TASKS_SPREADSHEET_ID not set")
        return []

    rows = _load_rows(spreadsheet_id)
    if rows is None:
        return []

    names = sorted(
        {_cell(row, COL_PROJECT) for _, row in rows if _cell(row, COL_PROJECT)}
    )
    return [Project(id=name, name=name) for name in names]


def update_task(
    task_id: str,
    content: str | None = None,
    description: str | None = None,
    project_id: str | None = None,
    due_string: str | None = None,
    api_token: str | None = None,
) -> bool:
    """Update a task. Only provided fields are changed."""
    del api_token
    spreadsheet_id = _spreadsheet_id()
    if not spreadsheet_id:
        logger.error("TASKS_SPREADSHEET_ID not set")
        return False

    updates: dict[int, str] = {}
    if content is not None:
        updates[COL_CONTENT] = content
    if description is not None:
        updates[COL_DESCRIPTION] = description
    if project_id is not None:
        updates[COL_PROJECT] = project_id
    if due_string is not None:
        parsed = dates.parse_due_string(due_string)
        updates[COL_DUE] = _iso(parsed.due)
        updates[COL_RECURRENCE] = parsed.recurrence or ""

    if not updates:
        return True

    found = _find_row(spreadsheet_id, task_id)
    if found is None:
        return False
    return _update_cells(spreadsheet_id, found[0], updates)
