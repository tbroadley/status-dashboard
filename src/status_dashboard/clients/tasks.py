import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeAlias
import uuid

from status_dashboard import dates
from status_dashboard.task_store import Rows, TaskStore

JsonDict: TypeAlias = dict[str, object]
COL_ID, COL_CONTENT, COL_PROJECT, COL_DESCRIPTION = 0, 1, 2, 3
COL_DUE, COL_RECURRENCE, COL_ORDER, COL_DONE, COL_COMPLETED_AT = 4, 5, 6, 7, 8


@dataclass
class Task:
    id: str
    content: str
    is_completed: bool
    url: str = ""
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


def _cell(row: list[str], index: int) -> str:
    return row[index].strip()


def _is_true(value: str) -> bool:
    return value.strip().upper() == "TRUE"


def _extract_local_time(due: str) -> str | None:
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


def _row_to_task(row: list[str]) -> Task:
    due = _cell(row, COL_DUE)
    recurrence = _cell(row, COL_RECURRENCE)
    order = _cell(row, COL_ORDER)
    return Task(
        id=_cell(row, COL_ID),
        content=_cell(row, COL_CONTENT),
        is_completed=_is_true(_cell(row, COL_DONE)),
        day_order=int(order) if order.lstrip("-").isdigit() else 0,
        due_date=due[:10] or None,
        due_time=_extract_local_time(due),
        description=_cell(row, COL_DESCRIPTION),
        is_recurring=bool(recurrence),
        due_string=recurrence or None,
        project_id=_cell(row, COL_PROJECT) or None,
    )


def get_today_tasks(api_token: str | None = None) -> list[Task]:
    return get_tasks_for_date(dt.date.today(), api_token)


def get_tasks_for_date(
    target_date: dt.date, api_token: str | None = None
) -> list[Task]:
    del api_token
    rows = TaskStore().read().rows
    target = target_date.isoformat()
    is_today = target_date == dt.date.today()
    result: list[Task] = []
    for row in rows:
        if _is_true(_cell(row, COL_DONE)):
            continue
        due = _cell(row, COL_DUE)[:10]
        if not due or (due > target if is_today else due != target):
            continue
        result.append(_row_to_task(row))
    result.sort(key=lambda task: task.day_order)
    return result


def _edit_task(task_id: str, edit: Callable[[list[str]], None]) -> bool:
    def apply(rows: Rows) -> bool:
        for row in rows:
            if row[COL_ID] == task_id:
                edit(row)
                return True
        return False

    return TaskStore().update(apply)


def _update_cells(task_id: str, updates: dict[int, str]) -> bool:
    def edit(row: list[str]) -> None:
        for column, value in updates.items():
            row[column] = value

    return _edit_task(task_id, edit)


def _recurrence_base(due: str, now: dt.datetime | None = None) -> dt.datetime:
    now = now or dt.datetime.now()
    if not due:
        return now
    try:
        parsed = dt.datetime.fromisoformat(due.replace("Z", "+00:00"))
    except ValueError:
        return now
    local = parsed.astimezone().replace(tzinfo=None) if parsed.tzinfo else parsed
    return max(local, now)


def complete_task(task_id: str, api_token: str | None = None) -> bool:
    del api_token

    def edit(row: list[str]) -> None:
        recurrence = _cell(row, COL_RECURRENCE)
        if recurrence:
            following = dates.next_occurrence(
                recurrence, _recurrence_base(_cell(row, COL_DUE))
            )
            if following is not None:
                row[COL_DUE] = _iso(following)
                return
        row[COL_DONE] = "TRUE"
        row[COL_COMPLETED_AT] = _iso(dt.datetime.now())

    return _edit_task(task_id, edit)


def defer_task(task_id: str, api_token: str | None = None) -> bool:
    del api_token
    return _update_cells(task_id, {COL_DUE: _iso(dates.next_working_day())})


def create_task(
    content: str,
    due_string: str = "today",
    description: str = "",
    api_token: str | None = None,
) -> str | None:
    del api_token
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

    def edit(rows: Rows) -> bool:
        if not any(existing[COL_ID] == task_id for existing in rows):
            rows.append(row.copy())
        return True

    return task_id if TaskStore().update(edit) else None


def delete_task(task_id: str, api_token: str | None = None) -> bool:
    del api_token

    def edit(rows: Rows) -> bool:
        before = len(rows)
        rows[:] = [row for row in rows if row[COL_ID] != task_id]
        return len(rows) != before

    return TaskStore().update(edit)


def reopen_task(task_id: str, api_token: str | None = None) -> bool:
    del api_token
    return _update_cells(task_id, {COL_DONE: "FALSE", COL_COMPLETED_AT: ""})


def get_task(task_id: str, api_token: str | None = None) -> JsonDict | None:
    del api_token
    row = next((row for row in TaskStore().read().rows if row[COL_ID] == task_id), None)
    if row is None:
        return None
    task = _row_to_task(row)
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
    del api_token
    parsed = dates.parse_due_string(due_date) if due_date else dates.ParsedDue(due=None)
    return _update_cells(task_id, {COL_DUE: _iso(parsed.due)})


def reschedule_to_today(
    task_id: str,
    is_recurring: bool = False,
    due_string: str | None = None,
    api_token: str | None = None,
) -> bool:
    del api_token
    today = dt.date.today()
    updates: dict[int, str] = {COL_DUE: _iso(today)}
    if is_recurring and due_string:
        at, _ = dates.extract_time(due_string)
        if at:
            updates[COL_DUE] = _iso(dt.datetime.combine(today, at))
        updates[COL_RECURRENCE] = due_string
    return _update_cells(task_id, updates)


def update_day_orders(
    ids_to_orders: dict[str, int], api_token: str | None = None
) -> bool:
    del api_token
    if not ids_to_orders:
        return True

    def edit(rows: Rows) -> bool:
        found = False
        for row in rows:
            if row[COL_ID] in ids_to_orders:
                row[COL_ORDER] = str(ids_to_orders[row[COL_ID]])
                found = True
        return found

    return TaskStore().update(edit)


def get_projects(api_token: str | None = None) -> list[Project]:
    del api_token
    names = sorted(
        {
            _cell(row, COL_PROJECT)
            for row in TaskStore().read().rows
            if _cell(row, COL_PROJECT)
        }
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
    del api_token
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
    return _update_cells(task_id, updates) if updates else True
