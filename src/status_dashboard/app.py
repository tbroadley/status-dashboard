import asyncio
import json
import logging
import os
import webbrowser

from dotenv import find_dotenv, load_dotenv
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.coordinate import Coordinate
from textual.containers import Container, VerticalScroll
from rich.text import Text
from textual.widgets import DataTable, Footer, Header, Static

from status_dashboard.clients import github, linear, todoist
from status_dashboard.widgets.create_modals import (
    CreateLinearIssueModal,
    CreateTodoistTaskModal,
)

load_dotenv(find_dotenv(usecwd=True))

def _load_hidden_review_requests() -> set[tuple[str, int]]:
    """Load hidden review requests from HIDDEN_REVIEW_REQUESTS env var (JSON array of [repo, pr_number])."""
    raw = os.environ.get("HIDDEN_REVIEW_REQUESTS", "[]")
    try:
        items = json.loads(raw)
        return {(repo, int(pr_num)) for repo, pr_num in items}
    except (json.JSONDecodeError, ValueError, TypeError):
        return set()

HIDDEN_REVIEW_REQUESTS = _load_hidden_review_requests()

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)


class ReviewRequestsDataTable(DataTable):
    """DataTable for review requests with remove reviewer binding."""

    BINDINGS = [
        Binding("x", "remove_self_as_reviewer", "Remove Self"),
    ]


class TodoistDataTable(DataTable):
    """DataTable for Todoist tasks with defer binding."""

    BINDINGS = [
        Binding("n", "app.defer_task", "Defer"),
        Binding("a", "app.create_todoist_task", "Add Task"),
    ]


class LinearDataTable(DataTable):
    """DataTable for Linear issues with state change bindings."""

    BINDINGS = [
        Binding("b", "app.set_linear_state('backlog')", "Backlog"),
        Binding("t", "app.set_linear_state('todo')", "Todo"),
        Binding("p", "app.set_linear_state('in_progress')", "In Progress"),
        Binding("v", "app.set_linear_state('in_review')", "In Review"),
        Binding("d", "app.set_linear_state('done')", "Done"),
        Binding("i", "app.create_linear_issue", "New Issue"),
        Binding("a", "app.assign_self_linear", "Assign Self"),
        Binding("u", "app.unassign_linear", "Unassign"),
    ]


class Panel(Container):
    """A panel with a title and data table."""

    def __init__(self, title: str, panel_id: str, table_class: type[DataTable] = DataTable):
        super().__init__(id=panel_id)
        self.panel_title = title
        self.table_class = table_class

    def compose(self) -> ComposeResult:
        yield Static(self.panel_title, classes="panel-title")
        yield self.table_class(id=f"{self.id}-table")


def _short_repo(repo: str) -> str:
    """Shorten 'METR/some-repo' to 'METR/some-'."""
    parts = repo.split("/")
    if len(parts) == 2:
        org = parts[0][:5]
        name = parts[1][:5]
        return f"{org}/{name}"
    return repo[:11]


class StatusDashboard(App):
    """Terminal dashboard for PRs, Todoist, and Linear."""

    CSS = """
    Screen {
        layout: vertical;
    }

    VerticalScroll {
        height: 1fr;
    }

    Panel {
        border: solid green;
        height: auto;
        min-height: 4;
        margin-bottom: 1;
    }

    #linear {
        height: 1fr;
    }

    .panel-title {
        background: $accent;
        color: $text;
        text-align: center;
        text-style: bold;
        padding: 0 1;
    }

    DataTable {
        height: auto;
        max-height: 10;
        overflow-x: hidden;
    }

    #linear-table {
        height: 1fr;
        max-height: 100%;
        overflow-x: hidden;
    }

    Footer {
        dock: bottom;
    }

    Header {
        dock: top;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("c", "complete_task", "Complete"),
        # Panel focus
        Binding("1", "focus_panel('my-prs')", "My PRs", show=False),
        Binding("2", "focus_panel('review-requests')", "Reviews", show=False),
        Binding("3", "focus_panel('todoist')", "Todoist", show=False),
        Binding("4", "focus_panel('linear')", "Linear", show=False),
    ]

    TITLE = "Status Dashboard"

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(can_focus=False):
            yield Panel("My PRs", "my-prs")
            yield Panel("Review Requests", "review-requests", table_class=ReviewRequestsDataTable)
            yield Panel("Todoist (Today)", "todoist", table_class=TodoistDataTable)
            yield Panel("Linear", "linear", table_class=LinearDataTable)
        yield Footer()

    def _setup_table(self, table: DataTable) -> None:
        """Common table setup."""
        table.cursor_type = "row"
        table.show_cursor = True
        table.zebra_stripes = True

    def on_mount(self) -> None:
        # Set up table columns - auto-sized based on content
        my_prs = self.query_one("#my-prs-table", DataTable)
        my_prs.add_columns("PR", "Title", "Repo", "Status")
        self._setup_table(my_prs)

        reviews = self.query_one("#review-requests-table", DataTable)
        reviews.add_columns("PR", "Title", "Repo", "Author", "Age")
        self._setup_table(reviews)

        todo = self.query_one("#todoist-table", DataTable)
        todo.add_columns("", "Task")
        self._setup_table(todo)

        lin = self.query_one("#linear-table", DataTable)
        lin.add_columns("ID", "Title", "Status", "Owner")
        self._setup_table(lin)

        self.refresh_all()
        self.set_interval(60, self.refresh_all)

    def refresh_all(self) -> None:
        self._refresh_my_prs()
        self._refresh_review_requests()
        self._refresh_todoist()
        self._refresh_linear()

    @work(exclusive=False)
    async def _refresh_my_prs(self) -> None:
        table: DataTable = self.query_one("#my-prs-table", DataTable)
        selected_key = self._get_selected_row_key(table)

        prs = await asyncio.to_thread(github.get_my_prs)
        table.clear()

        for pr in prs:
            if pr.is_draft:
                status = "draft"
            elif pr.is_approved:
                status = "approved"
            elif pr.needs_response:
                status = "needs response"
            elif pr.has_review:
                status = "reviewed"
            else:
                status = "waiting"

            repo = _short_repo(pr.repository)
            table.add_row(
                f"#{pr.number}",
                pr.title,
                repo,
                status,
                key=pr.url,
            )

        self._restore_cursor_by_key(table, selected_key)

    @work(exclusive=False)
    async def _refresh_review_requests(self) -> None:
        table: DataTable = self.query_one("#review-requests-table", DataTable)
        selected_key = self._get_selected_row_key(table)

        prs = await asyncio.to_thread(github.get_review_requests)
        table.clear()

        for pr in prs:
            if (pr.repository, pr.number) in HIDDEN_REVIEW_REQUESTS:
                continue

            repo = _short_repo(pr.repository)
            age = github._relative_time(pr.created_at)
            table.add_row(
                f"#{pr.number}",
                pr.title,
                repo,
                f"@{pr.author}",
                age,
                key=f"review:{pr.repository}:{pr.number}:{pr.url}",
            )

        self._restore_cursor_by_key(table, selected_key)

    def _get_selected_row_key(self, table: DataTable) -> str | None:
        if table.cursor_row is None or table.row_count == 0:
            return None
        cell_key = table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0))
        if cell_key.row_key and cell_key.row_key.value:
            return str(cell_key.row_key.value)
        return None

    def _restore_cursor_by_key(self, table: DataTable, row_key: str | None) -> None:
        if not row_key or table.row_count == 0:
            return
        for idx in range(table.row_count):
            cell_key = table.coordinate_to_cell_key(Coordinate(idx, 0))
            if cell_key.row_key and str(cell_key.row_key.value) == row_key:
                table.move_cursor(row=idx)
                return

    @work(exclusive=False)
    async def _refresh_todoist(self) -> None:
        table: DataTable = self.query_one("#todoist-table", DataTable)
        selected_key = self._get_selected_row_key(table)

        tasks = await asyncio.to_thread(todoist.get_today_tasks)
        table.clear()

        for task in tasks:
            checkbox = "[x]" if task.is_completed else "[ ]"
            content = task.content[:60] + "…" if len(task.content) > 60 else task.content
            table.add_row(
                checkbox,
                content,
                key=f"todoist:{task.id}:{task.url}",
            )

        self._restore_cursor_by_key(table, selected_key)

    @work(exclusive=False)
    async def _refresh_linear(self) -> None:
        table: DataTable = self.query_one("#linear-table", DataTable)
        selected_key = self._get_selected_row_key(table)

        issues = await asyncio.to_thread(linear.get_project_issues)
        table.clear()

        for issue in issues:
            if issue.state in ("Done", "Canceled", "Duplicate"):
                continue

            assignee = issue.assignee_initials or ""
            title = issue.title[:50] + "…" if len(issue.title) > 50 else issue.title
            table.add_row(
                issue.identifier,
                title,
                issue.state,
                assignee,
                key=f"linear:{issue.id}:{issue.team_id}:{issue.url}",
            )

        self._restore_cursor_by_key(table, selected_key)

    def action_refresh(self) -> None:
        self.refresh_all()
        self.notify("Refreshing...")

    def action_focus_panel(self, panel_id: str) -> None:
        table = self.query_one(f"#{panel_id}-table", DataTable)
        table.focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Handle Enter key on a row - open in browser."""
        key = str(event.row_key.value) if event.row_key.value else ""

        # Extract URL from key format
        if key.startswith("todoist:"):
            # Format: "todoist:{task_id}:{url}"
            url = key.split(":", 2)[2]
        elif key.startswith("linear:"):
            # Format: "linear:{issue_id}:{team_id}:{url}"
            url = key.split(":", 3)[3]
        elif key.startswith("review:"):
            # Format: "review:{repo}:{number}:{url}"
            url = key.split(":", 3)[3]
        else:
            # GitHub PRs use URL directly as key
            url = key

        if url:
            webbrowser.open(url)

    def action_complete_task(self) -> None:
        """Complete the selected Todoist task or Linear issue."""
        focused = self.focused
        if not isinstance(focused, DataTable):
            return

        if focused.cursor_row is None or focused.row_count == 0:
            return

        cell_key = focused.coordinate_to_cell_key(Coordinate(focused.cursor_row, 0))
        if not cell_key.row_key or not cell_key.row_key.value:
            return

        key = str(cell_key.row_key.value)

        if focused.id == "todoist-table" and key.startswith("todoist:"):
            # Key format: "todoist:{task_id}:{url}"
            parts = key.split(":", 2)
            if len(parts) >= 2:
                task_id = parts[1]
                self._do_complete_todoist_task(task_id)
        elif focused.id == "linear-table" and key.startswith("linear:"):
            # Key format: "linear:{issue_id}:{team_id}:{url}"
            parts = key.split(":", 3)
            if len(parts) >= 3:
                issue_id = parts[1]
                team_id = parts[2]
                self._do_complete_linear_issue(issue_id, team_id)
        else:
            self.notify("Can only complete Todoist tasks or Linear issues", severity="warning")

    @work(exclusive=False)
    async def _do_complete_todoist_task(self, task_id: str) -> None:
        success = await asyncio.to_thread(todoist.complete_task, task_id)
        if success:
            self.notify("Task completed!")
            self._refresh_todoist()
        else:
            self.notify("Failed to complete task", severity="error")

    def action_defer_task(self) -> None:
        """Defer the selected Todoist task to the next working day."""
        focused = self.focused
        if not isinstance(focused, DataTable):
            return

        if focused.id != "todoist-table":
            self.notify("Can only defer Todoist tasks", severity="warning")
            return

        if focused.cursor_row is None or focused.row_count == 0:
            return

        cell_key = focused.coordinate_to_cell_key(Coordinate(focused.cursor_row, 0))
        if not cell_key.row_key or not cell_key.row_key.value:
            return

        key = str(cell_key.row_key.value)

        if not key.startswith("todoist:"):
            return

        # Key format: "todoist:{task_id}:{url}"
        parts = key.split(":", 2)
        if len(parts) >= 2:
            task_id = parts[1]
            self._do_defer_todoist_task(task_id)

    @work(exclusive=False)
    async def _do_defer_todoist_task(self, task_id: str) -> None:
        success = await asyncio.to_thread(todoist.defer_task, task_id)
        if success:
            self.notify("Task deferred to next working day")
            self._refresh_todoist()
        else:
            self.notify("Failed to defer task", severity="error")

    @work(exclusive=False)
    async def _do_complete_linear_issue(self, issue_id: str, team_id: str) -> None:
        success = await asyncio.to_thread(linear.complete_issue, issue_id, team_id)
        if success:
            self.notify("Issue marked as Done!")
            self._refresh_linear()
        else:
            self.notify("Failed to complete issue", severity="error")

    def action_set_linear_state(self, state: str) -> None:
        """Set the selected Linear issue's state."""
        focused = self.focused
        if not isinstance(focused, DataTable):
            return

        if focused.id != "linear-table":
            self.notify("Can only change state on Linear issues", severity="warning")
            return

        if focused.cursor_row is None or focused.row_count == 0:
            return

        cell_key = focused.coordinate_to_cell_key(Coordinate(focused.cursor_row, 0))
        if not cell_key.row_key or not cell_key.row_key.value:
            return

        key = str(cell_key.row_key.value)

        if not key.startswith("linear:"):
            return

        # Key format: "linear:{issue_id}:{team_id}:{url}"
        parts = key.split(":", 3)
        if len(parts) >= 3:
            issue_id = parts[1]
            team_id = parts[2]
            self._do_set_linear_state(issue_id, team_id, state)

    @work(exclusive=False)
    async def _do_set_linear_state(self, issue_id: str, team_id: str, state: str) -> None:
        state_display = linear.STATE_NAME_MAP.get(state, state)
        success = await asyncio.to_thread(linear.set_issue_state, issue_id, team_id, state)
        if success:
            self.notify(f"Moved to {state_display}")
            self._refresh_linear()
        else:
            self.notify(f"Failed to set state to {state_display}", severity="error")

    def _get_selected_linear_issue_id(self) -> str | None:
        focused = self.focused
        if not isinstance(focused, DataTable) or focused.id != "linear-table":
            return None

        if focused.cursor_row is None or focused.row_count == 0:
            return None

        cell_key = focused.coordinate_to_cell_key(Coordinate(focused.cursor_row, 0))
        if not cell_key.row_key or not cell_key.row_key.value:
            return None

        key = str(cell_key.row_key.value)
        if not key.startswith("linear:"):
            return None

        parts = key.split(":", 3)
        return parts[1] if len(parts) >= 2 else None

    def action_assign_self_linear(self) -> None:
        issue_id = self._get_selected_linear_issue_id()
        if not issue_id:
            self.notify("Select a Linear issue first", severity="warning")
            return
        self._do_assign_linear_issue(issue_id, assign=True)

    def action_unassign_linear(self) -> None:
        issue_id = self._get_selected_linear_issue_id()
        if not issue_id:
            self.notify("Select a Linear issue first", severity="warning")
            return
        self._do_assign_linear_issue(issue_id, assign=False)

    @work(exclusive=False)
    async def _do_assign_linear_issue(self, issue_id: str, assign: bool) -> None:
        if assign:
            viewer_id = await asyncio.to_thread(linear.get_viewer_id)
            if not viewer_id:
                self.notify("Failed to get your user ID", severity="error")
                return
            assignee_id = viewer_id
        else:
            assignee_id = None

        success = await asyncio.to_thread(linear.assign_issue, issue_id, assignee_id)
        if success:
            self.notify("Assigned to you" if assign else "Unassigned")
            self._refresh_linear()
        else:
            self.notify("Failed to update assignment", severity="error")

    def action_remove_self_as_reviewer(self) -> None:
        """Remove yourself as a reviewer from the selected PR."""
        focused = self.focused
        if not isinstance(focused, DataTable):
            return

        if focused.id != "review-requests-table":
            self.notify("Can only remove self from review requests", severity="warning")
            return

        if focused.cursor_row is None or focused.row_count == 0:
            return

        cell_key = focused.coordinate_to_cell_key(Coordinate(focused.cursor_row, 0))
        if not cell_key.row_key or not cell_key.row_key.value:
            return

        key = str(cell_key.row_key.value)

        if not key.startswith("review:"):
            return

        # Key format: "review:{repo}:{number}:{url}"
        parts = key.split(":", 3)
        if len(parts) >= 3:
            repo = parts[1]
            pr_number = int(parts[2])
            self._do_remove_self_as_reviewer(repo, pr_number)

    @work(exclusive=False)
    async def _do_remove_self_as_reviewer(self, repo: str, pr_number: int) -> None:
        success = await asyncio.to_thread(github.remove_self_as_reviewer, repo, pr_number)
        if success:
            self.notify(f"Removed from PR #{pr_number}")
            self._refresh_review_requests()
        else:
            self.notify("Failed to remove self as reviewer", severity="error")

    def action_create_todoist_task(self) -> None:
        """Show modal to create a new Todoist task."""
        self.push_screen(CreateTodoistTaskModal(), self._handle_todoist_task_created)

    def _handle_todoist_task_created(self, result: dict | None) -> None:
        """Handle the result from the Todoist task creation modal."""
        if result:
            content = result["content"]
            due_string = result["due_string"]
            self._do_create_todoist_task(content, due_string)

    @work(exclusive=False)
    async def _do_create_todoist_task(self, content: str, due_string: str) -> None:
        success = await asyncio.to_thread(todoist.create_task, content, due_string)
        if success:
            self.notify("Task created!")
            self._refresh_todoist()
        else:
            self.notify("Failed to create task", severity="error")

    def action_create_linear_issue(self) -> None:
        """Show modal to create a new Linear issue."""
        # First, get team ID and members
        self._prepare_linear_issue_modal()

    @work(exclusive=False)
    async def _prepare_linear_issue_modal(self) -> None:
        """Load team data and show the Linear issue creation modal."""
        # Get team ID
        team_id = await asyncio.to_thread(linear.get_team_id)
        if not team_id:
            self.notify("Failed to get team ID", severity="error")
            return

        # Get team members
        team_members = await asyncio.to_thread(linear.get_team_members)

        # Store team_id for later use
        self._linear_team_id = team_id

        # Show modal
        self.push_screen(
            CreateLinearIssueModal(team_members),
            self._handle_linear_issue_created,
        )

    def _handle_linear_issue_created(self, result: dict | None) -> None:
        """Handle the result from the Linear issue creation modal."""
        if result:
            title = result["title"]
            state = result["state"]
            assignee_id = result.get("assignee_id")
            self._do_create_linear_issue(title, state, assignee_id)

    @work(exclusive=False)
    async def _do_create_linear_issue(
        self, title: str, state: str, assignee_id: str | None
    ) -> None:
        team_id = getattr(self, "_linear_team_id", None)
        if not team_id:
            self.notify("Team ID not available", severity="error")
            return

        success = await asyncio.to_thread(
            linear.create_issue, title, team_id, state, assignee_id
        )
        if success:
            self.notify("Issue created!")
            self._refresh_linear()
        else:
            self.notify("Failed to create issue", severity="error")


def main():
    app = StatusDashboard()
    app.run()


if __name__ == "__main__":
    main()
