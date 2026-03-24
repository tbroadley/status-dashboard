from dataclasses import dataclass, field


@dataclass
class UndoAction:
    description: str
    action_type: str = ""


@dataclass
class TodoistCompleteAction(UndoAction):
    task_id: str = ""
    action_type: str = "todoist_complete"


@dataclass
class TodoistDeferAction(UndoAction):
    task_id: str = ""
    original_due_date: str | None = None
    action_type: str = "todoist_defer"


@dataclass
class TodoistMoveAction(UndoAction):
    ids_to_orders: dict[str, int] = field(default_factory=dict)
    action_type: str = "todoist_move"


@dataclass
class GoalCompleteAction(UndoAction):
    goal_id: str = ""
    action_type: str = "goal_complete"


@dataclass
class GoalAbandonAction(UndoAction):
    goal_id: str = ""
    action_type: str = "goal_abandon"


class UndoStack:
    _max_size: int

    def __init__(self, max_size: int = 15):
        self._stack: list[UndoAction] = []
        self._max_size = max_size

    def push(self, action: UndoAction) -> None:
        self._stack.append(action)
        if len(self._stack) > self._max_size:
            _ = self._stack.pop(0)

    def pop(self) -> UndoAction | None:
        return self._stack.pop() if self._stack else None

    def pop_if_matches(self, action: UndoAction) -> bool:
        """Pop the top action if it is the given action. Returns True if popped."""
        if self._stack and self._stack[-1] is action:
            _ = self._stack.pop()
            return True
        return False

    def is_empty(self) -> bool:
        return len(self._stack) == 0
