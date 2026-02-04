# Goal Abandonment & Per-Goal Time Estimates Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add ability to abandon goals (strikethrough display) and track time estimates per individual goal instead of at the week level.

**Architecture:** Extend the `Goal` dataclass with `is_abandoned`, `abandoned_at`, and three time fields (`h2_2025_estimate`, `predicted_time`, `actual_time`). Modify the setup and review modals to show per-goal inputs. Compute week totals by summing non-abandoned goals.

**Tech Stack:** Python, SQLite, Textual TUI framework

---

## Task 1: Add Abandonment Fields to Goal Data Model

**Files:**
- Modify: `src/status_dashboard/db/goals.py:11-19` (Goal dataclass)
- Modify: `src/status_dashboard/db/goals.py:45-56` (schema creation)

**Step 1: Update Goal dataclass**

Add `is_abandoned` and `abandoned_at` fields to the Goal dataclass:

```python
@dataclass
class Goal:
    id: str
    content: str
    week_start: date
    is_completed: bool
    is_abandoned: bool  # NEW
    completed_at: datetime | None
    abandoned_at: datetime | None  # NEW
    created_at: datetime
    sort_order: int
```

**Step 2: Update schema creation**

Add new columns to the CREATE TABLE statement and migration:

```python
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
        sort_order INTEGER DEFAULT 0
    )
""")
# Add migration for existing databases
try:
    _ = conn.execute("ALTER TABLE goals ADD COLUMN is_abandoned INTEGER DEFAULT 0")
except sqlite3.OperationalError:
    pass  # Column already exists
try:
    _ = conn.execute("ALTER TABLE goals ADD COLUMN abandoned_at TEXT")
except sqlite3.OperationalError:
    pass  # Column already exists
```

**Step 3: Update get_goals_for_week query**

Update the SELECT to include new fields and update the Goal construction:

```python
cursor = conn.execute(
    """
    SELECT id, content, week_start, is_completed, is_abandoned,
           completed_at, abandoned_at, created_at, sort_order
    FROM goals
    WHERE week_start = ?
    ORDER BY sort_order, created_at
    """,
    (week_start.isoformat(),),
)
# ... in the loop:
Goal(
    id=row["id"],
    content=row["content"],
    week_start=date.fromisoformat(row["week_start"]),
    is_completed=bool(row["is_completed"]),
    is_abandoned=bool(row["is_abandoned"]),
    completed_at=datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None,
    abandoned_at=datetime.fromisoformat(row["abandoned_at"]) if row["abandoned_at"] else None,
    created_at=datetime.fromisoformat(row["created_at"]),
    sort_order=row["sort_order"],
)
```

**Step 4: Run app to verify no crashes**

```bash
uv run status-dashboard
```

Press `q` to quit after verifying it starts.

**Step 5: Commit**

```bash
git add src/status_dashboard/db/goals.py
git commit -m "feat(goals): add is_abandoned and abandoned_at fields to Goal model"
```

---

## Task 2: Add abandon_goal and unabandon_goal Database Functions

**Files:**
- Modify: `src/status_dashboard/db/goals.py` (add functions after `uncomplete_goal`)

**Step 1: Add abandon_goal function**

```python
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
```

**Step 2: Add unabandon_goal function**

```python
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
```

**Step 3: Commit**

```bash
git add src/status_dashboard/db/goals.py
git commit -m "feat(goals): add abandon_goal and unabandon_goal functions"
```

---

## Task 3: Add GoalAbandonAction to Undo System

**Files:**
- Modify: `src/status_dashboard/undo.py` (add after GoalCompleteAction)

**Step 1: Add GoalAbandonAction dataclass**

```python
@dataclass
class GoalAbandonAction(UndoAction):
    goal_id: str = ""
    action_type: str = "goal_abandon"
```

**Step 2: Commit**

```bash
git add src/status_dashboard/undo.py
git commit -m "feat(undo): add GoalAbandonAction for undoing goal abandonment"
```

---

## Task 4: Add Abandon Goal Action and Keybinding in App

**Files:**
- Modify: `src/status_dashboard/app.py`

**Step 1: Add import for GoalAbandonAction**

At the top of app.py, update the undo import (around line 31):

```python
from status_dashboard.undo import (
    GoalAbandonAction,
    GoalCompleteAction,
    # ... rest of imports
)
```

**Step 2: Add keybinding to GoalsDataTable**

In the GoalsDataTable class BINDINGS (around line 408), add:

```python
Binding("x", "app.abandon_goal", "Abandon"),
```

**Step 3: Add action_abandon_goal method**

Add after `action_delete_goal` (around line 2395):

```python
def action_abandon_goal(self) -> None:
    """Mark the selected goal as abandoned."""
    focused = self.focused
    if not isinstance(focused, DataTable) or focused.id != "goals-table":
        self.notify("Select a goal first", severity="warning")
        return

    cell_key = focused.coordinate_to_cell_key(focused.cursor_coordinate)
    if not cell_key.row_key:
        return

    key = str(cell_key.row_key.value)
    if not key.startswith("goal:") or key == "goal:prompt":
        return

    goal_id = key.split(":", 1)[1]
    goal = next((g for g in self._goals if g.id == goal_id), None)
    if not goal:
        return

    if goal.is_abandoned:
        # Already abandoned - unabandon it
        if goals_db.unabandon_goal(goal_id):
            self.notify(f"Restored: {goal.content[:30]}")
            self._refresh_goals()
        else:
            self.notify("Failed to restore goal", severity="error")
    else:
        # Abandon the goal
        if goals_db.abandon_goal(goal_id):
            description = f"Abandon: {goal.content[:30]}"
            self._undo_stack.push(
                GoalAbandonAction(goal_id=goal_id, description=description)
            )
            self.notify(f"Abandoned: {goal.content[:30]}")
            self._refresh_goals()
        else:
            self.notify("Failed to abandon goal", severity="error")
```

**Step 4: Add undo handler for GoalAbandonAction**

In the `action_undo` method (around line 1200), add a case for GoalAbandonAction:

```python
elif isinstance(action, GoalAbandonAction):
    success = goals_db.unabandon_goal(action.goal_id)
    if success:
        self._refresh_goals()
        self.notify(f"Undid: {action.description}")
    else:
        self.notify("Failed to undo", severity="error")
```

**Step 5: Run and test abandonment**

```bash
uv run status-dashboard
```

Test: Navigate to goals, press `x` on a goal, verify it works. Press `z` to undo.

**Step 6: Commit**

```bash
git add src/status_dashboard/app.py
git commit -m "feat(goals): add abandon goal action with x keybinding and undo support"
```

---

## Task 5: Display Abandoned Goals with Strikethrough

**Files:**
- Modify: `src/status_dashboard/app.py` (in `_render_goals_table`)

**Step 1: Update goal rendering to show strikethrough for abandoned goals**

In `_render_goals_table` (around line 738), update the incomplete_goals filter and rendering:

```python
# Change this line:
incomplete_goals = [g for g in self._goals if not g.is_completed]

# To include abandoned goals but not completed:
active_goals = [g for g in self._goals if not g.is_completed]
```

Then in the rendering loop, add strikethrough styling:

```python
for goal in active_goals:
    content = (
        goal.content[:60] + "…"
        if len(goal.content) > 60
        else goal.content
    )
    if goal.is_abandoned:
        # Strikethrough for abandoned goals
        text = Text(content, style="strike dim")
    else:
        text = Text(content)
    _ = table.add_row(
        "",
        "",
        text,
        key=f"goal:{goal.id}",
    )
```

**Step 2: Update review mode rendering similarly**

In the review mode section (around line 706), add strikethrough:

```python
for goal in self._goals:
    checkbox = "[x]" if goal.is_completed else "[ ]"
    content = (
        goal.content[:60] + "…"
        if len(goal.content) > 60
        else goal.content
    )
    if goal.is_abandoned:
        text = Text(f"{checkbox} {content}", style="strike dim")
    else:
        text = Text(f"{checkbox} {content}")
    _ = table.add_row(
        "",
        "",
        text,
        key=f"goal:{goal.id}",
    )
```

**Step 3: Test strikethrough display**

```bash
uv run status-dashboard
```

Abandon a goal with `x`, verify strikethrough appears.

**Step 4: Commit**

```bash
git add src/status_dashboard/app.py
git commit -m "feat(goals): display abandoned goals with strikethrough styling"
```

---

## Task 6: Add Time Estimate Fields to Goal Data Model

**Files:**
- Modify: `src/status_dashboard/db/goals.py`

**Step 1: Add time fields to Goal dataclass**

```python
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
    h2_2025_estimate: float | None = None  # NEW
    predicted_time: float | None = None     # NEW
    actual_time: float | None = None        # NEW
```

**Step 2: Add schema migration for time fields**

In `_get_connection`, add migrations:

```python
try:
    _ = conn.execute("ALTER TABLE goals ADD COLUMN h2_2025_estimate REAL")
except sqlite3.OperationalError:
    pass
try:
    _ = conn.execute("ALTER TABLE goals ADD COLUMN predicted_time REAL")
except sqlite3.OperationalError:
    pass
try:
    _ = conn.execute("ALTER TABLE goals ADD COLUMN actual_time REAL")
except sqlite3.OperationalError:
    pass
```

**Step 3: Update get_goals_for_week query**

```python
cursor = conn.execute(
    """
    SELECT id, content, week_start, is_completed, is_abandoned,
           completed_at, abandoned_at, created_at, sort_order,
           h2_2025_estimate, predicted_time, actual_time
    FROM goals
    WHERE week_start = ?
    ORDER BY sort_order, created_at
    """,
    (week_start.isoformat(),),
)
# ... in the Goal construction:
h2_2025_estimate=row["h2_2025_estimate"],
predicted_time=row["predicted_time"],
actual_time=row["actual_time"],
```

**Step 4: Commit**

```bash
git add src/status_dashboard/db/goals.py
git commit -m "feat(goals): add per-goal time estimate fields (h2, predicted, actual)"
```

---

## Task 7: Add update_goal_estimates Database Function

**Files:**
- Modify: `src/status_dashboard/db/goals.py`

**Step 1: Add update_goal_estimates function**

```python
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
```

**Step 2: Commit**

```bash
git add src/status_dashboard/db/goals.py
git commit -m "feat(goals): add update_goal_estimates and update_goal_actual_time functions"
```

---

## Task 8: Update WeeklyGoalsSetupModal for Per-Goal Estimates

**Files:**
- Modify: `src/status_dashboard/widgets/create_modals.py`

This is the largest change. The modal needs to show inline inputs for each goal.

**Step 1: Update the CSS for goal rows with estimates**

Add new CSS rules:

```python
CSS = """
WeeklyGoalsSetupModal {
    align: center middle;
}

#setup-dialog {
    background: $surface;
    border: thick $primary;
    width: 90;
    height: auto;
    max-height: 90%;
    padding: 1 2;
}

#setup-dialog Label {
    margin-top: 1;
}

#goals-container {
    height: auto;
    max-height: 16;
    margin-bottom: 1;
    border: solid $primary-lighten-2;
    overflow-y: auto;
}

.goal-row {
    layout: horizontal;
    height: 3;
    padding: 0 1;
}

.goal-row.-highlighted {
    background: $accent;
}

.goal-content {
    width: 1fr;
}

.goal-estimate-input {
    width: 8;
    margin-left: 1;
}

#keybindings-hint {
    color: $text-muted;
    margin-bottom: 1;
}

#totals-row {
    layout: horizontal;
    height: auto;
    margin-top: 1;
    margin-bottom: 1;
    padding: 0 1;
    background: $surface-darken-1;
}

#totals-row Label {
    margin-top: 0;
}

#setup-buttons {
    layout: horizontal;
    align: center middle;
    height: auto;
    margin-top: 1;
}

#setup-buttons Button {
    margin: 0 1;
}

#edit-container {
    display: none;
    height: auto;
    margin-bottom: 1;
}

#edit-container.-visible {
    display: block;
}

#edit-input {
    width: 100%;
}
"""
```

**Step 2: Rewrite compose() method**

Replace the compose method to use a scrollable container with goal rows instead of ListView:

```python
def compose(self) -> ComposeResult:
    week_str = self.week_start.strftime("%b %d, %Y")

    with Container(id="setup-dialog"):
        yield Label(f"Weekly Goals - Week of {week_str}", id="title")
        yield Label("Goals (H2 = H2 2025 estimate, Pred = Predicted time):")
        yield Vertical(id="goals-container")
        yield Label(
            "[a] Add  [e] Edit  [d] Delete  [J/K] Reorder  [Tab] Edit estimates",
            id="keybindings-hint",
        )

        with Container(id="edit-container"):
            yield Input(placeholder="Edit goal", id="edit-input")

        with Horizontal(id="totals-row"):
            yield Label("Totals:", id="totals-label")
            yield Label("H2: 0.0h", id="total-h2")
            yield Label("Pred: 0.0h", id="total-pred")

        with Horizontal(id="setup-buttons"):
            yield Button("Save", variant="primary", id="save-btn")
            yield Button("Cancel", id="cancel-btn")
```

**Step 3: Add _refresh_goals_list with estimate inputs**

```python
def _refresh_goals_list(self) -> None:
    container = self.query_one("#goals-container", Vertical)
    container.remove_children()

    if not self.goals:
        container.mount(Label("No goals yet - press 'a' to add", classes="goal-content"))
    else:
        for i, goal in enumerate(self.goals):
            content = goal.content[:40] + "…" if len(goal.content) > 40 else goal.content
            h2_val = str(goal.h2_2025_estimate) if goal.h2_2025_estimate is not None else ""
            pred_val = str(goal.predicted_time) if goal.predicted_time is not None else ""

            row = Horizontal(classes="goal-row", id=f"goal-row-{i}")
            row.compose_add_child(Label(f"{i + 1}. {content}", classes="goal-content"))
            row.compose_add_child(Input(value=h2_val, placeholder="H2", classes="goal-estimate-input", id=f"h2-{i}"))
            row.compose_add_child(Input(value=pred_val, placeholder="Pred", classes="goal-estimate-input", id=f"pred-{i}"))
            container.mount(row)

    self._update_totals()
```

**Step 4: Add _update_totals method**

```python
def _update_totals(self) -> None:
    total_h2 = sum(g.h2_2025_estimate or 0 for g in self.goals if not g.is_abandoned)
    total_pred = sum(g.predicted_time or 0 for g in self.goals if not g.is_abandoned)

    self.query_one("#total-h2", Label).update(f"H2: {total_h2:.1f}h")
    self.query_one("#total-pred", Label).update(f"Pred: {total_pred:.1f}h")
```

**Step 5: Update on_button_pressed to collect per-goal estimates**

```python
def on_button_pressed(self, event: Button.Pressed) -> None:
    if event.button.id == "save-btn":
        # Collect estimates from inputs
        for i, goal in enumerate(self.goals):
            try:
                h2_input = self.query_one(f"#h2-{i}", Input)
                h2_val = float(h2_input.value.strip()) if h2_input.value.strip() else None
            except (ValueError, Exception):
                h2_val = None
            try:
                pred_input = self.query_one(f"#pred-{i}", Input)
                pred_val = float(pred_input.value.strip()) if pred_input.value.strip() else None
            except (ValueError, Exception):
                pred_val = None

            # Update goal object with estimates
            self.goals[i] = goals_db.Goal(
                id=goal.id,
                content=goal.content,
                week_start=goal.week_start,
                is_completed=goal.is_completed,
                is_abandoned=goal.is_abandoned,
                completed_at=goal.completed_at,
                abandoned_at=goal.abandoned_at,
                created_at=goal.created_at,
                sort_order=goal.sort_order,
                h2_2025_estimate=h2_val,
                predicted_time=pred_val,
                actual_time=goal.actual_time,
            )

        _ = self.dismiss(
            {
                "week_start": self.week_start,
                "goals": self.goals,
            }
        )
    else:
        _ = self.dismiss(None)
```

**Step 6: Test the setup modal**

```bash
uv run status-dashboard
```

Press `e` on goals panel, verify per-goal estimate inputs appear.

**Step 7: Commit**

```bash
git add src/status_dashboard/widgets/create_modals.py
git commit -m "feat(modals): update WeeklyGoalsSetupModal with per-goal estimate inputs"
```

---

## Task 9: Update App to Handle Per-Goal Estimates from Setup Modal

**Files:**
- Modify: `src/status_dashboard/app.py`

**Step 1: Update _handle_setup_complete to save per-goal estimates**

In the loop that processes goals (around line 2492):

```python
for goal in goals_from_modal:
    if not goal.id:
        # New goal - create with estimates
        new_id = goals_db.create_goal(goal.content, week_start)
        if goal.h2_2025_estimate is not None or goal.predicted_time is not None:
            _ = goals_db.update_goal_estimates(
                new_id, goal.h2_2025_estimate, goal.predicted_time
            )
    elif goal.id in existing_goals:
        existing = existing_goals[goal.id]
        if existing.content != goal.content:
            _ = goals_db.update_goal_content(goal.id, goal.content)
        # Always update estimates
        _ = goals_db.update_goal_estimates(
            goal.id, goal.h2_2025_estimate, goal.predicted_time
        )
```

**Step 2: Remove the week_metrics upsert for h2/predicted**

Remove or comment out the section that calls `upsert_week_metrics` for h2_2025_estimate and predicted_time.

**Step 3: Update panel title to compute from goals**

In `_render_goals_table`, update the title computation:

```python
# Compute totals from non-abandoned, non-completed goals
active_goals = [g for g in self._goals if not g.is_completed and not g.is_abandoned]
total_h2 = sum(g.h2_2025_estimate or 0 for g in active_goals)
total_pred = sum(g.predicted_time or 0 for g in active_goals)

title_parts = ["Weekly Goals"]
if total_h2 > 0 or total_pred > 0:
    estimates = []
    if total_h2 > 0:
        estimates.append(f"Est: {total_h2:.1f}h")
    if total_pred > 0:
        estimates.append(f"Pred: {total_pred:.1f}h")
    title_parts.append(f"({' / '.join(estimates)})")

panel.border_title = " ".join(title_parts)
```

**Step 4: Test end-to-end**

```bash
uv run status-dashboard
```

Add goals with estimates, verify totals appear in title.

**Step 5: Commit**

```bash
git add src/status_dashboard/app.py
git commit -m "feat(app): save per-goal estimates and compute totals for panel title"
```

---

## Task 10: Update WeeklyReviewModal for Per-Goal Actual Time

**Files:**
- Modify: `src/status_dashboard/widgets/create_modals.py`

**Step 1: Update CSS for review modal with actual time inputs**

Add/update CSS:

```python
.review-goal-row {
    layout: horizontal;
    height: 3;
    padding: 0 1;
}

.review-goal-row.-highlighted {
    background: $accent;
}

.review-goal-content {
    width: 1fr;
}

.review-estimates {
    width: 20;
    color: $text-muted;
}

.review-actual-input {
    width: 8;
    margin-left: 1;
}

#review-totals-row {
    layout: horizontal;
    height: auto;
    margin-top: 1;
    padding: 0 1;
    background: $surface-darken-1;
}
```

**Step 2: Update compose() for per-goal layout**

Replace ListView with a scrollable container showing goal rows with actual time inputs:

```python
def compose(self) -> ComposeResult:
    week_str = self.week_start.strftime("%b %d, %Y")

    with Container(id="review-dialog"):
        yield Label(f"Weekly Review - Week of {week_str}", id="review-title")
        yield Label("Goals (Space/Enter to toggle completion):")
        yield Vertical(id="review-goals-container")
        yield Label(
            "[j/k] Navigate  [Space/Enter] Toggle",
            id="review-keybindings-hint",
        )

        with Horizontal(id="review-totals-row"):
            yield Label("Totals:", id="review-totals-label")
            yield Label("H2: 0.0h", id="review-total-h2")
            yield Label("Pred: 0.0h", id="review-total-pred")
            yield Label("Actual: 0.0h", id="review-total-actual")

        with Horizontal(id="review-buttons"):
            yield Button("Done", variant="primary", id="done-btn")
            yield Button("Skip", id="skip-btn")
```

**Step 3: Update _refresh_goals_list for review modal**

```python
async def _refresh_goals_list(self) -> None:
    container = self.query_one("#review-goals-container", Vertical)
    await container.remove_children()

    if not self.goals:
        await container.mount(Label("No goals from last week"))
    else:
        for i, goal in enumerate(self.goals):
            checkbox = "[x]" if self._completions.get(goal.id, False) else "[ ]"
            content = goal.content[:35] + "…" if len(goal.content) > 35 else goal.content

            h2_str = f"H2:{goal.h2_2025_estimate:.1f}" if goal.h2_2025_estimate else ""
            pred_str = f"P:{goal.predicted_time:.1f}" if goal.predicted_time else ""
            estimates = f"{h2_str} {pred_str}".strip()

            actual_val = str(goal.actual_time) if goal.actual_time is not None else ""
            actual_val = self._actual_times.get(goal.id, actual_val)

            style = "strike dim" if goal.is_abandoned else ""

            row = Horizontal(classes="review-goal-row", id=f"review-row-{i}")
            row.compose_add_child(Label(f"{checkbox} {content}", classes="review-goal-content", markup=False))
            row.compose_add_child(Label(estimates, classes="review-estimates"))
            row.compose_add_child(Input(value=actual_val, placeholder="Actual", classes="review-actual-input", id=f"actual-{i}"))
            await container.mount(row)

    self._update_totals()
```

**Step 4: Add _actual_times dict and _update_totals**

In `__init__`:
```python
self._actual_times: dict[str, str] = {}  # goal_id -> actual time string from input
```

```python
def _update_totals(self) -> None:
    total_h2 = sum(g.h2_2025_estimate or 0 for g in self.goals if not g.is_abandoned)
    total_pred = sum(g.predicted_time or 0 for g in self.goals if not g.is_abandoned)

    # Sum actual times from inputs
    total_actual = 0.0
    for i, goal in enumerate(self.goals):
        if goal.is_abandoned:
            continue
        try:
            actual_input = self.query_one(f"#actual-{i}", Input)
            if actual_input.value.strip():
                total_actual += float(actual_input.value.strip())
        except Exception:
            pass

    self.query_one("#review-total-h2", Label).update(f"H2: {total_h2:.1f}h")
    self.query_one("#review-total-pred", Label).update(f"Pred: {total_pred:.1f}h")
    self.query_one("#review-total-actual", Label).update(f"Actual: {total_actual:.1f}h")
```

**Step 5: Update on_button_pressed to return per-goal actual times**

```python
def on_button_pressed(self, event: Button.Pressed) -> None:
    if event.button.id == "done-btn":
        goal_actual_times: dict[str, float | None] = {}
        for i, goal in enumerate(self.goals):
            try:
                actual_input = self.query_one(f"#actual-{i}", Input)
                if actual_input.value.strip():
                    goal_actual_times[goal.id] = float(actual_input.value.strip())
                else:
                    goal_actual_times[goal.id] = None
            except Exception:
                goal_actual_times[goal.id] = None

        _ = self.dismiss(
            {
                "week_start": self.week_start,
                "goal_completions": self._completions,
                "goal_actual_times": goal_actual_times,
            }
        )
    else:
        _ = self.dismiss(None)
```

**Step 6: Commit**

```bash
git add src/status_dashboard/widgets/create_modals.py
git commit -m "feat(modals): update WeeklyReviewModal with per-goal actual time inputs"
```

---

## Task 11: Update App to Handle Per-Goal Actual Times from Review Modal

**Files:**
- Modify: `src/status_dashboard/app.py`

**Step 1: Update _handle_review_complete**

```python
def _handle_review_complete(self, result: dict[str, Any] | None) -> None:
    """Handle the result from the weekly review modal."""
    if result is None:
        self._refresh_goals()
        return

    goal_completions: dict[str, bool] = result.get("goal_completions", {})
    goal_actual_times: dict[str, float | None] = result.get("goal_actual_times", {})

    # Update goal completion statuses
    for goal_id, is_completed in goal_completions.items():
        _ = goals_db.update_goal_completion(goal_id, is_completed)

    # Update goal actual times
    for goal_id, actual_time in goal_actual_times.items():
        _ = goals_db.update_goal_actual_time(goal_id, actual_time)

    self._refresh_goals()
```

**Step 2: Test the full flow**

```bash
uv run status-dashboard
```

Create goals with estimates, abandon one, complete one, simulate review, verify actual times save.

**Step 3: Commit**

```bash
git add src/status_dashboard/app.py
git commit -m "feat(app): save per-goal actual times from review modal"
```

---

## Task 12: Run Linters and Fix Issues

**Files:**
- Potentially all modified files

**Step 1: Run ruff check**

```bash
uv run ruff check --fix .
```

**Step 2: Run ruff format**

```bash
uv run ruff format .
```

**Step 3: Run basedpyright**

```bash
uv run basedpyright
```

Fix any type errors.

**Step 4: Commit fixes**

```bash
git add -A
git commit -m "chore: fix linting and type errors"
```

---

## Task 13: Final Integration Test

**Step 1: Start the app**

```bash
uv run status-dashboard
```

**Step 2: Test checklist**

- [ ] Create a new goal
- [ ] Add H2 estimate and predicted time in setup modal
- [ ] Verify totals update in modal
- [ ] Verify panel title shows computed totals
- [ ] Press `x` to abandon a goal
- [ ] Verify strikethrough appears
- [ ] Verify abandoned goal doesn't count in totals
- [ ] Press `z` to undo abandonment
- [ ] Complete a goal with `c`
- [ ] Verify completed goal is hidden
- [ ] Open setup modal with `e`, verify estimates are preserved
- [ ] If Monday, verify review modal shows goals with actual time inputs

**Step 3: Final commit if needed**

```bash
git status
# If any uncommitted changes:
git add -A
git commit -m "chore: final integration fixes"
```
