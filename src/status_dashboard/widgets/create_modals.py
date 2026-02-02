"""Modal dialogs for creating Todoist tasks and Linear issues."""

from datetime import date
from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, ListItem, ListView, Select, TextArea

from status_dashboard.db import goals as goals_db


class CreateTodoistTaskModal(ModalScreen):
    """Modal for creating a new Todoist task."""

    BINDINGS = [("escape", "dismiss_modal", "Close")]

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)

    CSS = """
    CreateTodoistTaskModal {
        align: center middle;
    }

    #dialog {
        background: $surface;
        border: thick $primary;
        width: 60;
        height: auto;
        padding: 1 2;
    }

    #dialog Label {
        margin-top: 1;
    }

    #dialog Input {
        margin-bottom: 1;
    }

    #dialog Select {
        margin-bottom: 1;
    }

    #buttons {
        layout: horizontal;
        align: center middle;
        height: auto;
        margin-top: 1;
    }

    #buttons Button {
        margin: 0 1;
    }
    """

    def compose(self) -> ComposeResult:
        with Container(id="dialog"):
            yield Label("Create Todoist Task", id="title")
            yield Label("Task:")
            yield Input(placeholder="Enter task description", id="task-input")
            yield Label("Due:")
            yield Select(
                [
                    ("Today", "today"),
                    ("Tomorrow", "tomorrow"),
                    ("Monday", "monday"),
                    ("Next Week", "next week"),
                ],
                value="today",
                id="due-select",
            )
            with Vertical(id="buttons"):
                yield Button("Create", variant="primary", id="create-btn")
                yield Button("Cancel", id="cancel-btn")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "create-btn":
            task_input = self.query_one("#task-input", Input)
            due_select = self.query_one("#due-select", Select)

            task_content = task_input.value.strip()
            if task_content:
                self.dismiss(
                    {
                        "content": task_content,
                        "due_string": str(due_select.value),
                    }
                )
            else:
                task_input.focus()
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle Enter key in the input."""
        if event.input.id == "task-input":
            # Simulate create button press
            create_btn = self.query_one("#create-btn", Button)
            self.on_button_pressed(Button.Pressed(create_btn))


class CreateLinearIssueModal(ModalScreen):
    """Modal for creating a new Linear issue."""

    BINDINGS = [("escape", "dismiss_modal", "Close")]

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)

    CSS = """
    CreateLinearIssueModal {
        align: center middle;
    }

    #dialog {
        background: $surface;
        border: thick $primary;
        width: 60;
        height: auto;
        padding: 1 2;
    }

    #dialog Label {
        margin-top: 1;
    }

    #dialog Input {
        margin-bottom: 1;
    }

    #dialog Select {
        margin-bottom: 1;
    }

    #buttons {
        layout: horizontal;
        align: center middle;
        height: auto;
        margin-top: 1;
    }

    #buttons Button {
        margin: 0 1;
    }
    """

    def __init__(
        self,
        team_members: list[dict[str, Any]],
        *args: Any,
        viewer_id: str | None = None,
        **kwargs: Any,
    ):
        super().__init__(*args, **kwargs)
        self.team_members = team_members
        self.viewer_id = viewer_id

    def compose(self) -> ComposeResult:
        # Build assignee options, with viewer at the top
        assignee_options = [("Unassigned", "")]
        sorted_members = sorted(
            self.team_members,
            key=lambda m: (
                m["id"] != self.viewer_id,
                m.get("displayName") or m.get("name", ""),
            ),
        )
        for member in sorted_members:
            display_name = member.get("displayName") or member.get("name", "Unknown")
            assignee_options.append((display_name, member["id"]))

        with Container(id="dialog"):
            yield Label("Create Linear Issue", id="title")
            yield Label("Title:")
            yield Input(placeholder="Enter issue title", id="title-input")
            yield Label("State:")
            yield Select(
                [
                    ("Backlog", "backlog"),
                    ("Todo", "todo"),
                    ("In Progress", "in_progress"),
                    ("In Review", "in_review"),
                ],
                value="todo",
                id="state-select",
            )
            yield Label("Assignee:")
            yield Select(
                assignee_options,
                value="",
                id="assignee-select",
            )
            with Vertical(id="buttons"):
                yield Button("Create", variant="primary", id="create-btn")
                yield Button("Cancel", id="cancel-btn")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "create-btn":
            title_input = self.query_one("#title-input", Input)
            state_select = self.query_one("#state-select", Select)
            assignee_select = self.query_one("#assignee-select", Select)

            title = title_input.value.strip()
            if title:
                result = {
                    "title": title,
                    "state": str(state_select.value),
                }
                assignee_id = str(assignee_select.value)
                if assignee_id:
                    result["assignee_id"] = assignee_id
                self.dismiss(result)
            else:
                title_input.focus()
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle Enter key in the input."""
        if event.input.id == "title-input":
            # Simulate create button press
            create_btn = self.query_one("#create-btn", Button)
            self.on_button_pressed(Button.Pressed(create_btn))


class EditTodoistTaskModal(ModalScreen[dict[str, str] | None]):
    """Modal for editing an existing Todoist task."""

    BINDINGS = [("escape", "dismiss_modal", "Close")]

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)

    CSS = """
    EditTodoistTaskModal {
        align: center middle;
    }

    #dialog {
        background: $surface;
        border: thick $primary;
        width: 70;
        height: auto;
        padding: 1 2;
    }

    #dialog Label {
        margin-top: 1;
    }

    #dialog Input {
        margin-bottom: 1;
    }

    #dialog TextArea {
        margin-bottom: 1;
        height: 4;
    }

    #dialog Select {
        margin-bottom: 1;
    }

    #buttons {
        layout: horizontal;
        align: center middle;
        height: auto;
        margin-top: 1;
    }

    #buttons Button {
        margin: 0 1;
    }
    """

    def __init__(
        self,
        task_id: str,
        content: str,
        description: str,
        project_id: str | None,
        due_string: str | None,
        projects: list[tuple[str, str]],
        *args: Any,
        **kwargs: Any,
    ):
        super().__init__(*args, **kwargs)
        self.task_id = task_id
        self.initial_content = content
        self.initial_description = description
        self.initial_project_id = project_id
        self.initial_due_string = due_string
        self.projects = projects

    def compose(self) -> ComposeResult:
        project_options: list[tuple[str, str]] = [("Inbox", "")]
        project_options.extend(self.projects)

        with Container(id="dialog"):
            yield Label("Edit Todoist Task", id="title")
            yield Label("Title:")
            yield Input(
                value=self.initial_content,
                placeholder="Enter task title",
                id="content-input",
            )
            yield Label("Description:")
            yield TextArea(
                self.initial_description,
                id="description-input",
            )
            yield Label("Project:")
            yield Select(
                project_options,
                value=self.initial_project_id or "",
                id="project-select",
            )
            yield Label("Due:")
            yield Input(
                value=self.initial_due_string or "",
                placeholder="today, tomorrow, next week, 2024-01-15, etc.",
                id="due-input",
            )
            with Vertical(id="buttons"):
                yield Button("Save", variant="primary", id="save-btn")
                yield Button("Cancel", id="cancel-btn")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save-btn":
            content_input = self.query_one("#content-input", Input)
            description_input = self.query_one("#description-input", TextArea)
            project_select = self.query_one("#project-select", Select)
            due_input = self.query_one("#due-input", Input)

            content = content_input.value.strip()
            if not content:
                content_input.focus()
                return

            result: dict[str, str] = {"task_id": self.task_id}

            if content != self.initial_content:
                result["content"] = content

            description = description_input.text
            if description != self.initial_description:
                result["description"] = description

            project_id = str(project_select.value)
            if project_id != (self.initial_project_id or ""):
                result["project_id"] = project_id

            due_string = due_input.value.strip()
            if due_string != (self.initial_due_string or ""):
                result["due_string"] = due_string

            self.dismiss(result)
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle Enter key in the title input."""
        if event.input.id == "content-input":
            description_input = self.query_one("#description-input")
            description_input.focus()
        elif event.input.id == "due-input":
            save_btn = self.query_one("#save-btn", Button)
            self.on_button_pressed(Button.Pressed(save_btn))


class CreateGoalModal(ModalScreen[dict[str, str] | None]):
    """Modal for creating a new weekly goal."""

    BINDINGS = [("escape", "dismiss_modal", "Close")]

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)

    CSS = """
    CreateGoalModal {
        align: center middle;
    }

    #dialog {
        background: $surface;
        border: thick $primary;
        width: 60;
        height: auto;
        padding: 1 2;
    }

    #dialog Label {
        margin-top: 1;
    }

    #dialog Input {
        margin-bottom: 1;
    }

    #buttons {
        layout: horizontal;
        align: center middle;
        height: auto;
        margin-top: 1;
    }

    #buttons Button {
        margin: 0 1;
    }
    """

    def compose(self) -> ComposeResult:
        with Container(id="dialog"):
            yield Label("Add Weekly Goal", id="title")
            yield Label("Goal:")
            yield Input(placeholder="Enter your goal for this week", id="goal-input")
            with Vertical(id="buttons"):
                yield Button("Add", variant="primary", id="create-btn")
                yield Button("Cancel", id="cancel-btn")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "create-btn":
            goal_input = self.query_one("#goal-input", Input)
            content = goal_input.value.strip()
            if content:
                self.dismiss({"content": content})
            else:
                goal_input.focus()
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle Enter key in the input."""
        if event.input.id == "goal-input":
            create_btn = self.query_one("#create-btn", Button)
            self.on_button_pressed(Button.Pressed(create_btn))


class WeeklyGoalsSetupModal(ModalScreen[dict[str, Any] | None]):
    """Full-screen modal for managing all weekly goals at once."""

    BINDINGS = [
        Binding("escape", "dismiss_modal", "Close"),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("a", "add_goal", "Add"),
        Binding("e", "edit_goal", "Edit"),
        Binding("d", "delete_goal", "Delete"),
        Binding("J", "move_down", "Move Down", show=False),
        Binding("K", "move_up", "Move Up", show=False),
    ]

    CSS = """
    WeeklyGoalsSetupModal {
        align: center middle;
    }

    #setup-dialog {
        background: $surface;
        border: thick $primary;
        width: 70;
        height: auto;
        max-height: 90%;
        padding: 1 2;
    }

    #setup-dialog Label {
        margin-top: 1;
    }

    #goals-list {
        height: auto;
        max-height: 12;
        margin-bottom: 1;
        border: solid $primary-lighten-2;
    }

    #goals-list ListItem {
        padding: 0 1;
    }

    #goals-list ListItem.-highlighted {
        background: $accent;
    }

    #keybindings-hint {
        color: $text-muted;
        margin-bottom: 1;
    }

    .metrics-row {
        layout: horizontal;
        height: auto;
        margin-bottom: 1;
    }

    .metrics-row Label {
        width: 28;
        margin-top: 0;
    }

    .metrics-row Input {
        width: 12;
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

    def __init__(
        self,
        week_start: date,
        goals: list[goals_db.Goal],
        metrics: goals_db.WeekMetrics | None,
        *args: Any,
        **kwargs: Any,
    ):
        super().__init__(*args, **kwargs)
        self.week_start = week_start
        self.goals = list(goals)
        self.metrics = metrics
        self._editing_index: int | None = None

    def compose(self) -> ComposeResult:
        week_str = self.week_start.strftime("%b %d, %Y")
        h2_estimate = (
            str(self.metrics.h2_2025_estimate)
            if self.metrics and self.metrics.h2_2025_estimate is not None
            else ""
        )
        predicted = (
            str(self.metrics.predicted_time)
            if self.metrics and self.metrics.predicted_time is not None
            else ""
        )

        with Container(id="setup-dialog"):
            yield Label(f"Weekly Goals - Week of {week_str}", id="title")
            yield Label("Goals:")
            yield ListView(id="goals-list")
            yield Label(
                "[a] Add  [e] Edit  [d] Delete  [J/K] Reorder", id="keybindings-hint"
            )

            with Container(id="edit-container"):
                yield Input(placeholder="Edit goal", id="edit-input")

            with Horizontal(classes="metrics-row"):
                yield Label("H2 2025 estimate (hours):")
                yield Input(
                    value=h2_estimate, placeholder="0.0", id="h2-estimate-input"
                )

            with Horizontal(classes="metrics-row"):
                yield Label("Predicted time (hours):")
                yield Input(value=predicted, placeholder="0.0", id="predicted-input")

            with Horizontal(id="setup-buttons"):
                yield Button("Save", variant="primary", id="save-btn")
                yield Button("Cancel", id="cancel-btn")

    async def on_mount(self) -> None:
        await self._refresh_goals_list()
        goals_list = self.query_one("#goals-list", ListView)
        goals_list.focus()

    async def _refresh_goals_list(self) -> None:
        goals_list = self.query_one("#goals-list", ListView)
        await goals_list.clear()

        if not self.goals:
            goals_list.append(ListItem(Label("No goals yet"), id="empty-placeholder"))
        else:
            for i, goal in enumerate(self.goals):
                content = (
                    goal.content[:55] + "…" if len(goal.content) > 55 else goal.content
                )
                goals_list.append(
                    ListItem(Label(f"{i + 1}. {content}"), id=f"goal-{i}")
                )

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)

    def action_cursor_down(self) -> None:
        goals_list = self.query_one("#goals-list", ListView)
        if goals_list.has_focus and goals_list.index is not None:
            goals_list.index = min(goals_list.index + 1, len(self.goals) - 1)

    def action_cursor_up(self) -> None:
        goals_list = self.query_one("#goals-list", ListView)
        if goals_list.has_focus and goals_list.index is not None:
            goals_list.index = max(goals_list.index - 1, 0)

    def action_add_goal(self) -> None:
        if self._editing_index is not None:
            return
        self._editing_index = -1  # -1 means adding new
        edit_container = self.query_one("#edit-container")
        edit_container.add_class("-visible")
        edit_input = self.query_one("#edit-input", Input)
        edit_input.value = ""
        edit_input.focus()

    def action_edit_goal(self) -> None:
        if self._editing_index is not None or not self.goals:
            return
        goals_list = self.query_one("#goals-list", ListView)
        if goals_list.index is None:
            return
        self._editing_index = goals_list.index
        edit_container = self.query_one("#edit-container")
        edit_container.add_class("-visible")
        edit_input = self.query_one("#edit-input", Input)
        edit_input.value = self.goals[self._editing_index].content
        edit_input.focus()

    async def action_delete_goal(self) -> None:
        if self._editing_index is not None or not self.goals:
            return
        goals_list = self.query_one("#goals-list", ListView)
        if goals_list.index is None:
            return
        del self.goals[goals_list.index]
        await self._refresh_goals_list()
        if self.goals and goals_list.index >= len(self.goals):
            goals_list.index = len(self.goals) - 1

    async def action_move_down(self) -> None:
        if self._editing_index is not None or not self.goals:
            return
        goals_list = self.query_one("#goals-list", ListView)
        if goals_list.index is None or goals_list.index >= len(self.goals) - 1:
            return
        idx = goals_list.index
        self.goals[idx], self.goals[idx + 1] = self.goals[idx + 1], self.goals[idx]
        await self._refresh_goals_list()
        goals_list.index = idx + 1

    async def action_move_up(self) -> None:
        if self._editing_index is not None or not self.goals:
            return
        goals_list = self.query_one("#goals-list", ListView)
        if goals_list.index is None or goals_list.index <= 0:
            return
        idx = goals_list.index
        self.goals[idx], self.goals[idx - 1] = self.goals[idx - 1], self.goals[idx]
        await self._refresh_goals_list()
        goals_list.index = idx - 1

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "edit-input":
            await self._finish_editing()
        elif event.input.id in ("h2-estimate-input", "predicted-input"):
            save_btn = self.query_one("#save-btn", Button)
            self.on_button_pressed(Button.Pressed(save_btn))

    async def _finish_editing(self) -> None:
        if self._editing_index is None:
            return

        edit_input = self.query_one("#edit-input", Input)
        content = edit_input.value.strip()

        if content:
            if self._editing_index == -1:
                # Adding new goal - create a temporary Goal object
                new_goal = goals_db.Goal(
                    id="",  # Will be assigned on save
                    content=content,
                    week_start=self.week_start,
                    is_completed=False,
                    completed_at=None,
                    created_at=__import__("datetime").datetime.now(),
                    sort_order=len(self.goals),
                )
                self.goals.append(new_goal)
            else:
                # Editing existing goal
                goal = self.goals[self._editing_index]
                self.goals[self._editing_index] = goals_db.Goal(
                    id=goal.id,
                    content=content,
                    week_start=goal.week_start,
                    is_completed=goal.is_completed,
                    completed_at=goal.completed_at,
                    created_at=goal.created_at,
                    sort_order=goal.sort_order,
                )

        self._editing_index = None
        edit_container = self.query_one("#edit-container")
        edit_container.remove_class("-visible")
        await self._refresh_goals_list()
        goals_list = self.query_one("#goals-list", ListView)
        goals_list.focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save-btn":
            h2_input = self.query_one("#h2-estimate-input", Input)
            predicted_input = self.query_one("#predicted-input", Input)

            h2_estimate: float | None = None
            predicted: float | None = None

            try:
                if h2_input.value.strip():
                    h2_estimate = float(h2_input.value.strip())
            except ValueError:
                pass

            try:
                if predicted_input.value.strip():
                    predicted = float(predicted_input.value.strip())
            except ValueError:
                pass

            self.dismiss(
                {
                    "week_start": self.week_start,
                    "goals": self.goals,
                    "h2_2025_estimate": h2_estimate,
                    "predicted_time": predicted,
                }
            )
        else:
            self.dismiss(None)


class WeeklyReviewModal(ModalScreen[dict[str, Any] | None]):
    """Modal for reviewing last week's goals and entering actual time."""

    BINDINGS = [
        Binding("escape", "dismiss_modal", "Close"),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("space", "toggle_goal", "Toggle", show=False),
        Binding("enter", "toggle_or_submit", "Toggle/Submit"),
    ]

    CSS = """
    WeeklyReviewModal {
        align: center middle;
    }

    #review-dialog {
        background: $surface;
        border: thick $primary;
        width: 70;
        height: auto;
        max-height: 90%;
        padding: 1 2;
    }

    #review-dialog Label {
        margin-top: 1;
    }

    #review-goals-list {
        height: auto;
        max-height: 12;
        margin-bottom: 1;
        border: solid $primary-lighten-2;
    }

    #review-goals-list ListItem {
        padding: 0 1;
    }

    #review-goals-list ListItem.-highlighted {
        background: $accent;
    }

    #review-keybindings-hint {
        color: $text-muted;
        margin-bottom: 1;
    }

    #estimates-section {
        margin-top: 1;
        margin-bottom: 1;
    }

    #estimates-section Label {
        margin-top: 0;
        color: $text-muted;
    }

    .actual-time-row {
        layout: horizontal;
        height: auto;
        margin-top: 1;
        margin-bottom: 1;
    }

    .actual-time-row Label {
        width: 28;
        margin-top: 0;
    }

    .actual-time-row Input {
        width: 12;
    }

    #review-buttons {
        layout: horizontal;
        align: center middle;
        height: auto;
        margin-top: 1;
    }

    #review-buttons Button {
        margin: 0 1;
    }
    """

    def __init__(
        self,
        week_start: date,
        goals: list[goals_db.Goal],
        metrics: goals_db.WeekMetrics | None,
        *args: Any,
        **kwargs: Any,
    ):
        super().__init__(*args, **kwargs)
        self.week_start = week_start
        self.goals = list(goals)
        self.metrics = metrics
        self._completions: dict[str, bool] = {g.id: g.is_completed for g in goals}

    def compose(self) -> ComposeResult:
        week_str = self.week_start.strftime("%b %d, %Y")
        h2_str = (
            f"{self.metrics.h2_2025_estimate} hours"
            if self.metrics and self.metrics.h2_2025_estimate is not None
            else "not set"
        )
        predicted_str = (
            f"{self.metrics.predicted_time} hours"
            if self.metrics and self.metrics.predicted_time is not None
            else "not set"
        )

        with Container(id="review-dialog"):
            yield Label(f"Weekly Review - Week of {week_str}", id="review-title")
            yield Label("Goals (Space/Enter to toggle):")
            yield ListView(id="review-goals-list")
            yield Label(
                "[j/k] Navigate  [Space/Enter] Toggle", id="review-keybindings-hint"
            )

            with Vertical(id="estimates-section"):
                yield Label("Estimates:")
                yield Label(f"  H2 2025 estimate: {h2_str}")
                yield Label(f"  Predicted time: {predicted_str}")

            with Horizontal(classes="actual-time-row"):
                yield Label("Actual time spent (hours):")
                yield Input(placeholder="0.0", id="actual-time-input")

            with Horizontal(id="review-buttons"):
                yield Button("Done", variant="primary", id="done-btn")
                yield Button("Skip", id="skip-btn")

    async def on_mount(self) -> None:
        await self._refresh_goals_list()
        goals_list = self.query_one("#review-goals-list", ListView)
        goals_list.focus()

    async def _refresh_goals_list(self) -> None:
        goals_list = self.query_one("#review-goals-list", ListView)
        current_index = goals_list.index
        await goals_list.clear()

        if not self.goals:
            goals_list.append(
                ListItem(Label("No goals from last week"), id="empty-placeholder")
            )
        else:
            for i, goal in enumerate(self.goals):
                checkbox = "[x]" if self._completions.get(goal.id, False) else "[ ]"
                content = (
                    goal.content[:50] + "…" if len(goal.content) > 50 else goal.content
                )
                goals_list.append(
                    ListItem(Label(f"{checkbox} {content}"), id=f"review-goal-{i}")
                )

        if current_index is not None and self.goals:
            goals_list.index = min(current_index, len(self.goals) - 1)

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)

    def action_cursor_down(self) -> None:
        goals_list = self.query_one("#review-goals-list", ListView)
        if goals_list.has_focus and goals_list.index is not None:
            goals_list.index = min(goals_list.index + 1, len(self.goals) - 1)

    def action_cursor_up(self) -> None:
        goals_list = self.query_one("#review-goals-list", ListView)
        if goals_list.has_focus and goals_list.index is not None:
            goals_list.index = max(goals_list.index - 1, 0)

    async def action_toggle_goal(self) -> None:
        if not self.goals:
            return
        goals_list = self.query_one("#review-goals-list", ListView)
        if goals_list.index is None:
            return
        goal = self.goals[goals_list.index]
        self._completions[goal.id] = not self._completions.get(goal.id, False)
        await self._refresh_goals_list()

    async def action_toggle_or_submit(self) -> None:
        goals_list = self.query_one("#review-goals-list", ListView)
        if goals_list.has_focus and self.goals:
            await self.action_toggle_goal()
        else:
            done_btn = self.query_one("#done-btn", Button)
            self.on_button_pressed(Button.Pressed(done_btn))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "actual-time-input":
            done_btn = self.query_one("#done-btn", Button)
            self.on_button_pressed(Button.Pressed(done_btn))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "done-btn":
            actual_input = self.query_one("#actual-time-input", Input)

            actual_time: float | None = None
            try:
                if actual_input.value.strip():
                    actual_time = float(actual_input.value.strip())
            except ValueError:
                pass

            self.dismiss(
                {
                    "week_start": self.week_start,
                    "goal_completions": self._completions,
                    "actual_time": actual_time,
                }
            )
        else:
            self.dismiss(None)
