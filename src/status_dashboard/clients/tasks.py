import datetime as dt
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeAlias
import uuid

from status_dashboard import dates
from status_dashboard.task_store import Rows, StoreError, shared_store

JsonDict: TypeAlias = dict[str, object]
Edit: TypeAlias = Callable[[Rows], bool]
"""Changes the rows in place; returns whether the task(s) it targets exist.

Edits run twice: on the rows the dashboard shows, so the change appears at
once, and on the latest stored rows when saved. They must not depend on
anything but their arguments and the rows.
"""
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


def _order(row: list[str]) -> int:
    order = _cell(row, COL_ORDER)
    return int(order) if order.lstrip("-").isdigit() else 0


def _row_to_task(row: list[str]) -> Task:
    due = _cell(row, COL_DUE)
    recurrence = _cell(row, COL_RECURRENCE)
    return Task(
        id=_cell(row, COL_ID),
        content=_cell(row, COL_CONTENT),
        is_completed=_is_true(_cell(row, COL_DONE)),
        day_order=_order(row),
        due_date=due[:10] or None,
        due_time=_extract_local_time(due),
        description=_cell(row, COL_DESCRIPTION),
        is_recurring=bool(recurrence),
        due_string=recurrence or None,
        project_id=_cell(row, COL_PROJECT) or None,
    )


def get_today_tasks(api_token: str | None = None) -> list[Task]:
    return get_tasks_for_date(dt.date.today(), api_token)


def load() -> Rows:
    """Every stored row, including done tasks. Shared: don't modify it."""
    return shared_store().read().rows


def saved_rows() -> Rows | None:
    """The newest rows known without asking S3, e.g. the copy from the last run.

    Shared: don't modify it. May be out of date; show it only until a read or
    save returns.
    """
    try:
        snapshot = shared_store().cached
    except StoreError:
        return None
    return snapshot.rows if snapshot else None


def warm() -> None:
    """Reconnect ahead of a likely request (see `TaskStore.warm`)."""
    shared_store().warm()


def tasks_due(rows: Rows, target_date: dt.date) -> list[Task]:
    """Open tasks in the view for `target_date`, in their saved order."""
    result = [
        _row_to_task(row)
        for row in rows
        if not _is_true(row[COL_DONE])
        and is_due_on(_cell(row, COL_DUE)[:10] or None, target_date)
    ]
    result.sort(key=lambda task: task.day_order)
    return result


def get_tasks_for_date(
    target_date: dt.date, api_token: str | None = None
) -> list[Task]:
    del api_token
    return tasks_due(load(), target_date)


def is_due_on(due_date: str | None, target_date: dt.date) -> bool:
    """Whether a task due on `due_date` belongs in the view for `target_date`.

    The view for today also shows overdue tasks.
    """
    if not due_date:
        return False
    target = target_date.isoformat()
    if target_date == dt.date.today():
        return due_date <= target
    return due_date == target


def _row_edit(task_id: str, change: Callable[[list[str]], None]) -> Edit:
    def edit(rows: Rows) -> bool:
        for row in rows:
            if row[COL_ID] == task_id:
                change(row)
                return True
        return False

    return edit


def _cells_edit(task_id: str, updates: dict[int, str]) -> Edit:
    def change(row: list[str]) -> None:
        for column, value in updates.items():
            row[column] = value

    return _row_edit(task_id, change)


def save(edits: list[Edit]) -> tuple[list[bool], Rows]:
    """Apply `edits` in order in one write; return each one's result and the rows.

    The rows are what the store holds afterwards, so callers can show them
    without reading again.
    """
    results = [False] * len(edits)

    def apply(rows: Rows) -> bool:
        for index, edit in enumerate(edits):
            results[index] = edit(rows)
        return any(results)

    store = shared_store()
    written = store.update(apply)
    snapshot = written or store.cached
    return results, snapshot.rows if snapshot else []


def _save_one(edit: Edit) -> bool:
    results, _ = save([edit])
    return results[0]


def revert(rows: Rows, edit: Edit) -> Edit | None:
    """An edit undoing what `edit` does to `rows`, or None if it changes nothing.

    Only the cells `edit` changes are put back, so later unrelated changes to
    the same task survive. Removed rows are re-added and added rows removed.
    """
    before = {row[COL_ID]: row for row in rows}
    after_rows = [row.copy() for row in rows]
    if not edit(after_rows):
        return None
    after = {row[COL_ID]: row for row in after_rows}
    removed = [row.copy() for task_id, row in before.items() if task_id not in after]
    added = [task_id for task_id in after if task_id not in before]
    changed = {
        task_id: {
            column: old
            for column, (old, new) in enumerate(zip(row, after[task_id]))
            if old != new
        }
        for task_id, row in before.items()
        if task_id in after and row != after[task_id]
    }
    if not (removed or added or changed):
        return None

    def undo(target: Rows) -> bool:
        present = {row[COL_ID] for row in target}
        target[:] = [row for row in target if row[COL_ID] not in added]
        target.extend(row.copy() for row in removed if row[COL_ID] not in present)
        for row in target:
            for column, value in changed.get(row[COL_ID], {}).items():
                row[column] = value
        return True

    return undo


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


def complete_edit(task_id: str, now: dt.datetime | None = None) -> Edit:
    """Close the task, or move a recurring one to its next occurrence."""
    now = now or dt.datetime.now()

    def change(row: list[str]) -> None:
        recurrence = _cell(row, COL_RECURRENCE)
        if recurrence:
            following = dates.next_occurrence(
                recurrence, _recurrence_base(_cell(row, COL_DUE), now)
            )
            if following is not None:
                row[COL_DUE] = _iso(following)
                return
        row[COL_DONE] = "TRUE"
        row[COL_COMPLETED_AT] = _iso(now)

    return _row_edit(task_id, change)


def complete_task(task_id: str, api_token: str | None = None) -> bool:
    del api_token
    return _save_one(complete_edit(task_id))


def defer_edit(task_id: str, today: dt.date | None = None) -> Edit:
    return _cells_edit(task_id, {COL_DUE: _iso(dates.next_working_day(today))})


def defer_task(task_id: str, api_token: str | None = None) -> bool:
    del api_token
    return _save_one(defer_edit(task_id))


def _new_row(
    task_id: str,
    content: str,
    due_string: str,
    description: str,
    order: int,
    now: dt.datetime | None,
) -> list[str]:
    parsed = dates.parse_due_string(due_string, now)
    return [
        task_id,
        content,
        "",
        description,
        _iso(parsed.due),
        parsed.recurrence or "",
        str(order),
        "FALSE",
        "",
    ]


def new_task(
    task_id: str,
    content: str,
    due_string: str = "today",
    description: str = "",
    *,
    day_order: int = 0,
    now: dt.datetime | None = None,
) -> Task:
    """The task `create_task` stores for the same arguments, without storing it."""
    return _row_to_task(
        _new_row(task_id, content, due_string, description, day_order, now)
    )


def create_edit(
    task_id: str,
    content: str,
    due_string: str = "today",
    description: str = "",
    *,
    day_orders: dict[str, int] | None = None,
    now: dt.datetime | None = None,
) -> Edit:
    """Add a task, optionally reordering the day it lands in at the same time."""
    orders = day_orders or {}
    row = _new_row(task_id, content, due_string, description, 0, now)

    def edit(rows: Rows) -> bool:
        ranks = _arrange(rows, _in_order(orders)) if orders else {}
        if not any(existing[COL_ID] == task_id for existing in rows):
            new_row = row.copy()
            new_row[COL_ORDER] = str(ranks.get(task_id, 0))
            rows.append(new_row)
        return True

    return edit


def create_task(
    content: str,
    due_string: str = "today",
    description: str = "",
    api_token: str | None = None,
    *,
    task_id: str | None = None,
    day_orders: dict[str, int] | None = None,
    now: dt.datetime | None = None,
) -> str | None:
    """Store a new task, optionally reordering the day in the same write."""
    del api_token
    task_id = task_id or str(uuid.uuid4())
    edit = create_edit(
        task_id, content, due_string, description, day_orders=day_orders, now=now
    )
    return task_id if _save_one(edit) else None


def delete_edit(task_id: str) -> Edit:
    def edit(rows: Rows) -> bool:
        before = len(rows)
        rows[:] = [row for row in rows if row[COL_ID] != task_id]
        return len(rows) != before

    return edit


def delete_task(task_id: str, api_token: str | None = None) -> bool:
    del api_token
    return _save_one(delete_edit(task_id))


def reopen_task(task_id: str, api_token: str | None = None) -> bool:
    del api_token
    return _save_one(_cells_edit(task_id, {COL_DONE: "FALSE", COL_COMPLETED_AT: ""}))


def get_task(task_id: str, api_token: str | None = None) -> JsonDict | None:
    del api_token
    row = next((row for row in load() if row[COL_ID] == task_id), None)
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
    """Set the due date from a stored ISO value (as `get_task` returns) or a due string."""
    del api_token
    if due_date and _is_iso(due_date):
        return _save_one(_cells_edit(task_id, {COL_DUE: due_date}))
    parsed = dates.parse_due_string(due_date) if due_date else dates.ParsedDue(due=None)
    return _save_one(_cells_edit(task_id, {COL_DUE: _iso(parsed.due)}))


def _is_iso(value: str) -> bool:
    try:
        _ = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def reschedule_to_today_edit(task_ids: list[str], today: dt.date | None = None) -> Edit:
    """Move the tasks to today, keeping the time of day a recurring rule sets."""
    today = today or dt.date.today()
    wanted = set(task_ids)

    def edit(rows: Rows) -> bool:
        found = False
        for row in rows:
            if row[COL_ID] not in wanted:
                continue
            found = True
            due: dt.date | dt.datetime = today
            if recurrence := _cell(row, COL_RECURRENCE):
                at, _ = dates.extract_time(recurrence)
                if at:
                    due = dt.datetime.combine(today, at)
            row[COL_DUE] = _iso(due)
        return found

    return edit


def reschedule_to_today(
    task_id: str,
    is_recurring: bool = False,
    due_string: str | None = None,
    api_token: str | None = None,
) -> bool:
    del api_token, is_recurring, due_string
    return _save_one(reschedule_to_today_edit([task_id]))


def arrange_edit(ids_to_orders: dict[str, int]) -> Edit:
    """Arrange the given tasks in ascending order of their values (see `_arrange`)."""

    def edit(rows: Rows) -> bool:
        if not any(row[COL_ID] in ids_to_orders for row in rows):
            return False
        _ = _arrange(rows, _in_order(ids_to_orders))
        return True

    return edit


def update_day_orders(
    ids_to_orders: dict[str, int], api_token: str | None = None
) -> bool:
    del api_token
    if not ids_to_orders:
        return True
    return _save_one(arrange_edit(ids_to_orders))


def _in_order(ids_to_orders: dict[str, int]) -> list[str]:
    return sorted(ids_to_orders, key=lambda task_id: ids_to_orders[task_id])


def _arrange(rows: Rows, sequence: list[str]) -> dict[str, int]:
    """Put the listed tasks in `sequence` order and renumber every row.

    The order column is one ranking across all tasks, not a position within a
    day: the listed tasks trade the ranks they already hold, and every other
    task keeps its place. So tasks hidden from the day being arranged, such as
    recurring tasks already completed until tomorrow, stay in the order they
    had among themselves. A listed ID with no row yet (a task being created)
    is ranked just before the listed task that follows it. Returns the new
    rank of every ID, including those without a row.
    """
    present = {row[COL_ID] for row in rows}
    listed = set(sequence)
    queue = deque(dict.fromkeys(sequence))
    existing_left = sum(task_id in present for task_id in queue)
    ranking: list[str] = []
    for row in sorted(rows, key=_order):  # stable: ties keep document order
        task_id = row[COL_ID]
        if task_id not in listed:
            ranking.append(task_id)
            continue
        # This rank goes to the next listed task, after any new ones before it.
        while queue:
            next_id = queue.popleft()
            ranking.append(next_id)
            if next_id in present:
                existing_left -= 1
                break
        if existing_left == 0:
            ranking.extend(queue)  # new tasks after the last listed one
            queue.clear()
    ranking.extend(queue)
    ranks = {task_id: rank for rank, task_id in enumerate(ranking)}
    for row in rows:
        row[COL_ORDER] = str(ranks[row[COL_ID]])
    return ranks


def projects_in(rows: Rows) -> list[Project]:
    names = sorted({_cell(row, COL_PROJECT) for row in rows if _cell(row, COL_PROJECT)})
    return [Project(id=name, name=name) for name in names]


def get_projects(api_token: str | None = None) -> list[Project]:
    del api_token
    return projects_in(load())


def update_edit(
    task_id: str,
    content: str | None = None,
    description: str | None = None,
    project_id: str | None = None,
    due_string: str | None = None,
    now: dt.datetime | None = None,
) -> Edit:
    updates: dict[int, str] = {}
    if content is not None:
        updates[COL_CONTENT] = content
    if description is not None:
        updates[COL_DESCRIPTION] = description
    if project_id is not None:
        updates[COL_PROJECT] = project_id
    if due_string is not None:
        parsed = dates.parse_due_string(due_string, now)
        updates[COL_DUE] = _iso(parsed.due)
        updates[COL_RECURRENCE] = parsed.recurrence or ""
    return _cells_edit(task_id, updates)


def update_task(
    task_id: str,
    content: str | None = None,
    description: str | None = None,
    project_id: str | None = None,
    due_string: str | None = None,
    api_token: str | None = None,
) -> bool:
    del api_token
    if all(value is None for value in (content, description, project_id, due_string)):
        return True
    return _save_one(update_edit(task_id, content, description, project_id, due_string))
