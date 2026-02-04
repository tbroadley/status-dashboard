import asyncio
import json
import logging
import logging.handlers
import os
import re
import sys
import webbrowser
from collections import defaultdict
from datetime import date, timedelta
from importlib import metadata
import uuid
from itertools import groupby
from pathlib import Path

from dotenv import find_dotenv, load_dotenv
import httpx
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, VerticalScroll
from textual.coordinate import Coordinate
from textual.widgets import DataTable, Footer as TextualFooter, Static
from textual.widgets._footer import FooterKey, FooterLabel, KeyGroup

from status_dashboard.clients import github, linear, todoist
from status_dashboard.db import goals as goals_db
from status_dashboard.undo import (
    GoalAbandonAction,
    GoalCompleteAction,
    LinearAssignAction,
    LinearMoveAction,
    LinearSetStateAction,
    TodoistCompleteAction,
    TodoistDeferAction,
    TodoistMoveAction,
    UndoStack,
)
from status_dashboard.widgets.create_modals import (
    CreateGoalModal,
    CreateLinearIssueModal,
    CreateTodoistTaskModal,
    EditTodoistTaskModal,
    WeeklyGoalsSetupModal,
    WeeklyReviewModal,
)


def _get_config_dir() -> Path:
    """Get the config directory, following XDG conventions."""
    xdg_config = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg_config) if xdg_config else Path.home() / ".config"
    return base / "status-dashboard"


# Load .env from XDG config directory, falling back to cwd for development
_config_env = _get_config_dir() / ".env"
if _config_env.exists():
    load_dotenv(_config_env)
else:
    load_dotenv(find_dotenv(usecwd=True))


def _load_hidden_review_requests() -> set[tuple[str, int]]:
    """Load hidden review requests from HIDDEN_REVIEW_REQUESTS env var (JSON array of [repo, pr_number])."""
    raw = os.environ.get("HIDDEN_REVIEW_REQUESTS", "[]")
    try:
        items: list[list[str | int]] = json.loads(raw)
        return {(str(repo), int(pr_num)) for repo, pr_num in items}
    except (json.JSONDecodeError, ValueError, TypeError):
        return set()


HIDDEN_REVIEW_REQUESTS = _load_hidden_review_requests()

# Shorten Linear state display names to fit column width
LINEAR_STATE_SHORT = {
    "In Progress": "progress",
    "In Review": "review",
}


def _load_blocked_review_teams() -> set[str]:
    """Load blocked teams from BLOCKED_REVIEW_TEAMS env var (JSON array of team slugs)."""
    raw = os.environ.get("BLOCKED_REVIEW_TEAMS", "[]")
    try:
        teams: list[str] = json.loads(raw)
        return {str(team) for team in teams}
    except (json.JSONDecodeError, ValueError, TypeError):
        return set()


BLOCKED_REVIEW_TEAMS = _load_blocked_review_teams()


def _setup_logging() -> None:
    """Configure logging to stderr and a rotating log file."""
    xdg_state = os.environ.get("XDG_STATE_HOME")
    state_base = Path(xdg_state) if xdg_state else Path.home() / ".local" / "state"
    log_dir = state_base / "status-dashboard"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "status-dashboard.log"

    log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    formatter = logging.Formatter(log_format)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.WARNING)

    stderr_handler = logging.StreamHandler()
    stderr_handler.setFormatter(formatter)
    root_logger.addHandler(stderr_handler)

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=1_000_000, backupCount=3
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    # Silence noisy third-party libraries
    for name in ("httpx", "httpcore", "hpack"):
        logging.getLogger(name).setLevel(logging.WARNING)


_setup_logging()

_logger = logging.getLogger(__name__)


def _get_current_version() -> str | None:
    """Get the currently installed version of status-dashboard."""
    try:
        return metadata.version("status-dashboard")
    except metadata.PackageNotFoundError:
        return None


def _get_remote_version() -> str | None:
    """Fetch the latest version from the GitHub repository."""
    url = "https://raw.githubusercontent.com/tbroadley/status-dashboard/main/pyproject.toml"
    try:
        resp = httpx.get(url, timeout=10, follow_redirects=True)
        _ = resp.raise_for_status()
        content = resp.text
        for line in content.splitlines():
            if line.startswith("version"):
                # Parse: version = "0.1.0"
                match = re.search(r'version\s*=\s*["\']([^"\']+)["\']', line)
                if match:
                    return match.group(1)
        return None
    except (httpx.HTTPError, httpx.TimeoutException) as e:
        _logger.warning(f"Failed to fetch remote version: {e}")
        return None


def _version_tuple(v: str) -> tuple[int, ...]:
    """Convert version string to tuple for comparison."""
    return tuple(int(x) for x in v.split(".") if x.isdigit())


class Footer(TextualFooter):
    """Custom Footer that shows global bindings before pane-specific ones."""

    def compose(self):
        if not self._bindings_ready:
            return
        active_bindings = self.screen.active_bindings

        def sort_key(item):
            node = item[1][0]
            return 0 if isinstance(node, StatusDashboard) else 1

        sorted_items = sorted(active_bindings.items(), key=sort_key)
        bindings = [
            (binding, enabled, tooltip)
            for (_, binding, enabled, tooltip) in (v for _, v in sorted_items)
            if binding.show
        ]
        action_to_bindings: defaultdict[str, list[tuple]] = defaultdict(list)
        for binding, enabled, tooltip in bindings:
            action_to_bindings[binding.action].append((binding, enabled, tooltip))

        num_bindings = len(action_to_bindings)
        self.styles.grid_size_columns = (num_bindings + 1) // 2
        self.styles.grid_size_rows = 2

        for group, multi_bindings_iterable in groupby(
            action_to_bindings.values(),
            lambda multi_bindings_: multi_bindings_[0][0].group,
        ):
            multi_bindings = list(multi_bindings_iterable)
            if group is not None and len(multi_bindings) > 1:
                with KeyGroup(classes="-compact" if group.compact else ""):
                    for multi_bindings in multi_bindings:
                        binding, enabled, tooltip = multi_bindings[0]
                        yield FooterKey(
                            binding.key,
                            self.app.get_key_display(binding),
                            "",
                            binding.action,
                            disabled=not enabled,
                            tooltip=tooltip or binding.description,
                            classes="-grouped",
                        ).data_bind(compact=TextualFooter.compact)
                yield FooterLabel(group.description)
            else:
                for multi_bindings in multi_bindings:
                    binding, enabled, tooltip = multi_bindings[0]
                    yield FooterKey(
                        binding.key,
                        self.app.get_key_display(binding),
                        binding.description,
                        binding.action,
                        disabled=not enabled,
                        tooltip=tooltip,
                    ).data_bind(compact=TextualFooter.compact)
        if self.show_command_palette and self.app.ENABLE_COMMAND_PALETTE:
            try:
                _node, binding, enabled, tooltip = active_bindings[
                    self.app.COMMAND_PALETTE_BINDING
                ]
            except KeyError:
                pass
            else:
                yield FooterKey(
                    binding.key,
                    self.app.get_key_display(binding),
                    binding.description,
                    binding.action,
                    classes="-command-palette",
                    disabled=not enabled,
                    tooltip=binding.tooltip or binding.description,
                )


class VimDataTable(DataTable):
    """DataTable with vim-style navigation (j/k/g/G) and count prefixes (e.g., 5j)."""

    BINDINGS = [
        Binding("g", "cursor_top", "Top", show=False),
        Binding("G", "cursor_bottom", "Bottom", show=False),
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._vim_count = ""

    def _get_and_reset_count(self) -> int:
        count = int(self._vim_count) if self._vim_count else 1
        self._vim_count = ""
        return count

    def key_j(self) -> None:
        count = self._get_and_reset_count()
        for _ in range(count):
            # If at bottom row, move to next panel
            if self.cursor_row is not None and self.cursor_row >= self.row_count - 1:
                self.app.action_focus_next()
                return
            self.action_cursor_down()

    def key_k(self) -> None:
        count = self._get_and_reset_count()
        for _ in range(count):
            # If at top row, move to previous panel
            if self.cursor_row is not None and self.cursor_row <= 0:
                self.app.action_focus_previous()
                return
            self.action_cursor_up()

    def key_0(self) -> None:
        if self._vim_count:
            self._vim_count += "0"
        else:
            self._vim_count = ""

    def key_1(self) -> None:
        self._vim_count += "1"

    def key_2(self) -> None:
        self._vim_count += "2"

    def key_3(self) -> None:
        self._vim_count += "3"

    def key_4(self) -> None:
        self._vim_count += "4"

    def key_5(self) -> None:
        self._vim_count += "5"

    def key_6(self) -> None:
        self._vim_count += "6"

    def key_7(self) -> None:
        self._vim_count += "7"

    def key_8(self) -> None:
        self._vim_count += "8"

    def key_9(self) -> None:
        self._vim_count += "9"

    def action_cursor_top(self) -> None:
        self._vim_count = ""
        if self.row_count > 0:
            self.move_cursor(row=0)

    def action_cursor_bottom(self) -> None:
        self._vim_count = ""
        if self.row_count > 0:
            self.move_cursor(row=self.row_count - 1)

    def watch_cursor_coordinate(
        self, old_coordinate: Coordinate, new_coordinate: Coordinate
    ) -> None:
        super().watch_cursor_coordinate(old_coordinate, new_coordinate)
        self._update_relative_line_numbers()

    def _update_relative_line_numbers(self) -> None:
        if self.row_count == 0:
            return
        cursor_row = self.cursor_row or 0
        for row_idx in range(self.row_count):
            distance = abs(row_idx - cursor_row)
            label = "" if distance == 0 else str(distance)
            self.update_cell_at(Coordinate(row_idx, 0), label)

    def refresh_line_numbers(self) -> None:
        self._update_relative_line_numbers()


class ReviewRequestsDataTable(VimDataTable):
    """DataTable for review requests with remove reviewer binding."""

    BINDINGS = [
        Binding("x", "remove_self_as_reviewer", "Remove Self"),
        Binding("c", "app.copy_pr_link", "Copy Link"),
    ]


class NotificationsDataTable(VimDataTable):
    """DataTable for GitHub notifications with mark as read binding."""

    BINDINGS = [
        Binding("x", "app.mark_notification_read", "Mark Read"),
        Binding("c", "app.copy_pr_link", "Copy Link"),
    ]


class MyPRsDataTable(VimDataTable):
    """DataTable for user's PRs with merge binding."""

    BINDINGS = [
        Binding("m", "app.merge_pr", "Merge"),
        Binding("c", "app.copy_pr_link", "Copy Link"),
    ]


class TodoistDataTable(VimDataTable):
    """DataTable for Todoist tasks with defer binding."""

    BINDINGS = [
        Binding("T", "app.reschedule_overdue_to_today", "Reschedule All"),
        Binding("a", "app.create_todoist_task", "Add Task"),
        Binding("e", "app.edit_todoist_task", "Edit"),
        Binding("c", "app.complete_task", "Complete"),
        Binding("n", "app.defer_task", "Defer"),
        Binding("d", "app.delete_task", "Delete"),
        Binding("o", "app.open_task_link", "Open Link"),
        Binding("ctrl+enter", "app.open_todoist_in_browser", "Open in Browser"),
        Binding("J", "app.move_task_down", "Move Down"),
        Binding("K", "app.move_task_up", "Move Up"),
        Binding("shift+down", "app.move_task_down", "Move Down", show=False),
        Binding("shift+up", "app.move_task_up", "Move Up", show=False),
        Binding("h", "app.todoist_previous_day", "Prev Day"),
        Binding("l", "app.todoist_next_day", "Next Day"),
        Binding("left", "app.todoist_previous_day", "Prev Day", show=False),
        Binding("right", "app.todoist_next_day", "Next Day", show=False),
    ]


class LinearDataTable(VimDataTable):
    """DataTable for Linear issues with state change bindings."""

    BINDINGS = [
        Binding("i", "app.create_linear_issue", "New Issue"),
        Binding("a", "app.assign_self_linear", "Assign Self"),
        Binding("u", "app.unassign_linear", "Unassign"),
        Binding("c", "app.complete_task", "Complete"),
        Binding("b", "app.set_linear_state('backlog')", "Backlog"),
        Binding("t", "app.set_linear_state('todo')", "Todo"),
        Binding("p", "app.set_linear_state('in_progress')", "In Progress"),
        Binding("v", "app.set_linear_state('in_review')", "In Review"),
        Binding("d", "app.set_linear_state('done')", "Done"),
        Binding("J", "app.move_linear_issue_down", "Move Down", show=False),
        Binding("K", "app.move_linear_issue_up", "Move Up", show=False),
        Binding("shift+down", "app.move_linear_issue_down", "Move Down"),
        Binding("shift+up", "app.move_linear_issue_up", "Move Up"),
    ]


class GoalsDataTable(VimDataTable):
    """DataTable for weekly goals."""

    BINDINGS = [
        Binding("a", "app.create_goal", "Add Goal"),
        Binding("c", "app.complete_goal", "Complete"),
        Binding("x", "app.abandon_goal", "Abandon"),
        Binding("d", "app.delete_goal", "Delete"),
        Binding("e", "app.open_goals_setup", "Edit Week"),
        Binding("J", "app.move_goal_down", "Move Down", show=False),
        Binding("K", "app.move_goal_up", "Move Up", show=False),
        Binding("shift+down", "app.move_goal_down", "Move Down"),
        Binding("shift+up", "app.move_goal_up", "Move Up"),
    ]


class UpdateBanner(Static):
    """Banner showing when a new version is available."""

    DEFAULT_CSS = """
    UpdateBanner {
        background: $warning;
        color: $text;
        text-align: center;
        padding: 0 1;
        display: none;
    }

    UpdateBanner.-visible {
        display: block;
    }
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._new_version: str | None = None

    def show_update(self, new_version: str) -> None:
        self._new_version = new_version
        self.update(f"New version available: v{new_version} (press R to upgrade)")
        _ = self.add_class("-visible")

    def hide(self) -> None:
        _ = self.remove_class("-visible")


class Panel(Container):
    """A panel with a title and data table."""

    def __init__(
        self, title: str, panel_id: str, table_class: type[DataTable] = VimDataTable
    ):
        super().__init__(id=panel_id)
        self.panel_title = title
        self.table_class = table_class

    def compose(self) -> ComposeResult:
        yield Static(self.panel_title, classes="panel-title")
        yield self.table_class(id=f"{self.id}-table")


def _short_repo(repo: str) -> str:
    """Shorten 'METR/some-repo' to 'METR/some-repo' (truncated)."""
    parts = repo.split("/")
    if len(parts) == 2:
        org = parts[0][:6]
        name = parts[1][:12]
        return f"{org}/{name}"
    return repo[:11]


class StatusDashboard(App):
    """Terminal dashboard for PRs, Todoist, and Linear."""

    _goals: list[goals_db.Goal] = []  # pyright: ignore[reportUninitializedInstanceVariable]
    _goals_showing_review: bool = False  # pyright: ignore[reportUninitializedInstanceVariable]
    _goals_review_dismissed: bool = False  # pyright: ignore[reportUninitializedInstanceVariable]
    _goals_week_metrics: goals_db.WeekMetrics | None = None  # pyright: ignore[reportUninitializedInstanceVariable]
    _gh_notifications: list[github.Notification] = []  # pyright: ignore[reportUninitializedInstanceVariable]

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

    #goals {
        height: auto;
        max-height: 8;
    }

    #goals-table {
        height: auto;
        max-height: 5;
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
        height: 2;
        layout: grid;
        grid-gutter: 0 1;
    }

    FooterKey {
        height: 1;
    }

    """

    BINDINGS = [
        Binding("r", "refresh", "Refresh"),
        Binding("z", "undo", "Undo"),
        Binding("R", "restart", "Restart"),
        Binding("q", "quit", "Quit"),
        Binding("ctrl+shift+up", "focus_previous_pane", "Prev Pane", show=False),
        Binding("ctrl+shift+down", "focus_next_pane", "Next Pane", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield UpdateBanner(id="update-banner")
        yield Panel("Weekly Goals", "goals", table_class=GoalsDataTable)
        with VerticalScroll(can_focus=False):
            yield Panel("My PRs", "my-prs", table_class=MyPRsDataTable)
            yield Panel(
                "Review Requests",
                "review-requests",
                table_class=ReviewRequestsDataTable,
            )
            yield Panel(
                "Notifications",
                "notifications",
                table_class=NotificationsDataTable,
            )
            yield Panel("Todoist (Today)", "todoist", table_class=TodoistDataTable)
            yield Panel("Linear", "linear", table_class=LinearDataTable)
        yield Footer()

    def _setup_table(self, table: DataTable) -> None:
        """Common table setup."""
        table.cursor_type = "row"
        table.show_cursor = True
        table.zebra_stripes = True

    def on_mount(self) -> None:
        self._undo_stack = UndoStack()
        self._my_prs: list[github.PullRequest] = []
        self._todoist_tasks: list[todoist.Task] = []
        self._todoist_pending_orders: dict[str, int] | None = None
        self._todoist_debounce_handle: object | None = None
        self._todoist_restore_key: str | None = None
        self._todoist_selected_date: date = date.today()
        self._todoist_optimistic_tasks: dict[str, todoist.Task] = {}
        self._linear_issues: list[linear.Issue] = []
        self._linear_debounce_handle: object | None = None
        self._gh_notifications: list[github.Notification] = []
        self._goals: list[goals_db.Goal] = []
        self._goals_showing_review: bool = False
        self._goals_review_dismissed: bool = False
        self._goals_week_metrics: goals_db.WeekMetrics | None = None

        # Set up goals table
        goals_table = self.query_one("#goals-table", DataTable)
        _ = goals_table.add_columns("#", "", "Goal")
        self._setup_table(goals_table)

        # Set up table columns - auto-sized based on content
        my_prs = self.query_one("#my-prs-table", DataTable)
        _ = my_prs.add_columns("#", "PR", "Title", "Repo", "Status", "CI", "Cmt")
        self._setup_table(my_prs)

        reviews = self.query_one("#review-requests-table", DataTable)
        _ = reviews.add_columns("#", "PR", "Title", "Repo", "Author", "Age", "Reviewed")
        self._setup_table(reviews)

        notifs = self.query_one("#notifications-table", DataTable)
        _ = notifs.add_columns("#", "PR", "Title", "Repo", "Reason", "Age")
        self._setup_table(notifs)

        todo = self.query_one("#todoist-table", DataTable)
        _ = todo.add_columns("#", "!", "", "Time", "#C", "🔗", "Task")
        self._setup_table(todo)

        lin = self.query_one("#linear-table", DataTable)
        _ = lin.add_columns("#", "ID", "Title", "Status", "Owner")
        self._setup_table(lin)

        self.refresh_all()
        _ = self.set_interval(60, self.refresh_all)
        _ = self._check_for_updates()

    def refresh_all(self) -> None:
        self._refresh_goals()
        self._refresh_my_prs()
        self._refresh_review_requests()
        self._refresh_gh_notifications()
        self._refresh_todoist()
        self._refresh_linear()

    @work(exclusive=False)
    async def _check_for_updates(self) -> None:
        """Check for updates and show banner if new version is available."""
        current = _get_current_version()
        if not current:
            return

        remote = await asyncio.to_thread(_get_remote_version)
        if not remote:
            return

        try:
            if _version_tuple(remote) > _version_tuple(current):
                banner = self.query_one("#update-banner", UpdateBanner)
                banner.show_update(remote)
        except (ValueError, TypeError):
            pass

    def _refresh_goals(self) -> None:
        """Refresh the goals table based on current week/review state."""
        today = date.today()
        is_monday = today.weekday() == 0
        this_week = goals_db.get_week_start(today)
        this_week_goals = goals_db.get_goals_for_week(this_week)

        # Load current week metrics
        self._goals_week_metrics = goals_db.get_week_metrics(this_week)

        # Auto-show review modal on Monday if no current week goals and not dismissed
        if is_monday and not this_week_goals and not self._goals_review_dismissed:
            last_week = this_week - timedelta(days=7)
            last_week_goals = goals_db.get_goals_for_week(last_week)
            if last_week_goals:
                # Auto-push the review modal
                last_week_metrics = goals_db.get_week_metrics(last_week)
                self._goals_review_dismissed = True  # Prevent re-showing
                _ = self.push_screen(
                    WeeklyReviewModal(last_week, last_week_goals, last_week_metrics),
                    self._handle_review_complete,
                )

        # Show review only if Monday AND no goals yet for this week
        if is_monday and not this_week_goals:
            last_week = this_week - timedelta(days=7)
            self._goals = goals_db.get_goals_for_week(last_week)
            self._goals_showing_review = True
        else:
            self._goals = this_week_goals
            self._goals_showing_review = False

        self._render_goals_table()

    def _render_goals_table(self) -> None:
        """Render the goals table."""
        table = self.query_one("#goals-table", GoalsDataTable)
        selected_key = self._get_selected_row_key(table)
        _ = table.clear()

        # Update panel title
        panel = self.query_one("#goals", Panel)
        title_widget = panel.query_one(".panel-title", Static)

        if self._goals_showing_review:
            title_widget.update(
                "Weekly Goals (Last Week Review - press 'e' to set up this week)"
            )
            if not self._goals:
                _ = table.add_row(
                    "", "", Text("No goals from last week", style="dim italic")
                )
            else:
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
                # Add prompt to create new goals
                _ = table.add_row(
                    "",
                    "",
                    Text("Press 'a' to add goals for this week", style="dim italic"),
                    key="goal:prompt",
                )
        else:
            # Compute totals from per-goal estimates (non-abandoned, non-completed)
            active_goals = [
                g for g in self._goals if not g.is_completed and not g.is_abandoned
            ]
            total_h2 = sum(g.h2_2025_estimate or 0 for g in active_goals)
            total_pred = sum(g.predicted_time or 0 for g in active_goals)

            # Build title with computed totals
            title = "Weekly Goals"
            if total_h2 > 0 or total_pred > 0:
                estimates = []
                if total_h2 > 0:
                    estimates.append(f"Est: {total_h2:.1f}h")
                if total_pred > 0:
                    estimates.append(f"Pred: {total_pred:.1f}h")
                title = f"Weekly Goals ({' / '.join(estimates)})"
            title_widget.update(title)

            # Show non-completed goals (including abandoned ones with strikethrough)
            visible_goals = [g for g in self._goals if not g.is_completed]
            if not self._goals:
                _ = table.add_row(
                    "",
                    "",
                    Text("No goals yet - press 'a' to add", style="dim italic"),
                )
            elif not visible_goals:
                # All goals complete!
                _ = table.add_row(
                    "",
                    "",
                    Text("All goals complete! Good job!", style="bold green"),
                )
            else:
                for goal in visible_goals:
                    content = (
                        goal.content[:60] + "…"
                        if len(goal.content) > 60
                        else goal.content
                    )
                    if goal.is_abandoned:
                        text = Text(content, style="strike dim")
                        checkbox = "[-]"
                    else:
                        text = Text(content)
                        checkbox = "[ ]"
                    _ = table.add_row(
                        "",
                        checkbox,
                        text,
                        key=f"goal:{goal.id}",
                    )

        if selected_key:
            self._restore_cursor_by_key(table, selected_key)
        table.refresh_line_numbers()

    @work(exclusive=False)
    async def _refresh_my_prs(self) -> None:
        table: DataTable = self.query_one("#my-prs-table", DataTable)
        selected_key = self._get_selected_row_key(table)

        prs = await asyncio.to_thread(github.get_my_prs)
        self._my_prs = prs
        table.clear()

        if not prs:
            table.add_row(
                "", "", Text("No open PRs", style="dim italic"), "", "", "", ""
            )
        else:
            for pr in prs:
                if pr.is_draft:
                    status = "draft"
                elif pr.is_approved:
                    status = "approved"
                elif pr.needs_response:
                    status = "respond"
                elif pr.has_review:
                    status = "reviewed"
                else:
                    status = "waiting"

                ci_display = {
                    "SUCCESS": "pass",
                    "FAILURE": "fail",
                    "PENDING": "...",
                    "EXPECTED": "...",
                }.get(pr.ci_status, "")

                comment_display = (
                    str(pr.unresolved_comment_count)
                    if pr.unresolved_comment_count > 0
                    else ""
                )

                repo = _short_repo(pr.repository)
                title = pr.title[:40] + "…" if len(pr.title) > 40 else pr.title
                table.add_row(
                    "",
                    f"#{pr.number}",
                    title,
                    repo,
                    status,
                    ci_display,
                    comment_display,
                    key=pr.url,
                )

            self._restore_cursor_by_key(table, selected_key)
        table.refresh_line_numbers()

    @work(exclusive=False)
    async def _refresh_review_requests(self) -> None:
        table: DataTable = self.query_one("#review-requests-table", DataTable)
        selected_key = self._get_selected_row_key(table)

        prs = await asyncio.to_thread(github.get_review_requests)
        table.clear()

        def _is_visible(pr: github.ReviewRequest) -> bool:
            if (pr.repository, pr.number) in HIDDEN_REVIEW_REQUESTS:
                return False
            # Hide if all requested teams are blocked (and there are teams)
            if pr.requested_teams and all(
                team in BLOCKED_REVIEW_TEAMS for team in pr.requested_teams
            ):
                return False
            return True

        visible_prs = [pr for pr in prs if _is_visible(pr)]

        if not visible_prs:
            table.add_row(
                "", "", Text("No review requests", style="dim italic"), "", "", "", ""
            )
        else:
            for pr in visible_prs:
                repo = _short_repo(pr.repository)
                age = github._relative_time(pr.created_at)
                title = pr.title[:40] + "…" if len(pr.title) > 40 else pr.title
                reviewed = "✓" if pr.has_other_review else ""
                _ = table.add_row(
                    "",
                    f"#{pr.number}",
                    title,
                    repo,
                    f"@{pr.author}",
                    age,
                    reviewed,
                    key=f"review:{pr.repository}:{pr.number}:{pr.url}",
                )

            self._restore_cursor_by_key(table, selected_key)
        table.refresh_line_numbers()

    @work(exclusive=False)
    async def _refresh_gh_notifications(self) -> None:
        notifications = await asyncio.to_thread(github.get_notifications)
        self._gh_notifications = notifications or []
        self._render_notifications_table()

    def _render_notifications_table(self, preserve_cursor: bool = True) -> None:
        table = self.query_one("#notifications-table", NotificationsDataTable)
        selected_key = self._get_selected_row_key(table) if preserve_cursor else None

        table.clear()

        if not self._gh_notifications:
            table.add_row(
                "", "", Text("No notifications", style="dim italic"), "", "", ""
            )
        else:
            for notif in self._gh_notifications:
                repo = _short_repo(notif.repository)
                age = github._relative_time(notif.updated_at)
                pr_display = f"#{notif.pr_number}" if notif.pr_number else ""
                title = notif.title[:40] + "…" if len(notif.title) > 40 else notif.title
                table.add_row(
                    "",
                    pr_display,
                    title,
                    repo,
                    notif.reason,
                    age,
                    key=f"notif:{notif.id}:{notif.repository}:{notif.pr_number or ''}:{notif.url}",
                )

            if selected_key:
                self._restore_cursor_by_key(table, selected_key)
        table.refresh_line_numbers()

    def _get_selected_row_key(self, table: DataTable) -> str | None:
        if table.cursor_row is None or table.row_count == 0:
            return None
        cell_key = table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0))
        if cell_key.row_key and cell_key.row_key.value:
            return str(cell_key.row_key.value)
        return None

    def _get_row_key_above(self, table: DataTable) -> str | None:
        if table.cursor_row is None or table.cursor_row == 0 or table.row_count == 0:
            return None
        cell_key = table.coordinate_to_cell_key(Coordinate(table.cursor_row - 1, 0))
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
        selected_date = self._todoist_selected_date
        tasks = await asyncio.to_thread(todoist.get_tasks_for_date, selected_date)
        self._todoist_tasks = tasks
        self._update_todoist_panel_title()
        self._render_todoist_table()

    def _update_todoist_panel_title(self) -> None:
        panel = self.query_one("#todoist", Panel)
        title_widget = panel.query_one(".panel-title", Static)
        today = date.today()
        selected = self._todoist_selected_date
        if selected == today:
            title_widget.update("Todoist (Today)")
        elif selected == today + timedelta(days=1):
            title_widget.update("Todoist (Tomorrow)")
        else:
            title_widget.update(f"Todoist ({selected.strftime('%a %b %d')})")

    def action_todoist_previous_day(self) -> None:
        focused = self.focused
        if not isinstance(focused, DataTable) or focused.id != "todoist-table":
            return
        today = date.today()
        if self._todoist_selected_date <= today:
            self.notify("Cannot go before today", severity="warning")
            return
        self._todoist_selected_date -= timedelta(days=1)
        self._update_todoist_panel_title()
        self._refresh_todoist()

    def action_todoist_next_day(self) -> None:
        focused = self.focused
        if not isinstance(focused, DataTable) or focused.id != "todoist-table":
            return
        self._todoist_selected_date += timedelta(days=1)
        self._update_todoist_panel_title()
        self._refresh_todoist()

    def _render_todoist_table(self, preserve_cursor: bool = True) -> None:
        table: DataTable = self.query_one("#todoist-table", DataTable)
        if self._todoist_restore_key:
            selected_key = self._todoist_restore_key
            self._todoist_restore_key = None
        elif preserve_cursor:
            selected_key = self._get_selected_row_key(table)
        else:
            selected_key = None

        table.clear()

        today = date.today()
        selected = self._todoist_selected_date
        if not self._todoist_tasks:
            if selected == today:
                empty_msg = "No tasks for today"
            elif selected == today + timedelta(days=1):
                empty_msg = "No tasks for tomorrow"
            else:
                empty_msg = f"No tasks on {selected.strftime('%a %b %d')}"
            _ = table.add_row(
                "", "", "", "", "", "", Text(empty_msg, style="dim italic")
            )
        else:
            today_str = today.isoformat()
            for task in self._todoist_tasks:
                overdue = "!" if task.due_date and task.due_date < today_str else ""
                checkbox = "[x]" if task.is_completed else "[ ]"
                time_display = task.due_time or ""
                comment_display = (
                    str(task.comment_count) if task.comment_count > 0 else ""
                )
                has_link = (
                    self._extract_url(task.content) is not None
                    or self._extract_url(task.description) is not None
                )
                link_display = "🔗" if has_link else ""
                content = (
                    task.content[:60] + "…" if len(task.content) > 60 else task.content
                )
                _ = table.add_row(
                    "",
                    overdue,
                    checkbox,
                    time_display,
                    comment_display,
                    link_display,
                    content,
                    key=f"todoist:{task.id}:{task.url}",
                )

            if selected_key:
                self._restore_cursor_by_key(table, selected_key)
        table.refresh_line_numbers()

    @work(exclusive=False)
    async def _refresh_linear(self) -> None:
        issues = await asyncio.to_thread(linear.get_project_issues)
        self._linear_issues = [
            i for i in issues if i.state not in ("Done", "Canceled", "Duplicate")
        ]
        self._render_linear_table()

    def _render_linear_table(self, preserve_cursor: bool = True) -> None:
        table: DataTable = self.query_one("#linear-table", DataTable)
        selected_key = self._get_selected_row_key(table) if preserve_cursor else None

        table.clear()

        if not self._linear_issues:
            table.add_row("", "", Text("No active issues", style="dim italic"), "", "")
        else:
            for issue in self._linear_issues:
                assignee = issue.assignee_initials or ""
                title = issue.title[:50] + "…" if len(issue.title) > 50 else issue.title
                state_display = LINEAR_STATE_SHORT.get(issue.state, issue.state)
                table.add_row(
                    "",
                    issue.identifier,
                    title,
                    state_display,
                    assignee,
                    key=f"linear:{issue.id}:{issue.team_id}:{issue.url}",
                )

            if selected_key:
                self._restore_cursor_by_key(table, selected_key)
        table.refresh_line_numbers()

    def action_refresh(self) -> None:
        self.refresh_all()
        self.notify("Refreshing...")

    def action_restart(self) -> None:
        self._do_upgrade_and_restart()

    def action_focus_previous_pane(self) -> None:
        """Move focus to the previous pane."""
        tables = list(self.query(DataTable))
        if not tables:
            return
        focused = self.focused
        if not isinstance(focused, DataTable) or focused not in tables:
            tables[-1].focus()
            return
        current_idx = tables.index(focused)
        prev_idx = (current_idx - 1) % len(tables)
        tables[prev_idx].focus()

    def action_focus_next_pane(self) -> None:
        """Move focus to the next pane."""
        tables = list(self.query(DataTable))
        if not tables:
            return
        focused = self.focused
        if not isinstance(focused, DataTable) or focused not in tables:
            tables[0].focus()
            return
        current_idx = tables.index(focused)
        next_idx = (current_idx + 1) % len(tables)
        tables[next_idx].focus()

    def _is_uv_tool(self) -> bool:
        """Check if this app is installed as a uv tool."""
        import shutil
        import subprocess

        if not shutil.which("uv"):
            return False
        try:
            result = subprocess.run(
                ["uv", "tool", "list"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return "status-dashboard" in result.stdout
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def _upgrade_uv_tool(self) -> tuple[bool, str]:
        """Force reinstall the uv tool and return (success, message)."""
        import subprocess

        try:
            result = subprocess.run(
                [
                    "uv",
                    "tool",
                    "install",
                    "--force",
                    "git+https://github.com/tbroadley/status-dashboard",
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode != 0:
                return False, result.stderr[:100]
            return True, ""
        except subprocess.TimeoutExpired:
            return False, "Reinstall timed out"

    @work(exclusive=False)
    async def _do_upgrade_and_restart(self) -> None:
        if self._is_uv_tool():
            self.notify("Upgrading status-dashboard...")
            success, error = await asyncio.to_thread(self._upgrade_uv_tool)
            if not success:
                self.notify(f"Upgrade failed: {error}", severity="warning")

        self.exit()
        os.execv(sys.executable, [sys.executable] + sys.argv)

    def action_undo(self) -> None:
        """Undo the most recent action."""
        if self._undo_stack.is_empty():
            self.notify("Nothing to undo", severity="warning")
            return

        action = self._undo_stack.pop()
        if action is None:
            return

        self._execute_undo(action)

    @work(exclusive=False)
    async def _execute_undo(
        self,
        action: TodoistCompleteAction
        | TodoistDeferAction
        | TodoistMoveAction
        | LinearSetStateAction
        | LinearAssignAction
        | LinearMoveAction
        | GoalCompleteAction,
    ) -> None:
        """Execute the undo operation for a given action."""
        success = False

        if isinstance(action, TodoistCompleteAction):
            success = await asyncio.to_thread(todoist.reopen_task, action.task_id)
            if success:
                self._refresh_todoist()

        elif isinstance(action, TodoistDeferAction):
            success = await asyncio.to_thread(
                todoist.set_due_date, action.task_id, action.original_due_date
            )
            if success:
                self._refresh_todoist()

        elif isinstance(action, TodoistMoveAction):
            success = await asyncio.to_thread(
                todoist.update_day_orders, action.ids_to_orders
            )
            if success:
                self._refresh_todoist()

        elif isinstance(action, LinearSetStateAction):
            success = await asyncio.to_thread(
                linear.set_issue_state_by_name,
                action.issue_id,
                action.team_id,
                action.previous_state,
            )
            if success:
                self._refresh_linear()

        elif isinstance(action, LinearAssignAction):
            success = await asyncio.to_thread(
                linear.assign_issue, action.issue_id, action.previous_assignee_id
            )
            if success:
                self._refresh_linear()

        elif isinstance(action, LinearMoveAction):
            success = await asyncio.to_thread(
                linear.update_sort_order, action.issue_id, action.previous_sort_order
            )
            if success:
                self._refresh_linear()

        elif isinstance(action, GoalCompleteAction):
            success = goals_db.uncomplete_goal(action.goal_id)
            if success:
                self._refresh_goals()

        elif isinstance(action, GoalAbandonAction):
            success = goals_db.unabandon_goal(action.goal_id)
            if success:
                self._refresh_goals()

        if success:
            self.notify(f"Undid: {action.description}")
        else:
            self.notify(f"Failed to undo: {action.description}", severity="error")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Handle Enter key on a row - open edit modal for Todoist, browser for others."""
        key = str(event.row_key.value) if event.row_key.value else ""

        # For Todoist tasks, Enter opens the edit modal instead of browser
        if key.startswith("todoist:"):
            # Format: "todoist:{task_id}:{url}"
            parts = key.split(":", 2)
            if len(parts) >= 2:
                task_id = parts[1]
                _ = self._prepare_edit_todoist_task(task_id)
            return

        # Extract URL from key format for other types
        if key.startswith("linear:"):
            # Format: "linear:{issue_id}:{team_id}:{url}"
            url = key.split(":", 3)[3]
        elif key.startswith("review:"):
            # Format: "review:{repo}:{number}:{url}"
            url = key.split(":", 3)[3]
        elif key.startswith("notif:"):
            # Format: "notif:{thread_id}:{repo}:{pr_number}:{url}"
            url = key.split(":", 4)[4]
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
                task_name = self._get_row_content(focused)
                self._todoist_restore_key = self._get_row_key_above(focused)
                self._do_complete_todoist_task(task_id, task_name)
        elif focused.id == "linear-table" and key.startswith("linear:"):
            # Key format: "linear:{issue_id}:{team_id}:{url}"
            parts = key.split(":", 3)
            if len(parts) >= 3:
                issue_id = parts[1]
                team_id = parts[2]
                self._do_complete_linear_issue(issue_id, team_id)
        else:
            self.notify(
                "Can only complete Todoist tasks or Linear issues", severity="warning"
            )

    def _get_row_content(self, table: DataTable) -> str:
        """Get the content/title column text from the current row."""
        if table.cursor_row is None or table.row_count == 0:
            return ""
        try:
            row_data = table.get_row_at(table.cursor_row)
            col_idx = 5 if table.id == "todoist-table" else 2
            return str(row_data[col_idx]) if len(row_data) > col_idx else ""
        except Exception:
            return ""

    @work(exclusive=False)
    async def _do_complete_todoist_task(
        self, task_id: str, task_name: str | None
    ) -> None:
        success = await asyncio.to_thread(todoist.complete_task, task_id)
        if success:
            description = (
                f"Complete: {task_name[:30]}" if task_name else "Complete task"
            )
            self._undo_stack.push(
                TodoistCompleteAction(
                    task_id=task_id,
                    description=description,
                )
            )
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
            task_name = self._get_row_content(focused)
            self._todoist_restore_key = self._get_row_key_above(focused)
            self._do_defer_todoist_task(task_id, task_name)

    @work(exclusive=False)
    async def _do_defer_todoist_task(self, task_id: str, task_name: str | None) -> None:
        task = await asyncio.to_thread(todoist.get_task, task_id)
        original_due = (
            task.get("due", {}).get("date") if task and task.get("due") else None
        )

        success = await asyncio.to_thread(todoist.defer_task, task_id)
        if success:
            description = f"Defer: {task_name[:30]}" if task_name else "Defer task"
            self._undo_stack.push(
                TodoistDeferAction(
                    task_id=task_id,
                    original_due_date=original_due,
                    description=description,
                )
            )
            self.notify("Task deferred to next working day")
            self._refresh_todoist()
        else:
            self.notify("Failed to defer task", severity="error")

    def action_delete_task(self) -> None:
        """Delete the selected Todoist task."""
        focused = self.focused
        if not isinstance(focused, DataTable):
            return

        if focused.id != "todoist-table":
            self.notify("Can only delete Todoist tasks", severity="warning")
            return

        if focused.cursor_row is None or focused.row_count == 0:
            return

        cell_key = focused.coordinate_to_cell_key(Coordinate(focused.cursor_row, 0))
        if not cell_key.row_key or not cell_key.row_key.value:
            return

        key = str(cell_key.row_key.value)

        if not key.startswith("todoist:"):
            return

        parts = key.split(":", 2)
        if len(parts) >= 2:
            task_id = parts[1]
            self._do_delete_todoist_task(task_id)

    @work(exclusive=False)
    async def _do_delete_todoist_task(self, task_id: str) -> None:
        success = await asyncio.to_thread(todoist.delete_task, task_id)
        if success:
            self.notify("Task deleted")
            self._refresh_todoist()
        else:
            self.notify("Failed to delete task", severity="error")

    def action_move_task_down(self) -> None:
        """Move the selected Todoist task down in the Today view."""
        self._move_todoist_task(1)

    def action_move_task_up(self) -> None:
        """Move the selected Todoist task up in the Today view."""
        self._move_todoist_task(-1)

    def _move_todoist_task(self, direction: int) -> None:
        """Move the selected Todoist task up (-1) or down (+1) with optimistic UI update."""
        focused = self.focused
        if not isinstance(focused, DataTable):
            return

        if focused.id != "todoist-table":
            self.notify("Can only move Todoist tasks", severity="warning")
            return

        if focused.cursor_row is None or focused.row_count == 0:
            return

        current_row = focused.cursor_row
        target_row = current_row + direction

        if target_row < 0 or target_row >= len(self._todoist_tasks):
            return

        self._todoist_tasks[current_row], self._todoist_tasks[target_row] = (
            self._todoist_tasks[target_row],
            self._todoist_tasks[current_row],
        )

        self._render_todoist_table(preserve_cursor=False)
        focused.move_cursor(row=target_row)
        row_region = focused._get_row_region(target_row)
        focused.scroll_to_region(row_region, center=True, animate=False)

        self._schedule_todoist_sync()

    def _schedule_todoist_sync(self) -> None:
        """Schedule a debounced sync of task order to Todoist API."""
        if self._todoist_debounce_handle:
            self._todoist_debounce_handle.stop()

        self._todoist_debounce_handle = self.set_timer(0.5, self._flush_todoist_order)

    @work(exclusive=False)
    async def _flush_todoist_order(self) -> None:
        """Send current task order to Todoist API."""
        self._todoist_debounce_handle = None

        new_orders = {
            task.id: idx
            for idx, task in enumerate(self._todoist_tasks)
            if not task.id.startswith("temp-")
        }

        if not new_orders:
            return

        success = await asyncio.to_thread(todoist.update_day_orders, new_orders)
        if not success:
            self.notify("Failed to save task order", severity="error")
            self._refresh_todoist()

    def action_reschedule_overdue_to_today(self) -> None:
        """Reschedule all overdue Todoist tasks to today."""
        focused = self.focused
        if not isinstance(focused, DataTable):
            return

        if focused.id != "todoist-table":
            self.notify("Can only reschedule from Todoist panel", severity="warning")
            return

        today = date.today().isoformat()
        overdue_tasks = [
            t for t in self._todoist_tasks if t.due_date and t.due_date < today
        ]

        if not overdue_tasks:
            self.notify("No overdue tasks to reschedule")
            return

        self._do_reschedule_overdue_to_today(overdue_tasks)

    @work(exclusive=False)
    async def _do_reschedule_overdue_to_today(self, tasks: list[todoist.Task]) -> None:
        success_count = 0
        for task in tasks:
            success = await asyncio.to_thread(todoist.reschedule_to_today, task.id)
            if success:
                success_count += 1

        if success_count == len(tasks):
            self.notify(f"Rescheduled {success_count} task(s) to today")
        elif success_count > 0:
            self.notify(
                f"Rescheduled {success_count}/{len(tasks)} tasks", severity="warning"
            )
        else:
            self.notify("Failed to reschedule tasks", severity="error")

        self._refresh_todoist()

    def action_open_task_link(self) -> None:
        """Open the first link found in the selected Todoist task's description."""
        focused = self.focused
        if not isinstance(focused, DataTable):
            return

        if focused.id != "todoist-table":
            self.notify("Can only open links from Todoist tasks", severity="warning")
            return

        if focused.cursor_row is None or focused.row_count == 0:
            return

        cell_key = focused.coordinate_to_cell_key(Coordinate(focused.cursor_row, 0))
        if not cell_key.row_key or not cell_key.row_key.value:
            return

        key = str(cell_key.row_key.value)

        if not key.startswith("todoist:"):
            return

        parts = key.split(":", 2)
        if len(parts) >= 2:
            task_id = parts[1]
            self._do_open_task_link(task_id)

    def _extract_url(self, text: str) -> str | None:
        """Extract a URL from text, handling Markdown links like [text](url)."""
        markdown_link = re.search(r"\[.*?\]\((https?://[^)]+)\)", text)
        if markdown_link:
            return markdown_link.group(1)
        url_pattern = re.compile(r'https?://[^\s<>"{}|\\^`\[\]]+')
        match = url_pattern.search(text)
        if match:
            return match.group()
        return None

    @work(exclusive=False)
    async def _do_open_task_link(self, task_id: str) -> None:
        task = await asyncio.to_thread(todoist.get_task, task_id)
        if not task:
            self.notify("Failed to fetch task", severity="error")
            return

        content = task.get("content", "")
        url = self._extract_url(content)
        if url:
            webbrowser.open(url)
            return

        description = task.get("description", "")
        url = self._extract_url(description)
        if url:
            webbrowser.open(url)
            return

        self.notify("No link found in task", severity="warning")

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
            issue_identifier = self._get_row_identifier(focused)
            self._do_set_linear_state(issue_id, team_id, state, issue_identifier)

    def _get_row_identifier(self, table: DataTable) -> str:
        """Get the identifier (second column, after line numbers) from the current row."""
        if table.cursor_row is None or table.row_count == 0:
            return ""
        try:
            row_data = table.get_row_at(table.cursor_row)
            return str(row_data[1]) if len(row_data) > 1 else ""
        except Exception:
            return ""

    @work(exclusive=False)
    async def _do_set_linear_state(
        self, issue_id: str, team_id: str, state: str, issue_identifier: str | None
    ) -> None:
        issue = await asyncio.to_thread(linear.get_issue, issue_id)
        previous_state = issue.get("state", {}).get("name") if issue else None

        state_display = linear.STATE_NAME_MAP.get(state, state)
        success = await asyncio.to_thread(
            linear.set_issue_state, issue_id, team_id, state
        )
        if success:
            if previous_state:
                description = f"Set {issue_identifier or issue_id} to {state_display}"
                self._undo_stack.push(
                    LinearSetStateAction(
                        issue_id=issue_id,
                        team_id=team_id,
                        previous_state=previous_state,
                        description=description,
                    )
                )
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
        focused = self.focused
        if not isinstance(focused, DataTable) or focused.id != "linear-table":
            self.notify("Select a Linear issue first", severity="warning")
            return
        issue_id = self._get_selected_linear_issue_id()
        if not issue_id:
            self.notify("Select a Linear issue first", severity="warning")
            return
        issue_identifier = self._get_row_identifier(focused)
        self._do_assign_linear_issue(
            issue_id, assign=True, issue_identifier=issue_identifier
        )

    def action_unassign_linear(self) -> None:
        focused = self.focused
        if not isinstance(focused, DataTable) or focused.id != "linear-table":
            self.notify("Select a Linear issue first", severity="warning")
            return
        issue_id = self._get_selected_linear_issue_id()
        if not issue_id:
            self.notify("Select a Linear issue first", severity="warning")
            return
        issue_identifier = self._get_row_identifier(focused)
        self._do_assign_linear_issue(
            issue_id, assign=False, issue_identifier=issue_identifier
        )

    @work(exclusive=False)
    async def _do_assign_linear_issue(
        self, issue_id: str, assign: bool, issue_identifier: str | None
    ) -> None:
        issue = await asyncio.to_thread(linear.get_issue, issue_id)
        previous_assignee_id = (
            issue.get("assignee", {}).get("id")
            if issue and issue.get("assignee")
            else None
        )

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
            description = (
                f"{'Assign' if assign else 'Unassign'} {issue_identifier or issue_id}"
            )
            self._undo_stack.push(
                LinearAssignAction(
                    issue_id=issue_id,
                    previous_assignee_id=previous_assignee_id,
                    description=description,
                )
            )
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
        success = await asyncio.to_thread(
            github.remove_self_as_reviewer, repo, pr_number
        )
        if success:
            self.notify(f"Removed from PR #{pr_number}")
            self._refresh_review_requests()
        else:
            self.notify("Failed to remove self as reviewer", severity="error")

    def action_merge_pr(self) -> None:
        """Squash merge the selected approved PR."""
        focused = self.focused
        if not isinstance(focused, DataTable):
            return

        if focused.id != "my-prs-table":
            self.notify("Can only merge from My PRs", severity="warning")
            return

        if focused.cursor_row is None or focused.row_count == 0:
            return

        cell_key = focused.coordinate_to_cell_key(Coordinate(focused.cursor_row, 0))
        if not cell_key.row_key or not cell_key.row_key.value:
            return

        url = str(cell_key.row_key.value)

        pr = next((p for p in self._my_prs if p.url == url), None)
        if not pr:
            return

        if not pr.is_approved:
            self.notify("Can only merge approved PRs", severity="warning")
            return

        self._do_merge_pr(pr.repository, pr.number)

    @work(exclusive=False)
    async def _do_merge_pr(self, repo: str, pr_number: int) -> None:
        success = await asyncio.to_thread(github.squash_merge_pr, repo, pr_number)
        if success:
            self.notify(f"Merged PR #{pr_number}")
            self._refresh_my_prs()
        else:
            self.notify("Failed to merge PR", severity="error")

    def action_copy_pr_link(self) -> None:
        """Copy the selected PR's URL to the clipboard."""
        focused = self.focused
        if not isinstance(focused, DataTable):
            return

        if focused.id not in (
            "my-prs-table",
            "review-requests-table",
            "notifications-table",
        ):
            self.notify("Can only copy links from PR tables", severity="warning")
            return

        if focused.cursor_row is None or focused.row_count == 0:
            return

        cell_key = focused.coordinate_to_cell_key(Coordinate(focused.cursor_row, 0))
        if not cell_key.row_key or not cell_key.row_key.value:
            return

        key = str(cell_key.row_key.value)

        if focused.id == "my-prs-table":
            url = key
        elif key.startswith("review:"):
            url = key.split(":", 3)[3]
        elif key.startswith("notif:"):
            url = key.split(":", 4)[4]
        else:
            return

        self.copy_to_clipboard(url)
        self.notify("Link copied to clipboard")

    def action_mark_notification_read(self) -> None:
        """Mark the selected notification as read."""
        focused = self.focused
        if not isinstance(focused, DataTable):
            return

        if focused.id != "notifications-table":
            self.notify("Can only mark notifications as read", severity="warning")
            return

        if focused.cursor_row is None or focused.row_count == 0:
            return

        cell_key = focused.coordinate_to_cell_key(Coordinate(focused.cursor_row, 0))
        if not cell_key.row_key or not cell_key.row_key.value:
            return

        key = str(cell_key.row_key.value)

        if not key.startswith("notif:"):
            return

        # Key format: "notif:{thread_id}:{repo}:{pr_number}:{url}"
        parts = key.split(":", 4)
        if len(parts) < 2:
            return

        thread_id = parts[1]
        cursor_row = focused.cursor_row

        # Optimistic update: find and remove notification from list
        removed_notification = None
        removed_index = None
        for idx, notif in enumerate(self._gh_notifications):
            if notif.id == thread_id:
                removed_notification = notif
                removed_index = idx
                break

        if removed_notification is None or removed_index is None:
            return

        # Remove from list and re-render
        _ = self._gh_notifications.pop(removed_index)
        self._render_notifications_table(preserve_cursor=False)

        # Position cursor appropriately after removal
        table = self.query_one("#notifications-table", NotificationsDataTable)
        if table.row_count > 0:
            new_row = min(cursor_row, table.row_count - 1)
            table.move_cursor(row=new_row)
        table.refresh_line_numbers()

        # Make API call in background
        _ = self._do_mark_notification_read(
            thread_id, removed_notification, removed_index
        )

    @work(exclusive=False)
    async def _do_mark_notification_read(
        self,
        thread_id: str,
        removed_notification: github.Notification,
        removed_index: int,
    ) -> None:
        success = await asyncio.to_thread(github.mark_notification_read, thread_id)
        if success:
            self.notify("Notification marked as read")
        else:
            # Restore notification on failure
            self._gh_notifications.insert(removed_index, removed_notification)
            self._render_notifications_table()
            self.notify("Failed to mark notification as read", severity="error")

    def action_create_todoist_task(self) -> None:
        """Show modal to create a new Todoist task."""
        table = self.query_one("#todoist-table", TodoistDataTable)
        insert_position = table.cursor_row or 0

        def handle_result(result: dict[str, str] | None) -> None:
            self._handle_todoist_task_created(result, insert_position)

        self.push_screen(CreateTodoistTaskModal(), handle_result)

    def _handle_todoist_task_created(
        self, result: dict[str, str] | None, insert_position: int
    ) -> None:
        """Handle the result from the Todoist task creation modal."""
        if result:
            content = result["content"]
            due_string = result["due_string"]

            # Calculate the actual due date for the optimistic task
            optimistic_due_date = self._calculate_due_date(due_string)

            # Only show optimistic task if it will appear in the current view
            show_optimistic = optimistic_due_date == self._todoist_selected_date

            if show_optimistic:
                temp_id = f"temp-{uuid.uuid4()}"
                optimistic_task = todoist.Task(
                    id=temp_id,
                    content=content,
                    is_completed=False,
                    url="",
                    day_order=insert_position,
                    due_date=optimistic_due_date.isoformat(),
                    due_time=None,
                    comment_count=0,
                )

                self._todoist_tasks.insert(insert_position, optimistic_task)
                self._todoist_optimistic_tasks[temp_id] = optimistic_task
                self._render_todoist_table(preserve_cursor=False)

                table = self.query_one("#todoist-table", TodoistDataTable)
                table.move_cursor(row=insert_position)

                _ = self._do_create_todoist_task(content, due_string, temp_id)
            else:
                # Task is for a different day, just create it without optimistic UI
                _ = self._do_create_todoist_task(content, due_string, None)

    def _calculate_due_date(self, due_string: str) -> date:
        """Calculate the actual due date from a due_string.

        This mirrors Todoist's natural language parsing to predict
        where the task will appear.
        """
        today = date.today()

        if due_string == "today":
            return today
        elif due_string == "tomorrow":
            return today + timedelta(days=1)
        elif due_string == "monday":
            # Find the upcoming Monday
            days_until_monday = (7 - today.weekday()) % 7
            if days_until_monday == 0:
                days_until_monday = 7  # If today is Monday, go to next Monday
            return today + timedelta(days=days_until_monday)
        elif due_string == "next week":
            # Todoist interprets "next week" as 7 days from now
            return today + timedelta(days=7)
        else:
            # Default to today for unknown strings
            return today

    @work(exclusive=False)
    async def _do_create_todoist_task(
        self, content: str, due_string: str, temp_id: str | None
    ) -> None:
        new_task_id = await asyncio.to_thread(todoist.create_task, content, due_string)
        if not new_task_id:
            self.notify("Failed to create task", severity="error")
            if temp_id:
                self._todoist_tasks = [
                    t for t in self._todoist_tasks if t.id != temp_id
                ]
                _ = self._todoist_optimistic_tasks.pop(temp_id, None)
                self._render_todoist_table()
            return

        self.notify("Task created!")

        # If no optimistic task was created, we're done
        if not temp_id:
            return

        updated_task: todoist.Task | None = None
        for task in self._todoist_tasks:
            if task.id == temp_id:
                task.id = new_task_id
                task.url = f"https://app.todoist.com/app/task/{new_task_id}"
                updated_task = task
                break

        self._todoist_optimistic_tasks.pop(temp_id, None)

        new_orders: dict[str, int] = {}
        for idx, task in enumerate(self._todoist_tasks):
            if not task.id.startswith("temp-"):
                new_orders[task.id] = idx

        await asyncio.to_thread(todoist.update_day_orders, new_orders)

        # Check cursor position RIGHT BEFORE rendering to avoid race condition
        # where user moves cursor during the API call above
        table = self.query_one("#todoist-table", TodoistDataTable)
        selected_key = self._get_selected_row_key(table)
        cursor_on_optimistic = selected_key == f"todoist:{temp_id}:"

        if cursor_on_optimistic and updated_task:
            self._todoist_restore_key = f"todoist:{new_task_id}:{updated_task.url}"
        self._render_todoist_table()

    def action_edit_todoist_task(self) -> None:
        """Show modal to edit the selected Todoist task."""
        focused = self.focused
        if not isinstance(focused, DataTable) or focused.id != "todoist-table":
            self.notify("Select a Todoist task first", severity="warning")
            return

        if focused.cursor_row is None or focused.row_count == 0:
            return

        cell_key = focused.coordinate_to_cell_key(Coordinate(focused.cursor_row, 0))
        if not cell_key.row_key or not cell_key.row_key.value:
            return

        key = str(cell_key.row_key.value)
        if not key.startswith("todoist:"):
            return

        parts = key.split(":", 2)
        if len(parts) >= 2:
            task_id = parts[1]
            _ = self._prepare_edit_todoist_task(task_id)

    @work(exclusive=False)
    async def _prepare_edit_todoist_task(self, task_id: str) -> None:
        """Load task data and projects, then show edit modal."""
        task_data = await asyncio.to_thread(todoist.get_task, task_id)
        if not task_data:
            self.notify("Failed to load task", severity="error")
            return

        projects = await asyncio.to_thread(todoist.get_projects)

        content = task_data.get("content", "")
        description = task_data.get("description", "")
        project_id = task_data.get("project_id")
        due = task_data.get("due")
        due_string = due.get("string", "") if due else ""

        project_options = [(p.name, p.id) for p in projects]

        _ = self.push_screen(
            EditTodoistTaskModal(
                task_id=task_id,
                content=content,
                description=description,
                project_id=project_id,
                due_string=due_string,
                projects=project_options,
            ),
            self._handle_todoist_task_edited,
        )

    def _handle_todoist_task_edited(self, result: dict[str, str] | None) -> None:
        """Handle the result from the Todoist task edit modal."""
        if result and len(result) > 1:
            task_id = result["task_id"]
            content = result.get("content")
            description = result.get("description")
            project_id = result.get("project_id")
            due_string = result.get("due_string")
            _ = self._do_update_todoist_task(
                task_id, content, description, project_id, due_string
            )

    @work(exclusive=False)
    async def _do_update_todoist_task(
        self,
        task_id: str,
        content: str | None,
        description: str | None,
        project_id: str | None,
        due_string: str | None,
    ) -> None:
        success = await asyncio.to_thread(
            todoist.update_task,
            task_id,
            content=content,
            description=description,
            project_id=project_id,
            due_string=due_string,
        )
        if success:
            self.notify("Task updated!")
            _ = self._refresh_todoist()
        else:
            self.notify("Failed to update task", severity="error")

    def action_open_todoist_in_browser(self) -> None:
        """Open the selected Todoist task in the web app."""
        focused = self.focused
        if not isinstance(focused, DataTable) or focused.id != "todoist-table":
            self.notify("Select a Todoist task first", severity="warning")
            return

        if focused.cursor_row is None or focused.row_count == 0:
            return

        cell_key = focused.coordinate_to_cell_key(Coordinate(focused.cursor_row, 0))
        if not cell_key.row_key or not cell_key.row_key.value:
            return

        key = str(cell_key.row_key.value)
        if not key.startswith("todoist:"):
            return

        # Format: "todoist:{task_id}:{url}"
        parts = key.split(":", 2)
        if len(parts) >= 3:
            url = parts[2]
            if url:
                _ = webbrowser.open(url)

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

        # Get team members and viewer ID
        team_members = await asyncio.to_thread(linear.get_team_members)
        viewer_id = await asyncio.to_thread(linear.get_viewer_id)

        # Store team_id for later use
        self._linear_team_id = team_id

        # Show modal
        self.push_screen(
            CreateLinearIssueModal(team_members, viewer_id=viewer_id),
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

    def action_move_linear_issue_down(self) -> None:
        """Move the selected Linear issue down."""
        self._move_linear_issue(1)

    def action_move_linear_issue_up(self) -> None:
        """Move the selected Linear issue up."""
        self._move_linear_issue(-1)

    def _move_linear_issue(self, direction: int) -> None:
        """Move the selected Linear issue up (-1) or down (+1) within its status group."""
        focused = self.focused
        if not isinstance(focused, DataTable):
            return

        if focused.id != "linear-table":
            self.notify("Can only move Linear issues", severity="warning")
            return

        if focused.cursor_row is None or focused.row_count == 0:
            return

        current_row = focused.cursor_row
        target_row = current_row + direction

        if target_row < 0 or target_row >= len(self._linear_issues):
            return

        moved_issue = self._linear_issues[current_row]
        target_issue = self._linear_issues[target_row]

        if moved_issue.state != target_issue.state:
            return

        original_sort_order = moved_issue.sort_order

        self._linear_issues[current_row], self._linear_issues[target_row] = (
            self._linear_issues[target_row],
            self._linear_issues[current_row],
        )

        self._render_linear_table(preserve_cursor=False)
        focused.move_cursor(row=target_row)
        row_region = focused._get_row_region(target_row)
        focused.scroll_to_region(row_region, center=True, animate=False)

        self._schedule_linear_sync(moved_issue.id, original_sort_order, target_row)

    def _schedule_linear_sync(
        self, issue_id: str, original_sort_order: float, target_row: int
    ) -> None:
        """Schedule a debounced sync of issue order to Linear API."""
        if self._linear_debounce_handle:
            self._linear_debounce_handle.stop()

        self._linear_pending_move = (issue_id, original_sort_order, target_row)
        self._linear_debounce_handle = self.set_timer(0.5, self._flush_linear_order)

    @work(exclusive=False)
    async def _flush_linear_order(self) -> None:
        """Send current issue order to Linear API."""
        self._linear_debounce_handle = None

        if not hasattr(self, "_linear_pending_move"):
            return

        issue_id, original_sort_order, target_row = self._linear_pending_move
        delattr(self, "_linear_pending_move")

        if target_row < 0 or target_row >= len(self._linear_issues):
            return

        moved_issue = self._linear_issues[target_row]
        same_status_issues = [
            (idx, i)
            for idx, i in enumerate(self._linear_issues)
            if i.state == moved_issue.state
        ]

        pos_in_group = next(
            i for i, (idx, _) in enumerate(same_status_issues) if idx == target_row
        )

        if pos_in_group == 0:
            new_sort_order = (
                same_status_issues[1][1].sort_order - 1.0
                if len(same_status_issues) > 1
                else 0.0
            )
        elif pos_in_group == len(same_status_issues) - 1:
            new_sort_order = same_status_issues[-2][1].sort_order + 1.0
        else:
            prev_order = same_status_issues[pos_in_group - 1][1].sort_order
            next_order = same_status_issues[pos_in_group + 1][1].sort_order
            new_sort_order = (prev_order + next_order) / 2.0

        self._linear_issues[target_row].sort_order = new_sort_order

        success = await asyncio.to_thread(
            linear.update_sort_order, issue_id, new_sort_order
        )
        if success:
            issue = self._linear_issues[target_row]
            self._undo_stack.push(
                LinearMoveAction(
                    issue_id=issue_id,
                    previous_sort_order=original_sort_order,
                    description=f"Move {issue.identifier}",
                )
            )
        else:
            self.notify("Failed to save issue order", severity="error")
            self._refresh_linear()

    def action_create_goal(self) -> None:
        """Show modal to create a new weekly goal."""
        focused = self.focused
        if not isinstance(focused, DataTable) or focused.id != "goals-table":
            self.notify("Focus on goals panel first", severity="warning")
            return

        _ = self.push_screen(CreateGoalModal(), self._handle_goal_created)

    def _handle_goal_created(self, result: dict[str, str] | None) -> None:
        """Handle the result from the goal creation modal."""
        if result:
            content = result["content"]
            today = date.today()
            week_start = goals_db.get_week_start(today)
            _ = goals_db.create_goal(content, week_start)
            self.notify("Goal added!")
            self._refresh_goals()

    def action_complete_goal(self) -> None:
        """Mark the selected goal as complete."""
        focused = self.focused
        if not isinstance(focused, DataTable) or focused.id != "goals-table":
            self.notify("Select a goal first", severity="warning")
            return

        if focused.cursor_row is None or focused.row_count == 0:
            return

        cell_key = focused.coordinate_to_cell_key(Coordinate(focused.cursor_row, 0))
        if not cell_key.row_key or not cell_key.row_key.value:
            return

        key = str(cell_key.row_key.value)
        if not key.startswith("goal:") or key == "goal:prompt":
            return

        goal_id = key.split(":", 1)[1]
        goal = next((g for g in self._goals if g.id == goal_id), None)
        if not goal:
            return

        if goals_db.complete_goal(goal_id):
            description = f"Complete: {goal.content[:30]}"
            self._undo_stack.push(
                GoalCompleteAction(goal_id=goal_id, description=description)
            )
            self.notify("Goal completed!")
            self._refresh_goals()
        else:
            self.notify("Failed to complete goal", severity="error")

    def action_delete_goal(self) -> None:
        """Delete the selected goal."""
        focused = self.focused
        if not isinstance(focused, DataTable) or focused.id != "goals-table":
            self.notify("Select a goal first", severity="warning")
            return

        if focused.cursor_row is None or focused.row_count == 0:
            return

        cell_key = focused.coordinate_to_cell_key(Coordinate(focused.cursor_row, 0))
        if not cell_key.row_key or not cell_key.row_key.value:
            return

        key = str(cell_key.row_key.value)
        if not key.startswith("goal:") or key == "goal:prompt":
            return

        goal_id = key.split(":", 1)[1]
        if goals_db.delete_goal(goal_id):
            self.notify("Goal deleted")
            self._refresh_goals()
        else:
            self.notify("Failed to delete goal", severity="error")

    def action_abandon_goal(self) -> None:
        """Mark the selected goal as abandoned (or restore if already abandoned)."""
        focused = self.focused
        if not isinstance(focused, DataTable) or focused.id != "goals-table":
            self.notify("Select a goal first", severity="warning")
            return

        if focused.cursor_row is None or focused.row_count == 0:
            return

        cell_key = focused.coordinate_to_cell_key(Coordinate(focused.cursor_row, 0))
        if not cell_key.row_key or not cell_key.row_key.value:
            return

        key = str(cell_key.row_key.value)
        if not key.startswith("goal:") or key == "goal:prompt":
            return

        goal_id = key.split(":", 1)[1]
        goal = next((g for g in self._goals if g.id == goal_id), None)
        if not goal:
            return

        if goal.is_abandoned:
            # Already abandoned - restore it
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

    def action_move_goal_down(self) -> None:
        """Move the selected goal down."""
        self._move_goal(1)

    def action_move_goal_up(self) -> None:
        """Move the selected goal up."""
        self._move_goal(-1)

    def _move_goal(self, direction: int) -> None:
        """Move the selected goal up (-1) or down (+1)."""
        focused = self.focused
        if not isinstance(focused, DataTable) or focused.id != "goals-table":
            self.notify("Can only move goals", severity="warning")
            return

        if focused.cursor_row is None or focused.row_count == 0:
            return

        # Get the list of goals that can be reordered (incomplete goals in normal view)
        if self._goals_showing_review:
            # In review mode, we can reorder all goals (they're from last week)
            movable_goals = [g for g in self._goals]
        else:
            # In normal mode, only incomplete goals are shown and movable
            movable_goals = [g for g in self._goals if not g.is_completed]

        current_row = focused.cursor_row
        target_row = current_row + direction

        if target_row < 0 or target_row >= len(movable_goals):
            return

        # Don't allow moving onto the prompt row
        cell_key = focused.coordinate_to_cell_key(Coordinate(target_row, 0))
        if cell_key.row_key and str(cell_key.row_key.value) == "goal:prompt":
            return

        # Swap in the local list
        movable_goals[current_row], movable_goals[target_row] = (
            movable_goals[target_row],
            movable_goals[current_row],
        )

        # Update main goals list to reflect new order
        if self._goals_showing_review:
            self._goals = movable_goals
        else:
            # Rebuild _goals with updated order for incomplete goals
            completed = [g for g in self._goals if g.is_completed]
            self._goals = movable_goals + completed

        # Re-render and move cursor
        self._render_goals_table()
        focused.move_cursor(row=target_row)

        # Persist to database
        new_orders = {goal.id: idx for idx, goal in enumerate(movable_goals)}
        _ = goals_db.update_sort_orders(new_orders)

    def action_open_goals_setup(self) -> None:
        """Open the weekly goals setup modal."""
        focused = self.focused
        if not isinstance(focused, DataTable) or focused.id != "goals-table":
            self.notify("Focus on goals panel first", severity="warning")
            return

        today = date.today()
        week_start = goals_db.get_week_start(today)
        goals = goals_db.get_goals_for_week(week_start)
        metrics = goals_db.get_week_metrics(week_start)

        _ = self.push_screen(
            WeeklyGoalsSetupModal(week_start, goals, metrics),
            self._handle_setup_complete,
        )

    def _handle_setup_complete(self, result: dict[str, object] | None) -> None:
        """Handle the result from the weekly goals setup modal."""
        if not result:
            return

        week_start: date = result["week_start"]  # pyright: ignore[reportAssignmentType]
        goals_from_modal: list[goals_db.Goal] = result["goals"]  # pyright: ignore[reportAssignmentType]

        # Get existing goals for comparison
        existing_goals = {g.id: g for g in goals_db.get_goals_for_week(week_start)}
        modal_goal_ids = {g.id for g in goals_from_modal if g.id}

        # Delete goals that were removed in the modal
        for goal_id in existing_goals:
            if goal_id not in modal_goal_ids:
                _ = goals_db.delete_goal(goal_id)

        # Create or update goals with their per-goal estimates
        for i, goal in enumerate(goals_from_modal):
            if not goal.id:
                # New goal - create and then update estimates
                new_id = goals_db.create_goal(goal.content, week_start)
                if goal.h2_2025_estimate is not None or goal.predicted_time is not None:
                    _ = goals_db.update_goal_estimates(
                        new_id, goal.h2_2025_estimate, goal.predicted_time
                    )
            elif goal.id in existing_goals:
                existing = existing_goals[goal.id]
                if existing.content != goal.content:
                    _ = goals_db.update_goal_content(goal.id, goal.content)
                # Always update per-goal estimates
                _ = goals_db.update_goal_estimates(
                    goal.id, goal.h2_2025_estimate, goal.predicted_time
                )

        # Update sort orders based on modal order
        new_goals = goals_db.get_goals_for_week(week_start)
        # Map modal order to new goals
        modal_order = {g.content: i for i, g in enumerate(goals_from_modal)}
        new_orders = {}
        for g in new_goals:
            if g.content in modal_order:
                new_orders[g.id] = modal_order[g.content]
            else:
                new_orders[g.id] = len(modal_order)
        _ = goals_db.update_sort_orders(new_orders)

        self.notify("Goals saved!")
        self._refresh_goals()

    def _handle_review_complete(self, result: dict[str, object] | None) -> None:
        """Handle the result from the weekly review modal."""
        if not result:
            self._refresh_goals()
            return

        goal_completions: dict[str, bool] = result.get("goal_completions", {})  # pyright: ignore[reportAssignmentType]
        goal_actual_times: dict[str, float | None] = result.get("goal_actual_times", {})  # pyright: ignore[reportAssignmentType]

        # Update goal completion statuses
        for goal_id, is_completed in goal_completions.items():
            _ = goals_db.update_goal_completion(goal_id, is_completed)

        # Update per-goal actual times
        for goal_id, actual_time in goal_actual_times.items():
            _ = goals_db.update_goal_actual_time(goal_id, actual_time)

        self.notify("Review saved!")
        self._refresh_goals()


def main():
    app = StatusDashboard()
    app.run()


if __name__ == "__main__":
    main()
