"""SQLite database operations for weekly goals."""

import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path


@dataclass
class Goal:
    id: str
    content: str
    week_start: date
    is_completed: bool
    completed_at: datetime | None
    created_at: datetime
    sort_order: int


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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS goals (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            week_start DATE NOT NULL,
            is_completed INTEGER DEFAULT 0,
            completed_at TEXT,
            created_at TEXT NOT NULL,
            sort_order INTEGER DEFAULT 0
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_goals_week ON goals(week_start)")
    conn.commit()
    return conn


def get_week_start(d: date) -> date:
    """Get the Monday of the week containing the given date."""
    return d - __import__("datetime").timedelta(days=d.weekday())


def get_goals_for_week(week_start: date) -> list[Goal]:
    """Get all goals for a given week (by week_start Monday)."""
    conn = _get_connection()
    try:
        cursor = conn.execute(
            """
            SELECT id, content, week_start, is_completed, completed_at, created_at, sort_order
            FROM goals
            WHERE week_start = ?
            ORDER BY sort_order, created_at
            """,
            (week_start.isoformat(),),
        )
        goals = []
        for row in cursor.fetchall():
            goals.append(
                Goal(
                    id=row["id"],
                    content=row["content"],
                    week_start=date.fromisoformat(row["week_start"]),
                    is_completed=bool(row["is_completed"]),
                    completed_at=(
                        datetime.fromisoformat(row["completed_at"])
                        if row["completed_at"]
                        else None
                    ),
                    created_at=datetime.fromisoformat(row["created_at"]),
                    sort_order=row["sort_order"],
                )
            )
        return goals
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
        sort_order = cursor.fetchone()[0]
        conn.execute(
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
