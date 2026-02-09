"""SQLite database operations for weekly goals."""

import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import cast


@dataclass
class Goal:
    id: str
    content: str
    week_start: date
    is_completed: bool
    is_abandoned: bool
    completed_at: datetime | None
    abandoned_at: datetime | None
    created_at: datetime
    sort_order: int
    h2_2025_estimate: float | None = None
    predicted_time: float | None = None
    actual_time: float | None = None


@dataclass
class WeekMetrics:
    week_start: date
    h2_2025_estimate: float | None
    predicted_time: float | None
    actual_time: float | None
    created_at: datetime
    updated_at: datetime


def _get_db_path() -> Path:
    """Get the database path, following XDG conventions."""
    xdg_data = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg_data) if xdg_data else Path.home() / ".local" / "share"
    db_dir = base / "status-dashboard"
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir / "goals.db"


def _get_connection() -> sqlite3.Connection:
    """Get a database connection and ensure schema exists."""
    conn = sqlite3.connect(_get_db_path())
    conn.row_factory = sqlite3.Row
    _ = conn.execute("""
        CREATE TABLE IF NOT EXISTS goals (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            week_start DATE NOT NULL,
            is_completed INTEGER DEFAULT 0,
            is_abandoned INTEGER DEFAULT 0,
            completed_at TEXT,
            abandoned_at TEXT,
            created_at TEXT NOT NULL,
            sort_order INTEGER DEFAULT 0,
            h2_2025_estimate REAL,
            predicted_time REAL,
            actual_time REAL
        )
    """)
    _ = conn.execute("CREATE INDEX IF NOT EXISTS idx_goals_week ON goals(week_start)")
    # Migrations for existing databases
    for col, col_type, default in [
        ("is_abandoned", "INTEGER", "0"),
        ("abandoned_at", "TEXT", None),
        ("h2_2025_estimate", "REAL", None),
        ("predicted_time", "REAL", None),
        ("actual_time", "REAL", None),
    ]:
        try:
            default_clause = f" DEFAULT {default}" if default else ""
            _ = conn.execute(
                f"ALTER TABLE goals ADD COLUMN {col} {col_type}{default_clause}"
            )
        except sqlite3.OperationalError:
            pass  # Column already exists
    _ = conn.execute("""
        CREATE TABLE IF NOT EXISTS week_metrics (
            week_start DATE PRIMARY KEY,
            h2_2025_estimate REAL,
            predicted_time REAL,
            actual_time REAL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def get_week_start(d: date) -> date:
    """Get the Monday of the week containing the given date."""
    return d - timedelta(days=d.weekday())


def _row_to_goal(row: sqlite3.Row) -> Goal:
    """Convert a sqlite3.Row to a Goal dataclass."""
    completed_at_raw = cast(str | None, row["completed_at"])
    abandoned_at_raw = cast(str | None, row["abandoned_at"])
    return Goal(
        id=cast(str, row["id"]),
        content=cast(str, row["content"]),
        week_start=date.fromisoformat(cast(str, row["week_start"])),
        is_completed=bool(cast(int, row["is_completed"])),
        is_abandoned=bool(cast(int, row["is_abandoned"])),
        completed_at=(
            datetime.fromisoformat(completed_at_raw) if completed_at_raw else None
        ),
        abandoned_at=(
            datetime.fromisoformat(abandoned_at_raw) if abandoned_at_raw else None
        ),
        created_at=datetime.fromisoformat(cast(str, row["created_at"])),
        sort_order=cast(int, row["sort_order"]),
        h2_2025_estimate=cast(float | None, row["h2_2025_estimate"]),
        predicted_time=cast(float | None, row["predicted_time"]),
        actual_time=cast(float | None, row["actual_time"]),
    )


def get_goals_for_week(week_start: date) -> list[Goal]:
    """Get all goals for a given week (by week_start Monday)."""
    conn = _get_connection()
    try:
        cursor = conn.execute(
            """
            SELECT id, content, week_start, is_completed, is_abandoned,
                   completed_at, abandoned_at, created_at, sort_order,
                   h2_2025_estimate, predicted_time, actual_time
            FROM goals
            WHERE week_start = ?
            ORDER BY is_abandoned, sort_order, created_at
            """,
            (week_start.isoformat(),),
        )
        rows: list[sqlite3.Row] = cursor.fetchall()
        return [_row_to_goal(row) for row in rows]
    finally:
        conn.close()


def create_goal(content: str, week_start: date) -> str:
    """Create a new goal and return its ID."""
    conn = _get_connection()
    try:
        goal_id = str(uuid.uuid4())
        now = datetime.now().isoformat()
        cursor = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM goals WHERE week_start = ?",
            (week_start.isoformat(),),
        )
        fetch_row = cast(sqlite3.Row | None, cursor.fetchone())
        sort_order = cast(int, fetch_row[0]) if fetch_row else 0
        _ = conn.execute(
            """
            INSERT INTO goals (id, content, week_start, is_completed, created_at, sort_order)
            VALUES (?, ?, ?, 0, ?, ?)
            """,
            (goal_id, content, week_start.isoformat(), now, sort_order),
        )
        conn.commit()
        return goal_id
    finally:
        conn.close()


def complete_goal(goal_id: str) -> bool:
    """Mark a goal as completed. Returns True on success."""
    conn = _get_connection()
    try:
        now = datetime.now().isoformat()
        cursor = conn.execute(
            """
            UPDATE goals SET is_completed = 1, completed_at = ?
            WHERE id = ?
            """,
            (now, goal_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def uncomplete_goal(goal_id: str) -> bool:
    """Mark a goal as not completed (undo). Returns True on success."""
    conn = _get_connection()
    try:
        cursor = conn.execute(
            """
            UPDATE goals SET is_completed = 0, completed_at = NULL
            WHERE id = ?
            """,
            (goal_id,),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def delete_goal(goal_id: str) -> bool:
    """Delete a goal. Returns True on success."""
    conn = _get_connection()
    try:
        cursor = conn.execute("DELETE FROM goals WHERE id = ?", (goal_id,))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def update_sort_orders(ids_to_orders: dict[str, int]) -> bool:
    """Update sort orders for multiple goals. Returns True on success."""
    if not ids_to_orders:
        return True
    conn = _get_connection()
    try:
        for goal_id, sort_order in ids_to_orders.items():
            _ = conn.execute(
                "UPDATE goals SET sort_order = ? WHERE id = ?",
                (sort_order, goal_id),
            )
        conn.commit()
        return True
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def update_goal_content(goal_id: str, content: str) -> bool:
    """Update a goal's content. Returns True on success."""
    conn = _get_connection()
    try:
        cursor = conn.execute(
            "UPDATE goals SET content = ? WHERE id = ?",
            (content, goal_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def get_week_metrics(week_start: date) -> WeekMetrics | None:
    """Get metrics for a given week. Returns None if not found."""
    conn = _get_connection()
    try:
        cursor = conn.execute(
            """
            SELECT week_start, h2_2025_estimate, predicted_time, actual_time,
                   created_at, updated_at
            FROM week_metrics
            WHERE week_start = ?
            """,
            (week_start.isoformat(),),
        )
        row = cast(sqlite3.Row | None, cursor.fetchone())
        if not row:
            return None
        return WeekMetrics(
            week_start=date.fromisoformat(cast(str, row["week_start"])),
            h2_2025_estimate=cast(float | None, row["h2_2025_estimate"]),
            predicted_time=cast(float | None, row["predicted_time"]),
            actual_time=cast(float | None, row["actual_time"]),
            created_at=datetime.fromisoformat(cast(str, row["created_at"])),
            updated_at=datetime.fromisoformat(cast(str, row["updated_at"])),
        )
    finally:
        conn.close()


def upsert_week_metrics(
    week_start: date,
    h2_2025_estimate: float | None = None,
    predicted_time: float | None = None,
    actual_time: float | None = None,
) -> bool:
    """Insert or update week metrics. Returns True on success."""
    conn = _get_connection()
    try:
        now = datetime.now().isoformat()
        existing = cast(
            sqlite3.Row | None,
            conn.execute(
                "SELECT 1 FROM week_metrics WHERE week_start = ?",
                (week_start.isoformat(),),
            ).fetchone(),
        )

        if existing:
            updates = ["updated_at = ?"]
            params: list[str | float | None] = [now]

            if h2_2025_estimate is not None:
                updates.append("h2_2025_estimate = ?")
                params.append(h2_2025_estimate)
            if predicted_time is not None:
                updates.append("predicted_time = ?")
                params.append(predicted_time)
            if actual_time is not None:
                updates.append("actual_time = ?")
                params.append(actual_time)

            params.append(week_start.isoformat())
            _ = conn.execute(
                f"UPDATE week_metrics SET {', '.join(updates)} WHERE week_start = ?",
                params,
            )
        else:
            _ = conn.execute(
                """
                INSERT INTO week_metrics
                    (week_start, h2_2025_estimate, predicted_time, actual_time,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    week_start.isoformat(),
                    h2_2025_estimate,
                    predicted_time,
                    actual_time,
                    now,
                    now,
                ),
            )
        conn.commit()
        return True
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def update_goal_completion(goal_id: str, is_completed: bool) -> bool:
    """Update a goal's completion status. Returns True on success."""
    conn = _get_connection()
    try:
        now = datetime.now().isoformat() if is_completed else None
        cursor = conn.execute(
            """
            UPDATE goals SET is_completed = ?, completed_at = ?
            WHERE id = ?
            """,
            (1 if is_completed else 0, now, goal_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def abandon_goal(goal_id: str) -> bool:
    """Mark a goal as abandoned. Returns True on success."""
    conn = _get_connection()
    try:
        now = datetime.now().isoformat()
        cursor = conn.execute(
            """
            UPDATE goals SET is_abandoned = 1, abandoned_at = ?
            WHERE id = ?
            """,
            (now, goal_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def unabandon_goal(goal_id: str) -> bool:
    """Mark a goal as not abandoned (undo). Returns True on success."""
    conn = _get_connection()
    try:
        cursor = conn.execute(
            """
            UPDATE goals SET is_abandoned = 0, abandoned_at = NULL
            WHERE id = ?
            """,
            (goal_id,),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def update_goal_estimates(
    goal_id: str,
    h2_2025_estimate: float | None = None,
    predicted_time: float | None = None,
) -> bool:
    """Update a goal's time estimates. Returns True on success."""
    conn = _get_connection()
    try:
        cursor = conn.execute(
            """
            UPDATE goals SET h2_2025_estimate = ?, predicted_time = ?
            WHERE id = ?
            """,
            (h2_2025_estimate, predicted_time, goal_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def update_goal_actual_time(goal_id: str, actual_time: float | None) -> bool:
    """Update a goal's actual time spent. Returns True on success."""
    conn = _get_connection()
    try:
        cursor = conn.execute(
            """
            UPDATE goals SET actual_time = ?
            WHERE id = ?
            """,
            (actual_time, goal_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()
