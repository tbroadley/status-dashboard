"""Modal dialogs for creating Todoist tasks and Linear issues."""

from typing import Any

from textual.app import ComposeResult
from textual.containers import Container, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select


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
                    ("Next Monday", "next monday"),
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
