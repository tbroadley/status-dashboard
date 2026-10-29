from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class UndoAction:
    description: str
    action_type: str = ""


def _no_edit(_rows: list[list[str]]) -> bool:
    return False


@dataclass
class TaskEditAction(UndoAction):
    """Undo a task change by applying `edit`, which puts back what it changed."""

    edit: Callable[[list[list[str]]], bool] = _no_edit
    action_type: str = "task_edit"


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


@dataclass
class LinearMoveAction(UndoAction):
    issue_id: str = ""
    previous_sort_order: float = 0.0
    action_type: str = "linear_move"


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

    def remove(self, action: UndoAction) -> None:
        """Drop `action` wherever it is in the stack, e.g. when it failed to apply."""
        self._stack = [entry for entry in self._stack if entry is not action]

    def is_empty(self) -> bool:
        return len(self._stack) == 0
