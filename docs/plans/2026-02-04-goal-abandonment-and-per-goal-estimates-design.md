# Goal Abandonment & Per-Goal Time Estimates

## Overview

Two related enhancements to the weekly goals system:
1. Ability to mark goals as "abandoned" (not going to do)
2. Per-goal time estimates instead of week-level totals

## Feature 1: Goal Abandonment

### Data Model

Add `is_abandoned` boolean field to `Goal` dataclass and `goals` table:

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

Database migration adds:
- `is_abandoned INTEGER DEFAULT 0`
- `abandoned_at TEXT`

### UI Behavior

- **Keybinding:** `x` to abandon a goal
- **Display:** Abandoned goals show with strikethrough text styling
- **Visibility:** Abandoned goals remain visible in the main goals list (unlike completed goals which are hidden)
- **Undo:** `z` key reverses abandonment via new `GoalAbandonAction`
- **Review modal:** Abandoned goals display with strikethrough, cannot be toggled (already resolved)

### Database Functions

- `abandon_goal(goal_id: str) -> bool`
- `unaban_goal(goal_id: str) -> bool`

## Feature 2: Per-Goal Time Estimates

### Data Model

Add three nullable float fields to `Goal`:

```python
@dataclass
class Goal:
    # ... existing fields ...
    h2_2025_estimate: float | None  # NEW - hours
    predicted_time: float | None     # NEW - hours
    actual_time: float | None        # NEW - hours
```

Database migration adds:
- `h2_2025_estimate REAL`
- `predicted_time REAL`
- `actual_time REAL`

The `week_metrics` table becomes deprecated - totals are computed by summing non-abandoned goals.

### Setup Modal Changes

Each goal row displays inline input fields:
```
[goal text truncated...] [H2: ___h] [Pred: ___h]
```

Bottom of modal shows auto-calculated totals:
```
Total: H2 estimate: 10.5h | Predicted: 12.0h
```

Totals exclude abandoned goals.

### Review Modal Changes

Each goal row shows estimates (read-only) plus actual time input:
```
[x] Goal text here          H2: 2h | Pred: 3h | Actual: [___]h
```

Bottom shows totals row summing all values.

### Panel Title

Displays computed totals from non-abandoned, non-completed goals:
```
Weekly Goals (Est: 10.5h / Pred: 12.0h)
```

### Database Functions

- `update_goal_estimates(goal_id: str, h2_estimate: float | None, predicted: float | None) -> bool`
- `update_goal_actual_time(goal_id: str, actual: float | None) -> bool`
- `get_week_totals(week_start: date) -> dict` - computes sums from goals

## Migration Strategy

1. Run schema migration to add new columns with defaults
2. Existing goals get `is_abandoned=False` and `NULL` for time fields
3. Keep `week_metrics` table for historical data but stop writing to it
4. Panel title logic falls back to `week_metrics` if no per-goal estimates exist (backwards compat)
