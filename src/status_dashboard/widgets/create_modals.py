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
        _ = self.dismiss(None)

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
                _ = content_input.focus()
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

            _ = self.dismiss(result)
        else:
            _ = self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle Enter key in the title input."""
        if event.input.id == "content-input":
            description_input = self.query_one("#description-input")
            _ = description_input.focus()
        elif event.input.id == "due-input":
            save_btn = self.query_one("#save-btn", Button)
            self.on_button_pressed(Button.Pressed(save_btn))


class CreateGoalModal(ModalScreen[dict[str, str] | None]):
    """Modal for creating a new weekly goal."""

    BINDINGS = [("escape", "dismiss_modal", "Close")]

    def action_dismiss_modal(self) -> None:
        _ = self.dismiss(None)

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
                _ = self.dismiss({"content": content})
            else:
                _ = goal_input.focus()
        else:
            _ = self.dismiss(None)

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
        width: 90;
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

    #estimate-inputs-container {
        height: auto;
        max-height: 10;
        margin-bottom: 1;
    }

    .estimate-row {
        layout: horizontal;
        height: 3;
        padding: 0 1;
    }

    .estimate-label {
        width: 1fr;
    }

    .estimate-input {
        width: 10;
        margin-left: 1;
    }

    #totals-row {
        layout: horizontal;
        height: auto;
        padding: 0 1;
        margin-bottom: 1;
        background: $surface-darken-1;
    }

    #totals-row Label {
        margin-top: 0;
        margin-right: 2;
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

        with Container(id="setup-dialog"):
            yield Label(f"Weekly Goals - Week of {week_str}", id="setup-title")
            yield Label("Goals:")
            yield ListView(id="goals-list")
            yield Label(
                "[a] Add  [e] Edit  [d] Delete  [J/K] Reorder", id="keybindings-hint"
            )

            with Container(id="edit-container"):
                yield Input(placeholder="Edit goal", id="edit-input")

            yield Label("Time estimates (per goal):")
            yield Vertical(id="estimate-inputs-container")

            with Horizontal(id="totals-row"):
                yield Label("Totals:", id="totals-label")
                yield Label("H2: 0.0h", id="total-h2")
                yield Label("Pred: 0.0h", id="total-pred")

            with Horizontal(id="setup-buttons"):
                yield Button("Save", variant="primary", id="save-btn")
                yield Button("Cancel", id="cancel-btn")

    def on_mount(self) -> None:
        self._refresh_goals_list()
        self._refresh_estimate_inputs()
        goals_list = self.query_one("#goals-list", ListView)
        _ = goals_list.focus()

    def _refresh_goals_list(self) -> None:
        goals_list = self.query_one("#goals-list", ListView)
        _ = goals_list.clear()

        if not self.goals:
            _ = goals_list.append(
                ListItem(Label("No goals yet"), id="setup-empty-placeholder")
            )
        else:
            for i, goal in enumerate(self.goals):
                content = (
                    goal.content[:55] + "…" if len(goal.content) > 55 else goal.content
                )
                _ = goals_list.append(
                    ListItem(Label(f"{i + 1}. {content}"), id=f"setup-goal-{i}")
                )

    def _refresh_estimate_inputs(self) -> None:
        container = self.query_one("#estimate-inputs-container", Vertical)
        _ = container.remove_children()

        if not self.goals:
            _ = container.mount(Label("No goals to estimate", classes="estimate-label"))
        else:
            for i, goal in enumerate(self.goals):
                content = (
                    goal.content[:35] + "…" if len(goal.content) > 35 else goal.content
                )
                h2_val = (
                    str(goal.h2_2025_estimate)
                    if goal.h2_2025_estimate is not None
                    else ""
                )
                pred_val = (
                    str(goal.predicted_time) if goal.predicted_time is not None else ""
                )

                row = Horizontal(classes="estimate-row", id=f"estimate-row-{i}")
                _ = container.mount(row)
                _ = row.mount(Label(f"{i + 1}. {content}", classes="estimate-label"))
                _ = row.mount(
                    Input(
                        value=h2_val,
                        placeholder="H2",
                        classes="estimate-input",
                        id=f"h2-{i}",
                    )
                )
                _ = row.mount(
                    Input(
                        value=pred_val,
                        placeholder="Pred",
                        classes="estimate-input",
                        id=f"pred-{i}",
                    )
                )

        self._update_totals()

    def _update_totals(self) -> None:
        total_h2 = 0.0
        total_pred = 0.0

        for i, goal in enumerate(self.goals):
            if goal.is_abandoned:
                continue
            try:
                h2_input = self.query_one(f"#h2-{i}", Input)
                if h2_input.value.strip():
                    total_h2 += float(h2_input.value.strip())
            except Exception:
                if goal.h2_2025_estimate:
                    total_h2 += goal.h2_2025_estimate
            try:
                pred_input = self.query_one(f"#pred-{i}", Input)
                if pred_input.value.strip():
                    total_pred += float(pred_input.value.strip())
            except Exception:
                if goal.predicted_time:
                    total_pred += goal.predicted_time

        try:
            self.query_one("#total-h2", Label).update(f"H2: {total_h2:.1f}h")
            self.query_one("#total-pred", Label).update(f"Pred: {total_pred:.1f}h")
        except Exception:
            pass

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id and (
            event.input.id.startswith("h2-") or event.input.id.startswith("pred-")
        ):
            self._update_totals()

    def action_dismiss_modal(self) -> None:
        _ = self.dismiss(None)

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
        _ = edit_container.add_class("-visible")
        edit_input = self.query_one("#edit-input", Input)
        edit_input.value = ""
        _ = edit_input.focus()

    def action_edit_goal(self) -> None:
        if self._editing_index is not None or not self.goals:
            return
        goals_list = self.query_one("#goals-list", ListView)
        if goals_list.index is None:
            return
        self._editing_index = goals_list.index
        edit_container = self.query_one("#edit-container")
        _ = edit_container.add_class("-visible")
        edit_input = self.query_one("#edit-input", Input)
        edit_input.value = self.goals[self._editing_index].content
        _ = edit_input.focus()

    def action_delete_goal(self) -> None:
        if self._editing_index is not None or not self.goals:
            return
        goals_list = self.query_one("#goals-list", ListView)
        if goals_list.index is None:
            return
        del self.goals[goals_list.index]
        self._refresh_goals_list()
        self._refresh_estimate_inputs()
        if self.goals and goals_list.index >= len(self.goals):
            goals_list.index = len(self.goals) - 1

    def action_move_down(self) -> None:
        if self._editing_index is not None or not self.goals:
            return
        goals_list = self.query_one("#goals-list", ListView)
        if goals_list.index is None or goals_list.index >= len(self.goals) - 1:
            return
        idx = goals_list.index
        self.goals[idx], self.goals[idx + 1] = self.goals[idx + 1], self.goals[idx]
        self._refresh_goals_list()
        self._refresh_estimate_inputs()
        goals_list.index = idx + 1

    def action_move_up(self) -> None:
        if self._editing_index is not None or not self.goals:
            return
        goals_list = self.query_one("#goals-list", ListView)
        if goals_list.index is None or goals_list.index <= 0:
            return
        idx = goals_list.index
        self.goals[idx], self.goals[idx - 1] = self.goals[idx - 1], self.goals[idx]
        self._refresh_goals_list()
        self._refresh_estimate_inputs()
        goals_list.index = idx - 1

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "edit-input":
            self._finish_editing()

    def _finish_editing(self) -> None:
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
                    is_abandoned=False,
                    completed_at=None,
                    abandoned_at=None,
                    created_at=__import__("datetime").datetime.now(),
                    sort_order=len(self.goals),
                )
                self.goals.append(new_goal)
            else:
                # Editing existing goal - preserve all fields including estimates
                goal = self.goals[self._editing_index]
                self.goals[self._editing_index] = goals_db.Goal(
                    id=goal.id,
                    content=content,
                    week_start=goal.week_start,
                    is_completed=goal.is_completed,
                    is_abandoned=goal.is_abandoned,
                    completed_at=goal.completed_at,
                    abandoned_at=goal.abandoned_at,
                    created_at=goal.created_at,
                    sort_order=goal.sort_order,
                    h2_2025_estimate=goal.h2_2025_estimate,
                    predicted_time=goal.predicted_time,
                    actual_time=goal.actual_time,
                )

        self._editing_index = None
        edit_container = self.query_one("#edit-container")
        _ = edit_container.remove_class("-visible")
        self._refresh_goals_list()
        self._refresh_estimate_inputs()
        goals_list = self.query_one("#goals-list", ListView)
        _ = goals_list.focus()

    def _collect_estimates_from_inputs(self) -> None:
        """Collect per-goal estimates from input fields into self.goals."""
        for i, goal in enumerate(self.goals):
            h2_val: float | None = None
            pred_val: float | None = None

            try:
                h2_input = self.query_one(f"#h2-{i}", Input)
                if h2_input.value.strip():
                    h2_val = float(h2_input.value.strip())
            except Exception:
                pass

            try:
                pred_input = self.query_one(f"#pred-{i}", Input)
                if pred_input.value.strip():
                    pred_val = float(pred_input.value.strip())
            except Exception:
                pass

            # Update goal with estimates
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

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save-btn":
            self._collect_estimates_from_inputs()
            _ = self.dismiss(
                {
                    "week_start": self.week_start,
                    "goals": self.goals,
                }
            )
        else:
            _ = self.dismiss(None)


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
        width: 90;
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

    #actual-times-container {
        height: auto;
        max-height: 10;
        margin-bottom: 1;
    }

    .actual-row {
        layout: horizontal;
        height: 3;
        padding: 0 1;
    }

    .actual-label {
        width: 1fr;
    }

    .actual-estimates {
        width: 18;
        color: $text-muted;
    }

    .actual-input {
        width: 10;
        margin-left: 1;
    }

    #review-totals-row {
        layout: horizontal;
        height: auto;
        padding: 0 1;
        margin-bottom: 1;
        background: $surface-darken-1;
    }

    #review-totals-row Label {
        margin-top: 0;
        margin-right: 2;
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

        with Container(id="review-dialog"):
            yield Label(f"Weekly Review - Week of {week_str}", id="review-title")
            yield Label("Goals (Space/Enter to toggle completion):")
            yield ListView(id="review-goals-list")
            yield Label(
                "[j/k] Navigate  [Space/Enter] Toggle", id="review-keybindings-hint"
            )

            yield Label("Actual time spent (per goal):")
            yield Vertical(id="actual-times-container")

            with Horizontal(id="review-totals-row"):
                yield Label("Totals:", id="review-totals-label")
                yield Label("H2: 0.0h", id="review-total-h2")
                yield Label("Pred: 0.0h", id="review-total-pred")
                yield Label("Actual: 0.0h", id="review-total-actual")

            with Horizontal(id="review-buttons"):
                yield Button("Done", variant="primary", id="done-btn")
                yield Button("Skip", id="skip-btn")

    async def on_mount(self) -> None:
        await self._refresh_goals_list()
        self._refresh_actual_inputs()
        goals_list = self.query_one("#review-goals-list", ListView)
        _ = goals_list.focus()

    async def _refresh_goals_list(self) -> None:
        goals_list = self.query_one("#review-goals-list", ListView)
        current_index = goals_list.index
        await goals_list.clear()

        if not self.goals:
            _ = goals_list.append(
                ListItem(
                    Label("No goals from last week"), id="review-empty-placeholder"
                )
            )
        else:
            from rich.text import Text

            for i, goal in enumerate(self.goals):
                checkbox = "[x]" if self._completions.get(goal.id, False) else "[ ]"
                content = (
                    goal.content[:45] + "…" if len(goal.content) > 45 else goal.content
                )
                if goal.is_abandoned:
                    text = Text(f"{checkbox} {content}", style="strike dim")
                else:
                    text = Text(f"{checkbox} {content}")
                _ = goals_list.append(ListItem(Label(text), id=f"review-goal-{i}"))

        if current_index is not None and self.goals:
            goals_list.index = min(current_index, len(self.goals) - 1)

    def _refresh_actual_inputs(self) -> None:
        container = self.query_one("#actual-times-container", Vertical)
        _ = container.remove_children()

        if not self.goals:
            _ = container.mount(Label("No goals to review", classes="actual-label"))
        else:
            for i, goal in enumerate(self.goals):
                content = (
                    goal.content[:30] + "…" if len(goal.content) > 30 else goal.content
                )

                # Build estimates string
                estimates_parts = []
                if goal.h2_2025_estimate:
                    estimates_parts.append(f"H2:{goal.h2_2025_estimate:.1f}")
                if goal.predicted_time:
                    estimates_parts.append(f"P:{goal.predicted_time:.1f}")
                estimates_str = " ".join(estimates_parts) if estimates_parts else "-"

                actual_val = (
                    str(goal.actual_time) if goal.actual_time is not None else ""
                )

                row = Horizontal(classes="actual-row", id=f"actual-row-{i}")
                _ = container.mount(row)
                _ = row.mount(Label(f"{i + 1}. {content}", classes="actual-label"))
                _ = row.mount(Label(estimates_str, classes="actual-estimates"))
                _ = row.mount(
                    Input(
                        value=actual_val,
                        placeholder="Actual",
                        classes="actual-input",
                        id=f"actual-{i}",
                    )
                )

        self._update_totals()

    def _update_totals(self) -> None:
        total_h2 = sum(
            g.h2_2025_estimate or 0 for g in self.goals if not g.is_abandoned
        )
        total_pred = sum(
            g.predicted_time or 0 for g in self.goals if not g.is_abandoned
        )

        total_actual = 0.0
        for i, goal in enumerate(self.goals):
            if goal.is_abandoned:
                continue
            try:
                actual_input = self.query_one(f"#actual-{i}", Input)
                if actual_input.value.strip():
                    total_actual += float(actual_input.value.strip())
            except Exception:
                if goal.actual_time:
                    total_actual += goal.actual_time

        try:
            self.query_one("#review-total-h2", Label).update(f"H2: {total_h2:.1f}h")
            self.query_one("#review-total-pred", Label).update(
                f"Pred: {total_pred:.1f}h"
            )
            self.query_one("#review-total-actual", Label).update(
                f"Actual: {total_actual:.1f}h"
            )
        except Exception:
            pass

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id and event.input.id.startswith("actual-"):
            self._update_totals()

    def action_dismiss_modal(self) -> None:
        _ = self.dismiss(None)

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
        # Don't allow toggling abandoned goals
        if goal.is_abandoned:
            return
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
        # Move to next input or submit
        done_btn = self.query_one("#done-btn", Button)
        self.on_button_pressed(Button.Pressed(done_btn))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "done-btn":
            # Collect per-goal actual times
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
