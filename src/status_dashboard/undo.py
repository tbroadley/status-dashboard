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
class LinearSetStateAction(UndoAction):
    issue_id: str = ""
    team_id: str = ""
    previous_state: str = ""
    action_type: str = "linear_set_state"


@dataclass
class LinearAssignAction(UndoAction):
    issue_id: str = ""
    previous_assignee_id: str | None = None
    action_type: str = "linear_assign"


class UndoStack:
    def __init__(self, max_size: int = 15):
        self._stack: list[UndoAction] = []
        self._max_size = max_size

    def push(self, action: UndoAction) -> None:
        self._stack.append(action)
        if len(self._stack) > self._max_size:
            self._stack.pop(0)

    def pop(self) -> UndoAction | None:
        return self._stack.pop() if self._stack else None

    def is_empty(self) -> bool:
        return len(self._stack) == 0
