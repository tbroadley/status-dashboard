import asyncio
import json
import logging
import logging.handlers
import os
import re
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from itertools import groupby
from pathlib import Path
from typing import ClassVar, ParamSpec, TypeAlias, TypeVar, cast, final, override

from dotenv import find_dotenv, load_dotenv
from rich.markup import escape
from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Container, VerticalScroll
from textual.coordinate import Coordinate
from textual.css.query import NoMatches
from textual.timer import Timer  # used for cast of debounce handles
from textual.widgets import DataTable, Footer as TextualFooter, Static
from textual.widgets._footer import FooterKey, FooterLabel, KeyGroup

from status_dashboard import aws_login, credentials, notifications
from status_dashboard.clients import github, linear
from status_dashboard.clients import tasks as sheets
from status_dashboard.task_store import Rows, SignInRequired, StoreError
from status_dashboard.undo import (
    LinearAssignAction,
    LinearMoveAction,
    LinearSetStateAction,
    TaskEditAction,
    UndoStack,
)
from status_dashboard.widgets.create_modals import (
    ConfirmationModal,
    CreateLinearIssueModal,
    CreateTodoistTaskModal,
    EditTodoistTaskModal,
)


P = ParamSpec("P")
T = TypeVar("T")


def _get_config_dir() -> Path:
    """Get the config directory, following XDG conventions."""
    xdg_config = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg_config) if xdg_config else Path.home() / ".config"
    return base / "status-dashboard"


# Load .env from XDG config directory, falling back to cwd for development
_config_env = _get_config_dir() / ".env"
if _config_env.exists():
    _ = load_dotenv(_config_env)
else:
    _ = load_dotenv(find_dotenv(usecwd=True))


def _load_hidden_review_requests() -> set[tuple[str, int]]:
    """Load hidden review requests from HIDDEN_REVIEW_REQUESTS env var (JSON array of [repo, pr_number])."""
    raw = os.environ.get("HIDDEN_REVIEW_REQUESTS", "[]")
    try:
        items = cast(list[list[str | int]], json.loads(raw))
        return {(str(repo), int(pr_num)) for repo, pr_num in items}
    except (json.JSONDecodeError, ValueError, TypeError):
        return set()


HIDDEN_REVIEW_REQUESTS = _load_hidden_review_requests()


def _hidden_prs_file() -> Path:
    """Path to the JSON file storing PRs permanently hidden from My PRs."""
    return _get_config_dir() / "hidden_prs.json"


def _load_hidden_prs() -> set[tuple[str, int]]:
    """Load permanently hidden PRs (JSON array of [repo, pr_number])."""
    try:
        raw = _hidden_prs_file().read_text()
        items = cast(list[list[str | int]], json.loads(raw))
        return {(str(repo), int(pr_num)) for repo, pr_num in items}
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return set()


def _save_hidden_prs(hidden: set[tuple[str, int]]) -> bool:
    """Persist the set of hidden PRs to disk. Returns True on success."""
    try:
        path = _hidden_prs_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = sorted([repo, num] for repo, num in hidden)
        _ = path.write_text(json.dumps(payload, indent=2))
        return True
    except OSError:
        _logger.warning("Failed to save hidden PRs", exc_info=True)
        return False


HIDDEN_PRS = _load_hidden_prs()

# Shorten Linear state display names to fit column width
LINEAR_STATE_SHORT = {
    "In Progress": "progress",
    "In Review": "review",
    "Todo": "todo",
    "Backlog": "backlog",
}


def _load_blocked_review_teams() -> set[str]:
    """Load blocked teams from BLOCKED_REVIEW_TEAMS env var (JSON array of team slugs)."""
    raw = os.environ.get("BLOCKED_REVIEW_TEAMS", "[]")
    try:
        teams = cast(list[str], json.loads(raw))
        return {str(team) for team in teams}
    except (json.JSONDecodeError, ValueError, TypeError):
        return set()


BLOCKED_REVIEW_TEAMS = _load_blocked_review_teams()


def _load_due_notifications_enabled() -> bool:
    """Whether to send desktop notifications when a Todoist task's due time arrives."""
    raw = os.environ.get("TODOIST_DUE_NOTIFICATIONS", "true")
    return raw.strip().lower() not in ("0", "false", "no", "off")


TODOIST_DUE_NOTIFICATIONS = _load_due_notifications_enabled()


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


def _get_local_commit() -> str | None:
    """Get the git commit SHA of the currently running code.

    Tries two strategies:
    1. git rev-parse HEAD in the source directory (works for dev installs)
    2. direct_url.json in the package dist-info (works for uv tool installs from git)
    """

    # Strategy 1: git rev-parse in the source directory
    source_dir = Path(__file__).parent
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=source_dir,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # Strategy 2: read commit hash from direct_url.json (uv tool installs from git)
    from importlib import metadata as importlib_metadata

    try:
        dist = importlib_metadata.distribution("status-dashboard")
        direct_url_text = dist.read_text("direct_url.json")
        if direct_url_text:
            direct_url = cast(dict[str, object], json.loads(direct_url_text))
            vcs_info_raw = direct_url.get("vcs_info")
            if isinstance(vcs_info_raw, dict):
                vcs_info = cast(dict[str, object], vcs_info_raw)
                commit_id = vcs_info.get("commit_id")
                if isinstance(commit_id, str):
                    return commit_id
    except (importlib_metadata.PackageNotFoundError, KeyError, json.JSONDecodeError):
        pass

    return None


def _get_remote_commit() -> str | None:
    """Fetch the latest commit SHA from the GitHub repository's main branch."""

    try:
        result = subprocess.run(
            [
                "gh",
                "api",
                "repos/tbroadley/status-dashboard/commits/main",
                "-H",
                "Accept: application/vnd.github.sha",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            _logger.warning("Failed to fetch remote commit: %s", result.stderr.strip())
            return None
        return result.stdout.strip() or None
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        _logger.warning("Failed to fetch remote commit: %s", e)
        return None


@dataclass(eq=False)
class PendingTaskEdit:
    """A task edit shown on screen whose save hasn't finished."""

    edit: sheets.Edit
    on_saved: Callable[[bool], None] | None = None


_BindingInfo: TypeAlias = tuple[Binding, bool, str | None]


class Footer(TextualFooter):
    """Custom Footer that shows global bindings before pane-specific ones."""

    @override
    def compose(self) -> ComposeResult:
        if not self._bindings_ready:
            return
        app = cast(App[object], self.app)
        active_bindings = self.screen.active_bindings

        def sort_key(item: tuple[str, tuple[object, Binding, bool, str | None]]) -> int:
            node = item[1][0]
            return 0 if isinstance(node, StatusDashboard) else 1

        sorted_items = sorted(active_bindings.items(), key=sort_key)
        bindings: list[_BindingInfo] = [
            (binding, enabled, tooltip)
            for (_, binding, enabled, tooltip) in (v for _, v in sorted_items)
            if binding.show
        ]
        action_to_bindings: defaultdict[str, list[_BindingInfo]] = defaultdict(list)
        for binding, enabled, tooltip in bindings:
            action_to_bindings[binding.action].append((binding, enabled, tooltip))

        num_bindings = len(action_to_bindings)
        self.styles.grid_size_columns = (num_bindings + 1) // 2
        self.styles.grid_size_rows = 2

        for group, multi_bindings_iterable in groupby(
            action_to_bindings.values(),
            lambda multi_bindings_: multi_bindings_[0][0].group,
        ):
            multi_bindings_list = list(multi_bindings_iterable)
            if group is not None and len(multi_bindings_list) > 1:
                with KeyGroup(classes="-compact" if group.compact else ""):
                    for multi_bindings in multi_bindings_list:
                        binding, enabled, tooltip = multi_bindings[0]
                        yield FooterKey(
                            binding.key,
                            app.get_key_display(binding),
                            "",
                            binding.action,
                            disabled=not enabled,
                            tooltip=tooltip or binding.description,
                            classes="-grouped",
                        ).data_bind(compact=TextualFooter.compact)
                yield FooterLabel(group.description)
            else:
                for multi_bindings in multi_bindings_list:
                    binding, enabled, tooltip = multi_bindings[0]
                    yield FooterKey(
                        binding.key,
                        app.get_key_display(binding),
                        binding.description,
                        binding.action,
                        disabled=not enabled,
                        tooltip=tooltip or binding.description,
                    ).data_bind(compact=TextualFooter.compact)
        if self.show_command_palette and app.ENABLE_COMMAND_PALETTE:
            try:
                _node, binding, enabled, tooltip = active_bindings[
                    app.COMMAND_PALETTE_BINDING
                ]
            except KeyError:
                pass
            else:
                yield FooterKey(
                    binding.key,
                    app.get_key_display(binding),
                    binding.description,
                    binding.action,
                    classes="-command-palette",
                    disabled=not enabled,
                    tooltip=binding.tooltip or binding.description,
                )


class VimDataTable(DataTable[str | Text]):
    """DataTable with vim-style navigation (j/k/g/G) and count prefixes (e.g., 5j)."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("g", "cursor_top", "Top", show=False),
        Binding("G", "cursor_bottom", "Bottom", show=False),
    ]

    _vim_count: str

    def __init__(self, *args: object, **kwargs: object) -> None:
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
            if self.cursor_row >= self.row_count - 1:
                cast(App[None], self.app).action_focus_next()
                return
            self.action_cursor_down()

    def key_k(self) -> None:
        count = self._get_and_reset_count()
        for _ in range(count):
            # If at top row, move to previous panel
            if self.cursor_row <= 0:
                cast(App[None], self.app).action_focus_previous()
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

    @override
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

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("x", "app.remove_self_as_reviewer", "Remove & Unsub"),
        Binding("c", "app.copy_pr_link", "Copy Link"),
    ]


class NotificationsDataTable(VimDataTable):
    """DataTable for GitHub notifications with mark as read binding."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("x", "app.mark_notification_read", "Mark Read"),
        Binding("c", "app.copy_pr_link", "Copy Link"),
    ]


class MyPRsDataTable(VimDataTable):
    """DataTable for user's PRs with merge and close bindings."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("m", "app.merge_pr", "Merge"),
        Binding("x", "app.close_pr", "Close"),
        Binding("c", "app.copy_pr_link", "Copy Link"),
        Binding("H", "app.hide_pr", "Hide"),
        Binding("a", "app.run_gated_checks", "Run Checks"),
    ]


class TodoistDataTable(VimDataTable):
    """DataTable for Todoist tasks with defer binding."""

    BINDINGS: ClassVar[list[BindingType]] = [
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
        Binding("t", "app.todoist_go_to_today", "Today"),
        Binding("h", "app.todoist_previous_day", "Prev Day"),
        Binding("l", "app.todoist_next_day", "Next Day"),
        Binding("left", "app.todoist_previous_day", "Prev Day", show=False),
        Binding("right", "app.todoist_next_day", "Next Day", show=False),
    ]


class LinearDataTable(VimDataTable):
    """DataTable for Linear issues with state change bindings."""

    BINDINGS: ClassVar[list[BindingType]] = [
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


class UpdateBanner(Static):
    """Banner showing when a new version is available."""

    DEFAULT_CSS: ClassVar[str] = """
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

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # pyright: ignore[reportArgumentType]
        self._new_version: str | None = None

    def show_update(self, remote_ref: str) -> None:
        self._new_version = remote_ref
        self.update(f"Update available ({remote_ref}) — press R to upgrade")
        _ = self.add_class("-visible")

    def hide(self) -> None:
        _ = self.remove_class("-visible")


class Panel(Container):
    """A panel with a title and data table."""

    panel_title: str
    table_class: type[VimDataTable]

    def __init__(
        self, title: str, panel_id: str, table_class: type[VimDataTable] = VimDataTable
    ):
        super().__init__(id=panel_id)
        self.panel_title = title
        self.table_class = table_class

    @override
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


def _changes_text(additions: int, deletions: int) -> Text:
    """Render a PR's line counts as green additions and red deletions."""
    return Text.assemble((f"+{additions}", "green"), " ", (f"−{deletions}", "red"))


def _is_macos_dark_mode() -> bool | None:
    """Detect macOS system appearance. Returns True for dark, False for light, None if not macOS."""
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            ["defaults", "read", "-g", "AppleInterfaceStyle"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return result.returncode == 0 and "Dark" in result.stdout
    except (OSError, subprocess.TimeoutExpired):
        return None


@final
class StatusDashboard(App[None]):
    """Terminal dashboard for PRs, Todoist, and Linear."""

    CSS: ClassVar[str] = """
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

    .panel-title {
        background: $accent;
        color: $text;
        text-align: center;
        text-style: bold;
        padding: 0 1;
    }

    DataTable {
        height: auto;
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

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("r", "refresh", "Refresh"),
        Binding("z", "undo", "Undo"),
        Binding("R", "restart", "Restart"),
        Binding("q", "quit", "Quit"),
        Binding("ctrl+shift+up", "focus_previous_pane", "Prev Pane", show=False),
        Binding("ctrl+shift+down", "focus_next_pane", "Next Pane", show=False),
    ]

    _undo_stack: UndoStack  # pyright: ignore[reportUninitializedInstanceVariable]
    _my_prs: list[github.PullRequest]  # pyright: ignore[reportUninitializedInstanceVariable]
    _review_requests: list[github.ReviewRequest]  # pyright: ignore[reportUninitializedInstanceVariable]
    _todoist_debounce_handle: Timer | None  # pyright: ignore[reportUninitializedInstanceVariable]
    _todoist_day_debounce_handle: Timer | None  # pyright: ignore[reportUninitializedInstanceVariable]
    _todoist_selected_date: date  # pyright: ignore[reportUninitializedInstanceVariable]
    _todoist_notified: set[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    _todoist_notify_seeded: bool  # pyright: ignore[reportUninitializedInstanceVariable]
    _linear_issues: list[linear.Issue]  # pyright: ignore[reportUninitializedInstanceVariable]
    _linear_debounce_handle: Timer | None  # pyright: ignore[reportUninitializedInstanceVariable]
    _linear_viewer_initials: str | None  # pyright: ignore[reportUninitializedInstanceVariable]
    _gh_notifications: list[github.Notification]  # pyright: ignore[reportUninitializedInstanceVariable]
    _todoist_projects: list[sheets.Project]  # pyright: ignore[reportUninitializedInstanceVariable]
    _linear_team_id: str  # pyright: ignore[reportUninitializedInstanceVariable]
    _linear_pending_move: tuple[str, float, int] | None  # pyright: ignore[reportUninitializedInstanceVariable]
    _last_action_undoable: bool  # pyright: ignore[reportUninitializedInstanceVariable]
    _create_todoist_modal: CreateTodoistTaskModal  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def compose(self) -> ComposeResult:
        yield UpdateBanner(id="update-banner")
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
            yield Panel("Tasks (Today)", "todoist", table_class=TodoistDataTable)
            yield Panel("Linear", "linear", table_class=LinearDataTable)
        yield Footer()

    def _setup_table(self, table: DataTable[str | Text]) -> None:
        """Common table setup."""
        table.cursor_type = "row"
        table.show_cursor = True
        table.zebra_stripes = True

    def __init__(self) -> None:
        super().__init__()
        # Task view model: every stored row as of the last read or save, with
        # edits that haven't been saved yet applied on top, in order. Edits
        # are row functions (see `sheets.Edit`), so the same change that is
        # shown immediately is the one written to S3. `_todoist_tasks` is the
        # derived list the table shows; rebuild it with `_render_todoist_table`.
        self._task_rows: Rows | None = None
        self._task_pending: list[PendingTaskEdit] = []
        self._task_queue: list[PendingTaskEdit] = []
        self._todoist_tasks: list[sheets.Task] = []
        # One writer at a time, so saves reach S3 in the order they were made;
        # edits queued meanwhile go out together in the next save. Each save
        # returns the stored rows, so reads that overlap a save are dropped
        # (they may predate it) rather than retried.
        self._task_writing = False
        self._task_write_version = 0
        # Reads are numbered so a slow, older read never replaces a newer one.
        self._todoist_read_seq = 0
        self._todoist_applied_seq = 0
        self._task_warmed_at = 0.0
        self._task_rows_fresh = False
        # One `aws sso login` at a time, shared by every request waiting on it.
        self._aws_login: asyncio.Task[bool] | None = None
        self._aws_login_failed_at: float | None = None
        dark_mode = _is_macos_dark_mode()
        if dark_mode is not None:
            self.theme = "textual-dark" if dark_mode else "textual-light"

    def on_mount(self) -> None:
        self._undo_stack = UndoStack()
        self._my_prs = []
        self._review_requests = []
        self._todoist_debounce_handle = None
        self._todoist_day_debounce_handle = None
        self._todoist_selected_date = date.today()
        self._todoist_notified = set()
        self._todoist_notify_seeded = False
        self._linear_issues = []
        self._linear_debounce_handle = None
        self._linear_viewer_initials = None
        self._gh_notifications = []
        self._todoist_projects = []
        self._linear_team_id = ""
        self._linear_pending_move = None
        self._last_action_undoable = True

        # Pre-install the Todoist create modal so subsequent opens are instant
        self._create_todoist_modal = CreateTodoistTaskModal()
        self.install_screen(self._create_todoist_modal, "create-todoist-task")  # pyright: ignore[reportUnknownMemberType]

        # Set up table columns - auto-sized based on content
        my_prs = self.query_one("#my-prs-table", MyPRsDataTable)
        _ = my_prs.add_columns(
            "#", "PR", "Title", "Repo", "Status", "CI", "Cmt", "Changes", "Rvw"
        )
        self._setup_table(my_prs)
        my_prs.cursor_foreground_priority = "renderable"

        reviews = self.query_one("#review-requests-table", ReviewRequestsDataTable)
        _ = reviews.add_columns(
            "#", "PR", "Title", "Repo", "Author", "Age", "Changes", "Reviewed"
        )
        self._setup_table(reviews)
        reviews.cursor_foreground_priority = "renderable"

        notifs = self.query_one("#notifications-table", NotificationsDataTable)
        _ = notifs.add_columns("#", "PR", "Title", "Repo", "Reason", "Age")
        self._setup_table(notifs)

        todo = self.query_one("#todoist-table", TodoistDataTable)
        _ = todo.add_columns("#", "!", "", "Time", "#C", "📝", "🔗", "Task")
        self._setup_table(todo)

        lin = self.query_one("#linear-table", LinearDataTable)
        _ = lin.add_columns("#", "ID", "Title", "Status", "Owner")
        self._setup_table(lin)

        # Show the list saved by the last run while the first read is in flight.
        if (saved := sheets.saved_rows()) is not None:
            self._set_task_rows(saved, fresh=False)
            self._render_todoist_table()

        self.refresh_all()
        _ = self.set_interval(60, self.refresh_all)
        _ = self._check_for_updates()
        _ = self.set_interval(30 * 60, self._check_for_updates)
        if TODOIST_DUE_NOTIFICATIONS:
            _ = self._check_todoist_due_times()
            _ = self.set_interval(60, self._check_todoist_due_times)

    def refresh_all(self) -> None:
        _ = self._refresh_my_prs()
        _ = self._refresh_review_requests()
        _ = self._refresh_gh_notifications()
        _ = self._refresh_todoist()
        _ = self._refresh_linear()

    @work(exclusive=False)
    async def _check_for_updates(self) -> None:
        """Check for updates by comparing local and remote git commits."""
        local_commit = await asyncio.to_thread(_get_local_commit)
        if not local_commit:
            return

        remote_commit = await asyncio.to_thread(_get_remote_commit)
        if not remote_commit:
            return

        if local_commit != remote_commit:
            banner = self.query_one("#update-banner", UpdateBanner)
            banner.show_update(remote_commit[:7])

    async def _task_request(
        self, function: Callable[P, T], *args: P.args, **kwargs: P.kwargs
    ) -> T | None:
        try:
            try:
                return await asyncio.to_thread(function, *args, **kwargs)
            except SignInRequired:
                if not await self._aws_sign_in():
                    raise
                # The request failed before reaching S3, so it is safe to repeat.
                return await asyncio.to_thread(function, *args, **kwargs)
        except (StoreError, OSError) as error:
            message = (
                str(error)
                if isinstance(error, StoreError)
                else "Local task storage I/O failed."
            )
            logging.getLogger(__name__).warning("Task storage: %s", message)
            self.notify(
                message, title="Task storage unavailable", severity="error", timeout=10
            )
            return None

    async def _aws_sign_in(self) -> bool:
        """Run `aws sso login`, or join the one already running.

        Returns whether it succeeded. After a failed or ignored sign-in, no
        new one starts for a while, so the browser isn't opened repeatedly.
        """
        if self._aws_login is None:
            if not aws_login.enabled():
                return False
            failed_at = self._aws_login_failed_at
            if (
                failed_at is not None
                and time.monotonic() - failed_at < aws_login.RETRY_AFTER_SECONDS
            ):
                return False
            self._aws_login = asyncio.create_task(self._run_aws_login())
        return await asyncio.shield(self._aws_login)

    async def _run_aws_login(self) -> bool:
        try:
            self.notify(
                "Running `aws sso login`; approve it in your browser.",
                title="AWS sign-in expired",
                timeout=10,
            )
            _ = await asyncio.to_thread(
                notifications.send_desktop_notification,
                "Status Dashboard",
                "AWS sign-in expired. Running aws sso login; approve it in your browser.",
            )
            signed_in = await aws_login.sso_login()
            self._aws_login_failed_at = None if signed_in else time.monotonic()
            return signed_in
        finally:
            self._aws_login = None

    def _queue_task_edit(
        self,
        edit: sheets.Edit,
        on_saved: Callable[[bool], None] | None = None,
    ) -> PendingTaskEdit:
        """Show `edit` now and save it in the background.

        `on_saved` is called with whether the save succeeded and the edit found
        its task. If it didn't, the edit is dropped and the view reverts.
        """
        pending = PendingTaskEdit(edit, on_saved)
        self._task_pending.append(pending)
        self._task_queue.append(pending)
        if not self._task_writing:
            self._task_writing = True
            _ = self._save_task_edits()
        return pending

    @work(exclusive=False)
    async def _save_task_edits(self) -> None:
        """Save queued edits, batching those that queue up while a save runs."""
        try:
            while self._task_queue:
                batch, self._task_queue = self._task_queue, []
                outcome = await self._task_request(
                    sheets.save, [pending.edit for pending in batch]
                )
                self._task_write_version += 1
                for pending in batch:
                    self._task_pending.remove(pending)
                if outcome is None:
                    results = [False] * len(batch)
                    # The save may or may not have landed; show what S3 has.
                    _ = self._refresh_todoist()
                else:
                    results, rows = outcome
                    self._set_task_rows(rows)
                self._render_todoist_table()
                for pending, saved in zip(batch, results):
                    if pending.on_saved:
                        pending.on_saved(saved)
        finally:
            self._task_writing = False

    def _set_task_rows(self, rows: Rows, fresh: bool = True) -> None:
        """Show `rows`; `fresh` means S3 just returned them, as opposed to a
        copy saved by an earlier run."""
        self._task_rows = rows
        self._todoist_projects = sheets.projects_in(rows)
        self._task_rows_fresh = fresh
        self._update_todoist_panel_title()

    def _task_view_rows(self) -> Rows | None:
        """The stored rows with unsaved edits applied."""
        if self._task_rows is None or not self._task_pending:
            return self._task_rows
        rows = [row.copy() for row in self._task_rows]
        for pending in self._task_pending:
            _ = pending.edit(rows)
        return rows

    def _warm_task_store(self) -> None:
        """Reconnect to S3 while the user is in the task list, before they act."""
        now = time.monotonic()
        if now - self._task_warmed_at < 1:
            return
        self._task_warmed_at = now
        _ = self.run_worker(asyncio.to_thread(sheets.warm), exclusive=False)

    @work(exclusive=False)
    async def _refresh_my_prs(self) -> None:
        prs = await asyncio.to_thread(github.get_my_prs)
        self._my_prs = prs
        self._render_my_prs_table()

    def _render_my_prs_table(self, preserve_cursor: bool = True) -> None:
        table = self.query_one("#my-prs-table", MyPRsDataTable)
        selected_key = self._get_selected_row_key(table) if preserve_cursor else None

        _ = table.clear()

        visible_prs = self._visible_my_prs()

        if not visible_prs:
            _ = table.add_row(
                "",
                "",
                Text("No open PRs", style="dim italic"),
                "",
                "",
                "",
                "",
                "",
                "",
            )
        else:
            for pr in visible_prs:
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

                if pr.mergeable == "CONFLICTING":
                    ci_display = "conflict"
                else:
                    ci_display = {
                        "SUCCESS": "pass",
                        github.AWAITING_APPROVAL: "pass*",
                        "FAILURE": "fail",
                        "PENDING": "...",
                        "EXPECTED": "...",
                    }.get(pr.ci_status or "", "")

                comment_display = (
                    str(pr.unresolved_comment_count)
                    if pr.unresolved_comment_count > 0
                    else ""
                )

                reviewers_display = ", ".join(pr.reviewers) if pr.reviewers else ""

                repo = _short_repo(pr.repository)
                title = pr.title[:40] + "…" if len(pr.title) > 40 else pr.title
                _ = table.add_row(
                    "",
                    f"#{pr.number}",
                    title,
                    repo,
                    status,
                    ci_display,
                    comment_display,
                    _changes_text(pr.additions, pr.deletions),
                    reviewers_display,
                    key=pr.url,
                )

            if selected_key:
                _ = self._restore_cursor_by_key(table, selected_key)
        table.refresh_line_numbers()
        self._render_notifications_table()

    def _visible_my_prs(self) -> list[github.PullRequest]:
        return [
            pr for pr in self._my_prs if (pr.repository, pr.number) not in HIDDEN_PRS
        ]

    def _visible_review_requests(self) -> list[github.ReviewRequest]:
        def _is_visible(pr: github.ReviewRequest) -> bool:
            if (pr.repository, pr.number) in HIDDEN_REVIEW_REQUESTS:
                return False
            # Hide if all requested teams are blocked (and there are teams)
            if pr.requested_teams and all(
                team in BLOCKED_REVIEW_TEAMS for team in pr.requested_teams
            ):
                return False
            return True

        return [pr for pr in self._review_requests if _is_visible(pr)]

    def _visible_notifications(self) -> list[github.Notification]:
        """Notifications, minus PRs already shown in My PRs or Review Requests."""
        shown = {
            (pr.repository.lower(), pr.number)
            for pr in [*self._visible_my_prs(), *self._visible_review_requests()]
        }
        return [
            n
            for n in self._gh_notifications
            if n.pr_number is None or (n.repository.lower(), n.pr_number) not in shown
        ]

    @work(exclusive=False)
    async def _refresh_review_requests(self) -> None:
        prs = await asyncio.to_thread(github.get_review_requests)
        self._review_requests = prs
        self._render_review_requests_table()

    def _render_review_requests_table(self, preserve_cursor: bool = True) -> None:
        table = self.query_one("#review-requests-table", ReviewRequestsDataTable)
        selected_key = self._get_selected_row_key(table) if preserve_cursor else None

        _ = table.clear()

        visible_prs = self._visible_review_requests()

        if not visible_prs:
            _ = table.add_row(
                "",
                "",
                Text("No review requests", style="dim italic"),
                "",
                "",
                "",
                "",
                "",
            )
        else:
            for pr in visible_prs:
                repo = _short_repo(pr.repository)
                age = github._relative_time(pr.created_at)  # pyright: ignore[reportPrivateUsage]
                title = pr.title[:40] + "…" if len(pr.title) > 40 else pr.title
                reviewed = "✓" if pr.has_other_review else ""
                _ = table.add_row(
                    "",
                    f"#{pr.number}",
                    title,
                    repo,
                    f"@{pr.author}",
                    age,
                    _changes_text(pr.additions, pr.deletions),
                    reviewed,
                    key=f"review:{pr.repository}:{pr.number}:{pr.url}",
                )

            if selected_key:
                _ = self._restore_cursor_by_key(table, selected_key)
        table.refresh_line_numbers()
        self._render_notifications_table()

    @work(exclusive=False)
    async def _refresh_gh_notifications(self) -> None:
        notifications = await asyncio.to_thread(github.get_notifications)
        self._gh_notifications = notifications or []
        self._render_notifications_table()

    def _render_notifications_table(self, preserve_cursor: bool = True) -> None:
        table = self.query_one("#notifications-table", NotificationsDataTable)
        selected_key = self._get_selected_row_key(table) if preserve_cursor else None

        _ = table.clear()

        visible_notifications = self._visible_notifications()
        if not visible_notifications:
            _ = table.add_row(
                "", "", Text("No notifications", style="dim italic"), "", "", ""
            )
        else:
            for notif in visible_notifications:
                repo = _short_repo(notif.repository)
                age = github._relative_time(notif.updated_at)  # pyright: ignore[reportPrivateUsage]
                number = notif.pr_number or notif.issue_number
                pr_display = f"#{number}" if number else ""
                reason = (
                    f"{notif.reason} (issue)" if notif.issue_number else notif.reason
                )
                title = notif.title[:40] + "…" if len(notif.title) > 40 else notif.title
                _ = table.add_row(
                    "",
                    pr_display,
                    title,
                    repo,
                    reason,
                    age,
                    key=f"notif:{notif.id}:{notif.repository}:{notif.pr_number or ''}:{notif.url}",
                )

            if selected_key:
                _ = self._restore_cursor_by_key(table, selected_key)
        table.refresh_line_numbers()
        self._recompute_layout()

    def _get_selected_row_key(self, table: DataTable[str | Text]) -> str | None:
        if table.row_count == 0:
            return None
        cell_key = table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0))
        if cell_key.row_key and cell_key.row_key.value:
            return str(cell_key.row_key.value)
        return None

    def _restore_cursor_by_key(
        self, table: DataTable[str | Text], row_key: str | None
    ) -> bool:
        if not row_key or table.row_count == 0:
            return False
        for idx in range(table.row_count):
            cell_key = table.coordinate_to_cell_key(Coordinate(idx, 0))
            if cell_key.row_key and str(cell_key.row_key.value) == row_key:
                table.move_cursor(row=idx)
                return True
        return False

    @staticmethod
    def _todoist_row_key(task: sheets.Task) -> str:
        return f"todoist:{task.id}:{task.url}"

    def _selected_todoist_task(self) -> sheets.Task | None:
        """The task under the cursor, if the task table is focused."""
        focused = self.focused
        if not isinstance(focused, VimDataTable) or focused.id != "todoist-table":
            return None
        key = self._get_selected_row_key(focused)
        return next(
            (t for t in self._todoist_tasks if self._todoist_row_key(t) == key), None
        )

    @work(exclusive=False)
    async def _refresh_todoist(self) -> None:
        write_version = self._task_write_version
        self._todoist_read_seq += 1
        read_seq = self._todoist_read_seq
        rows = await self._task_request(sheets.load)
        if rows is None or read_seq < self._todoist_applied_seq:
            return
        if self._task_writing or write_version != self._task_write_version:
            # The read may predate a save, which brings its own fresher rows.
            return
        self._todoist_applied_seq = read_seq
        self._set_task_rows(rows)
        self._render_todoist_table()
        if TODOIST_DUE_NOTIFICATIONS and not self._todoist_notify_seeded:
            _ = self._check_todoist_due_times()

    def _derive_todoist_tasks(self) -> list[sheets.Task]:
        rows = self._task_view_rows()
        if rows is None:
            return []
        return sheets.tasks_due(rows, self._todoist_selected_date)

    @work(exclusive=False)
    async def _check_todoist_due_times(self) -> None:
        """Notify when a task due today reaches its due time.

        Uses the rows from the last refresh, whatever day is on screen. On the
        first run, tasks already past their due time are recorded silently so
        the user isn't spammed about earlier tasks when the app starts. After
        that, a desktop notification fires once per task as its due time
        arrives.
        """
        rows = self._task_view_rows()
        if rows is None:
            return
        today = date.today()
        today_str = today.isoformat()
        now = datetime.now()

        for task in sheets.tasks_due(rows, today):
            if task.due_date != today_str or not task.due_time:
                continue

            hour, minute = (int(part) for part in task.due_time.split(":"))
            due_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if due_dt > now:
                continue

            notify_key = f"{task.id}:{task.due_date}"
            if notify_key in self._todoist_notified:
                continue
            self._todoist_notified.add(notify_key)

            if self._todoist_notify_seeded:
                _ = await asyncio.to_thread(
                    notifications.send_desktop_notification,
                    "Todoist task due",
                    task.content,
                    "Glass",
                )

        self._todoist_notify_seeded = True

    def _update_todoist_panel_title(self) -> None:
        panel = self.query_one("#todoist", Panel)
        title_widget = panel.query_one(".panel-title", Static)
        today = date.today()
        selected = self._todoist_selected_date
        if selected == today:
            title = "Tasks (Today)"
        elif selected == today - timedelta(days=1):
            title = "Tasks (Yesterday)"
        elif selected == today + timedelta(days=1):
            title = "Tasks (Tomorrow)"
        else:
            title = f"Tasks ({selected.strftime('%a %b %d')})"
        if self._task_rows is not None and not self._task_rows_fresh:
            title += " · saved copy, not yet refreshed"
        title_widget.update(title)

    def action_todoist_previous_day(self) -> None:
        focused = self.focused
        if not isinstance(focused, VimDataTable) or focused.id != "todoist-table":
            return
        self._todoist_selected_date -= timedelta(days=1)
        self._update_todoist_panel_title()
        self._debounced_refresh_todoist()

    def action_todoist_next_day(self) -> None:
        focused = self.focused
        if not isinstance(focused, VimDataTable) or focused.id != "todoist-table":
            return
        self._todoist_selected_date += timedelta(days=1)
        self._update_todoist_panel_title()
        self._debounced_refresh_todoist()

    def action_todoist_go_to_today(self) -> None:
        focused = self.focused
        if not isinstance(focused, VimDataTable) or focused.id != "todoist-table":
            return
        today = date.today()
        if self._todoist_selected_date == today:
            return
        self._todoist_selected_date = today
        self._update_todoist_panel_title()
        self._debounced_refresh_todoist()

    def _debounced_refresh_todoist(self) -> None:
        """Show the new day from loaded rows now; refresh once navigation settles."""
        self._render_todoist_table()
        if self._todoist_day_debounce_handle:
            self._todoist_day_debounce_handle.stop()
        self._todoist_day_debounce_handle = self.set_timer(
            0.3, self._fire_todoist_day_refresh
        )

    def _fire_todoist_day_refresh(self) -> None:
        self._todoist_day_debounce_handle = None
        _ = self._refresh_todoist()

    def _render_todoist_table(self, select_key: str | None = None) -> None:
        """Rebuild the task list and redraw it.

        The cursor goes to `select_key` if given, otherwise stays on the task it
        was on. If that task is gone, the cursor keeps its row position, so the
        task below slides under it.
        """
        self._todoist_tasks = self._derive_todoist_tasks()
        table = self.query_one("#todoist-table", TodoistDataTable)
        previous_row = table.cursor_row
        selected_key = select_key or self._get_selected_row_key(table)

        _ = table.clear()

        today = date.today()
        selected = self._todoist_selected_date
        if self._task_rows is None:
            _ = table.add_row(
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                Text(
                    "Tasks not loaded — refresh to retry",
                    style="dim italic",
                ),
            )
        elif not self._todoist_tasks:
            if selected == today:
                empty_msg = "No tasks for today"
            elif selected == today + timedelta(days=1):
                empty_msg = "No tasks for tomorrow"
            else:
                empty_msg = f"No tasks on {selected.strftime('%a %b %d')}"
            _ = table.add_row(
                "", "", "", "", "", "", "", Text(empty_msg, style="dim italic")
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
                desc_display = "📝" if task.description else ""
                content = (
                    task.content[:60] + "…" if len(task.content) > 60 else task.content
                )
                _ = table.add_row(
                    "",
                    overdue,
                    checkbox,
                    time_display,
                    comment_display,
                    desc_display,
                    link_display,
                    content,
                    key=self._todoist_row_key(task),
                )

            if not self._restore_cursor_by_key(table, selected_key):
                table.move_cursor(row=min(previous_row, table.row_count - 1))
        table.refresh_line_numbers()
        self._recompute_layout()

    @work(exclusive=False)
    async def _refresh_linear(self) -> None:
        issues = await asyncio.to_thread(linear.get_my_issues)
        self._linear_issues = [
            i for i in issues if i.state not in ("Done", "Canceled", "Duplicate")
        ]
        self._render_linear_table()

    def _render_linear_table(self, preserve_cursor: bool = True) -> None:
        table = self.query_one("#linear-table", LinearDataTable)
        selected_key = self._get_selected_row_key(table) if preserve_cursor else None

        _ = table.clear()

        if not self._linear_issues:
            _ = table.add_row(
                "", "", Text("No active issues", style="dim italic"), "", ""
            )
        else:
            for issue in self._linear_issues:
                assignee = issue.assignee_initials or ""
                title = issue.title[:50] + "…" if len(issue.title) > 50 else issue.title
                state_display = LINEAR_STATE_SHORT.get(issue.state, issue.state)
                _ = table.add_row(
                    "",
                    issue.identifier,
                    title,
                    state_display,
                    assignee,
                    key=f"linear:{issue.id}:{issue.team_id}:{issue.url}",
                )

            if selected_key:
                _ = self._restore_cursor_by_key(table, selected_key)
        table.refresh_line_numbers()
        self._recompute_layout()

    # Order in which panels yield vertical space (become scrollable) when the
    # terminal is too short to show every panel in full. Earliest = made
    # scrollable first.
    _SHRINK_ORDER: ClassVar[list[str]] = [
        "linear",
        "notifications",
        "review-requests",
        "my-prs",
        "todoist",
    ]

    def _recompute_layout(self) -> None:
        """Size each panel's table so panels expand to fill available space,
        and become scrollable in a fixed priority order when space is tight.

        Any spare whitespace is given to My PRs so it grows rather than leaving
        an empty gap. When there isn't room for everything, panels shrink (and
        gain a scrollbar) in ``_SHRINK_ORDER``: Linear first, Todoist last.
        """
        try:
            scroll = self.query_one(VerticalScroll)
        except NoMatches:
            return
        avail = scroll.size.height
        if avail <= 0:
            return

        panel_ids = self._SHRINK_ORDER
        try:
            tables = {
                pid: self.query_one(f"#{pid}-table", VimDataTable) for pid in panel_ids
            }
        except NoMatches:
            return

        # Fixed chrome per panel, independent of visible rows:
        # border (2) + title (1) + margin-bottom (1) + table header (1) = 5
        PANEL_CHROME = 5
        # Number of data rows each panel would need to show all its content.
        natural = {pid: max(t.row_count, 1) for pid, t in tables.items()}
        # Total data rows displayable across all panels without an outer scroll.
        budget = avail - PANEL_CHROME * len(panel_ids)

        # Minimum data rows a panel keeps when scrollable. Todoist keeps up to
        # 3 (1/2/3 items -> 1/2/3 tall; 4+ -> 3 tall and scrolls); every other
        # panel can shrink to a single row.
        floors = {pid: 1 for pid in panel_ids}
        floors["todoist"] = 3
        floors = {pid: min(floors[pid], natural[pid]) for pid in panel_ids}

        shown = dict(floors)
        surplus = budget - sum(shown.values())

        # Grow panels toward their natural height in keep-priority order (the
        # reverse of the shrink order): the most-protected panel fills first,
        # so the least-protected (Linear) is the first to be left scrolling.
        for pid in reversed(panel_ids):
            if surplus <= 0:
                break
            give = min(natural[pid] - shown[pid], surplus)
            shown[pid] += give
            surplus -= give

        # Leftover whitespace expands My PRs rather than sitting empty.
        if surplus > 0:
            shown["my-prs"] += surplus

        for pid, table in tables.items():
            table.styles.height = shown[pid] + 1  # +1 for the header row

    def on_resize(self, event: events.Resize) -> None:
        del event
        self._recompute_layout()

    def action_refresh(self) -> None:
        self.refresh_all()
        self.notify("Refreshing...")

    def action_restart(self) -> None:
        _ = self._do_upgrade_and_restart()

    def action_focus_previous_pane(self) -> None:
        """Move focus to the previous pane."""
        tables = list(self.query(VimDataTable))
        if not tables:
            return
        focused = self.focused
        if not isinstance(focused, VimDataTable) or focused not in tables:
            _ = tables[-1].focus()
            return
        current_idx = tables.index(focused)
        prev_idx = (current_idx - 1) % len(tables)
        _ = tables[prev_idx].focus()

    def action_focus_next_pane(self) -> None:
        """Move focus to the next pane."""
        tables = list(self.query(VimDataTable))
        if not tables:
            return
        focused = self.focused
        if not isinstance(focused, VimDataTable) or focused not in tables:
            _ = tables[0].focus()
            return
        current_idx = tables.index(focused)
        next_idx = (current_idx + 1) % len(tables)
        _ = tables[next_idx].focus()

    def _is_uv_tool(self) -> bool:
        """Check if this app is installed as a uv tool."""
        import shutil

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

        if not self._last_action_undoable:
            self.notify("Last action is not undoable", severity="warning")
            return

        action = self._undo_stack.pop()
        if action is None:
            return

        self._last_action_undoable = True
        if isinstance(action, TaskEditAction):
            self._undo_task_edit(action)  # shown at once, no worker needed
        else:
            _ = self._execute_undo(action)

    @work(exclusive=False)
    async def _execute_undo(
        self,
        action: LinearSetStateAction | LinearAssignAction | LinearMoveAction,
    ) -> None:
        """Execute the undo operation for a given Linear action."""
        success = False

        if isinstance(action, LinearSetStateAction):
            success = await asyncio.to_thread(
                linear.set_issue_state_by_name,
                action.issue_id,
                action.team_id,
                action.previous_state,
            )
            if success:
                _ = self._refresh_linear()

        elif isinstance(action, LinearAssignAction):
            success = await asyncio.to_thread(
                linear.assign_issue, action.issue_id, action.previous_assignee_id
            )
            if success:
                _ = self._refresh_linear()

        else:
            success = await asyncio.to_thread(
                linear.update_sort_order, action.issue_id, action.previous_sort_order
            )
            if success:
                _ = self._refresh_linear()

        if success:
            self.notify(f"Undid: {action.description}")
        else:
            self.notify(f"Failed to undo: {action.description}", severity="error")

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        # Moving through tasks usually precedes acting on one.
        table = cast(DataTable[str | Text], event.data_table)
        if table.id == "todoist-table" and table.has_focus:
            self._warm_task_store()

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        if event.widget.id == "todoist-table":
            self._warm_task_store()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Handle Enter key on a row - open edit modal for Todoist, browser for others."""
        key = str(event.row_key.value) if event.row_key.value else ""

        # For Todoist tasks, Enter opens the edit modal instead of browser
        if key.startswith("todoist:"):
            # Format: "todoist:{task_id}:{url}"
            parts = key.split(":", 2)
            if len(parts) >= 2:
                task_id = parts[1]
                self._edit_todoist_task(task_id)
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
            _ = webbrowser.open(url)

    def action_complete_task(self) -> None:
        """Complete the selected Todoist task or Linear issue."""
        focused = self.focused
        if not isinstance(focused, VimDataTable):
            return

        if focused.id == "todoist-table":
            task = self._selected_todoist_task()
            if task is not None:
                self._edit_tasks(
                    sheets.complete_edit(task.id),
                    f"Complete: {task.content[:30]}",
                    "Task completed!",
                    "Failed to complete task",
                )
        elif focused.id == "linear-table":
            if (self._get_selected_row_key(focused) or "").startswith("linear:"):
                self.action_set_linear_state("done")
        else:
            self.notify(
                "Can only complete Todoist tasks or Linear issues", severity="warning"
            )

    def _edit_tasks(
        self,
        edit: sheets.Edit,
        description: str,
        success_message: str | None,
        failure_message: str,
    ) -> None:
        """Apply an undoable edit now, save it in the background, and report.

        The undo entry exists from the start, so undoing right away works: the
        reverting edit is saved after this one. A failed save retracts it.
        """
        rows = self._task_view_rows()
        undo = sheets.revert(rows, edit) if rows is not None else None
        action = TaskEditAction(description=description, edit=undo) if undo else None
        if action is not None:
            self._undo_stack.push(action)
            self._last_action_undoable = True

        def on_saved(saved: bool) -> None:
            if saved:
                if success_message:
                    self.notify(success_message)
                return
            if action is not None:
                self._undo_stack.remove(action)
            self.notify(failure_message, severity="error")

        _ = self._queue_task_edit(edit, on_saved)
        self._render_todoist_table()

    def _undo_task_edit(self, action: TaskEditAction) -> None:
        def on_saved(saved: bool) -> None:
            if saved:
                self.notify(f"Undid: {action.description}")
            else:
                self.notify(f"Failed to undo: {action.description}", severity="error")

        _ = self._queue_task_edit(action.edit, on_saved)
        self._render_todoist_table()

    def action_defer_task(self) -> None:
        """Defer the selected Todoist task to the next working day."""
        focused = self.focused
        if not isinstance(focused, VimDataTable):
            return

        if focused.id != "todoist-table":
            self.notify("Can only defer Todoist tasks", severity="warning")
            return

        task = self._selected_todoist_task()
        if task is not None:
            self._edit_tasks(
                sheets.defer_edit(task.id),
                f"Defer: {task.content[:30]}",
                "Task deferred to next working day",
                "Failed to defer task",
            )

    def action_delete_task(self) -> None:
        """Delete the selected Todoist task."""
        focused = self.focused
        if not isinstance(focused, VimDataTable):
            return

        if focused.id != "todoist-table":
            self.notify("Can only delete Todoist tasks", severity="warning")
            return

        task = self._selected_todoist_task()
        if task is None:
            return
        task_id = task.id
        task_name = (
            task.content[:40] + "..." if len(task.content) > 40 else task.content
        )

        def handle_delete_confirmation(confirmed: bool) -> None:
            if confirmed:
                self._edit_tasks(
                    sheets.delete_edit(task_id),
                    f"Delete: {task.content[:30]}",
                    "Task deleted",
                    "Failed to delete task",
                )

        self.push_screen(  # pyright: ignore[reportCallIssue]
            ConfirmationModal(
                title="Delete Task",
                message=f"Delete '{task_name}'?",
                confirm_label="Delete",
            ),
            handle_delete_confirmation,  # pyright: ignore[reportArgumentType]
        )

    def action_move_task_down(self) -> None:
        """Move the selected Todoist task down in the Today view."""
        self._move_todoist_task(1)

    def action_move_task_up(self) -> None:
        """Move the selected Todoist task up in the Today view."""
        self._move_todoist_task(-1)

    def _move_todoist_task(self, direction: int) -> None:
        """Move the selected Todoist task up (-1) or down (+1) with optimistic UI update."""
        focused = self.focused
        if not isinstance(focused, VimDataTable):
            return

        if focused.id != "todoist-table":
            self.notify("Can only move Todoist tasks", severity="warning")
            return

        task = self._selected_todoist_task()
        if task is None:
            return
        task_ids = [t.id for t in self._todoist_tasks]
        current_row = task_ids.index(task.id)
        target_row = current_row + direction
        if target_row < 0 or target_row >= len(task_ids):
            return

        task_ids[current_row], task_ids[target_row] = (
            task_ids[target_row],
            task_ids[current_row],
        )
        orders = {task_id: index for index, task_id in enumerate(task_ids)}

        def on_saved(saved: bool) -> None:
            if not saved:
                self.notify("Failed to save task order", severity="error")

        _ = self._queue_task_edit(sheets.arrange_edit(orders), on_saved)
        self._render_todoist_table(select_key=self._todoist_row_key(task))
        row_region = focused._get_row_region(target_row)  # pyright: ignore[reportPrivateUsage]
        _ = focused.scroll_to_region(row_region, center=True, animate=False)

    def action_reschedule_overdue_to_today(self) -> None:
        """Reschedule all overdue tasks in the view to today, in one save."""
        focused = self.focused
        if not isinstance(focused, VimDataTable):
            return

        if focused.id != "todoist-table":
            self.notify("Can only reschedule from Todoist panel", severity="warning")
            return

        today = date.today().isoformat()
        overdue = [
            t.id for t in self._todoist_tasks if t.due_date and t.due_date < today
        ]
        if not overdue:
            self.notify("No overdue tasks to reschedule")
            return

        self._edit_tasks(
            sheets.reschedule_to_today_edit(overdue),
            f"Reschedule {len(overdue)} to today",
            f"Rescheduled {len(overdue)} task(s) to today",
            "Failed to reschedule tasks",
        )

    def action_open_task_link(self) -> None:
        """Open the first link in the selected task's title or description."""
        focused = self.focused
        if not isinstance(focused, VimDataTable):
            return

        if focused.id != "todoist-table":
            self.notify("Can only open links from Todoist tasks", severity="warning")
            return

        task = self._selected_todoist_task()
        if task is None:
            return
        url = self._extract_url(task.content) or self._extract_url(task.description)
        if url:
            _ = webbrowser.open(url)
        else:
            self.notify("No link found in task", severity="warning")

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

    def action_set_linear_state(self, state: str) -> None:
        """Set the selected Linear issue's state."""
        focused = self.focused
        if not isinstance(focused, VimDataTable):
            return

        if focused.id != "linear-table":
            self.notify("Can only change state on Linear issues", severity="warning")
            return

        if focused.row_count == 0:
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

            # Optimistic update: find issue and update its state
            issue_to_update: linear.Issue | None = None
            original_state: str | None = None
            removed_issue: linear.Issue | None = None
            removed_index: int = -1

            for idx, issue in enumerate(self._linear_issues):
                if issue.id == issue_id:
                    issue_to_update = issue
                    original_state = issue.state
                    break

            state_display = linear.STATE_NAME_MAP.get(state, state)

            if issue_to_update is not None:
                # Check if new state will remove the issue from view
                if state_display in ("Done", "Canceled", "Duplicate"):
                    # Remove from list
                    for idx, issue in enumerate(self._linear_issues):
                        if issue.id == issue_id:
                            removed_issue = issue
                            removed_index = idx
                            _ = self._linear_issues.pop(idx)
                            break
                else:
                    # Update state in place
                    issue_to_update.state = state_display
                self._render_linear_table()

            # Push undo action immediately so 'z' always undoes the right thing
            undo_action: LinearSetStateAction | None = None
            if original_state:
                description = f"Set {issue_identifier or issue_id} to {state_display}"
                undo_action = LinearSetStateAction(
                    issue_id=issue_id,
                    team_id=team_id,
                    previous_state=original_state,
                    description=description,
                )
                self._undo_stack.push(undo_action)
                self._last_action_undoable = True

            _ = self._do_set_linear_state(
                issue_id,
                team_id,
                state,
                removed_issue,
                removed_index,
                original_state,
                undo_action,
            )

    def _get_row_identifier(self, table: DataTable[str | Text]) -> str:
        """Get the identifier (second column, after line numbers) from the current row."""
        if table.row_count == 0:
            return ""
        try:
            row_data = table.get_row_at(table.cursor_row)
            return str(row_data[1]) if len(row_data) > 1 else ""
        except Exception:
            return ""

    @work(exclusive=False)
    async def _do_set_linear_state(
        self,
        issue_id: str,
        team_id: str,
        state: str,
        removed_issue: linear.Issue | None,
        removed_index: int,
        original_state: str | None,
        undo_action: LinearSetStateAction | None,
    ) -> None:
        state_display = linear.STATE_NAME_MAP.get(state, state)
        success = await asyncio.to_thread(
            linear.set_issue_state, issue_id, team_id, state
        )
        if success:
            self.notify(f"Moved to {state_display}")
        else:
            # Pop the undo action we pushed optimistically (if still there)
            if undo_action is not None:
                _ = self._undo_stack.pop_if_matches(undo_action)
            # Rollback: restore original state or re-insert removed issue
            if removed_issue is not None and removed_index >= 0:
                self._linear_issues.insert(removed_index, removed_issue)
                self._render_linear_table()
            elif original_state is not None:
                # Find the issue and restore its state
                for issue in self._linear_issues:
                    if issue.id == issue_id:
                        issue.state = original_state
                        break
                self._render_linear_table()
            self.notify(f"Failed to set state to {state_display}", severity="error")

    def _get_selected_linear_issue_id(self) -> str | None:
        focused = self.focused
        if not isinstance(focused, VimDataTable) or focused.id != "linear-table":
            return None

        if focused.row_count == 0:
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
        if not isinstance(focused, VimDataTable) or focused.id != "linear-table":
            self.notify("Select a Linear issue first", severity="warning")
            return
        issue_id = self._get_selected_linear_issue_id()
        if not issue_id:
            self.notify("Select a Linear issue first", severity="warning")
            return
        issue_identifier = self._get_row_identifier(focused)

        # Optimistic update: find issue and update assignee_initials
        original_initials: str | None = None
        for issue in self._linear_issues:
            if issue.id == issue_id:
                original_initials = issue.assignee_initials
                # Use cached initials if available, otherwise use "..." placeholder
                issue.assignee_initials = self._linear_viewer_initials or "..."
                break
        self._render_linear_table()

        _ = self._do_assign_linear_issue(
            issue_id,
            assign=True,
            issue_identifier=issue_identifier,
            original_initials=original_initials,
        )

    def action_unassign_linear(self) -> None:
        focused = self.focused
        if not isinstance(focused, VimDataTable) or focused.id != "linear-table":
            self.notify("Select a Linear issue first", severity="warning")
            return
        issue_id = self._get_selected_linear_issue_id()
        if not issue_id:
            self.notify("Select a Linear issue first", severity="warning")
            return
        issue_identifier = self._get_row_identifier(focused)

        # Optimistic update: find issue and clear assignee_initials
        original_initials: str | None = None
        for issue in self._linear_issues:
            if issue.id == issue_id:
                original_initials = issue.assignee_initials
                issue.assignee_initials = ""
                break
        self._render_linear_table()

        _ = self._do_assign_linear_issue(
            issue_id,
            assign=False,
            issue_identifier=issue_identifier,
            original_initials=original_initials,
        )

    @work(exclusive=False)
    async def _do_assign_linear_issue(
        self,
        issue_id: str,
        assign: bool,
        issue_identifier: str | None,
        original_initials: str | None,
    ) -> None:
        # get_issue and get_viewer_id are independent; when assigning, fetch both
        # concurrently. When unassigning we don't need the viewer ID.
        if assign:
            issue, viewer_id = await asyncio.gather(
                asyncio.to_thread(linear.get_issue, issue_id),
                asyncio.to_thread(linear.get_viewer_id),
            )
        else:
            issue = await asyncio.to_thread(linear.get_issue, issue_id)
            viewer_id = None

        assignee_raw = issue.get("assignee") if issue else None
        previous_assignee_id = cast(
            str | None,
            cast(dict[str, object], assignee_raw).get("id") if assignee_raw else None,
        )

        if assign:
            if not viewer_id:
                # Rollback: restore original initials
                for i in self._linear_issues:
                    if i.id == issue_id:
                        i.assignee_initials = original_initials
                        break
                self._render_linear_table()
                self.notify("Failed to get your user ID", severity="error")
                return
            assignee_id = viewer_id

            # Fetch and cache viewer initials for future optimistic updates
            if not self._linear_viewer_initials:
                team_members = await asyncio.to_thread(linear.get_team_members)
                for member in team_members:
                    if member.get("id") == viewer_id:
                        name = member.get("displayName") or member.get("name")
                        if name:
                            self._linear_viewer_initials = linear.get_initials(name)
                            # Update the optimistic placeholder with real initials
                            for i in self._linear_issues:
                                if i.id == issue_id and i.assignee_initials == "...":
                                    i.assignee_initials = self._linear_viewer_initials
                            self._render_linear_table()
                        break
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
            self._last_action_undoable = True
            self.notify("Assigned to you" if assign else "Unassigned")
        else:
            # Rollback: restore original initials
            for i in self._linear_issues:
                if i.id == issue_id:
                    i.assignee_initials = original_initials
                    break
            self._render_linear_table()
            self.notify("Failed to update assignment", severity="error")

    def action_remove_self_as_reviewer(self) -> None:
        """Remove yourself as a reviewer from the selected PR and unsubscribe."""
        focused = self.focused
        if not isinstance(focused, VimDataTable):
            return

        if focused.id != "review-requests-table":
            self.notify("Can only remove self from review requests", severity="warning")
            return

        if focused.row_count == 0:
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

            # Find the review request for optimistic update
            review_to_remove: github.ReviewRequest | None = None
            review_index: int = -1
            for idx, r in enumerate(self._review_requests):
                if r.repository == repo and r.number == pr_number:
                    review_to_remove = r
                    review_index = idx
                    break

            def handle_remove_reviewer_confirmation(confirmed: bool) -> None:
                if confirmed:
                    # Optimistic update: remove review request from list immediately
                    # and drop the PR's notifications, which would otherwise
                    # surface once the review request no longer covers them.
                    removed_notifications = [
                        n
                        for n in self._gh_notifications
                        if n.repository.lower() == repo.lower()
                        and n.pr_number == pr_number
                    ]
                    self._gh_notifications = [
                        n
                        for n in self._gh_notifications
                        if n not in removed_notifications
                    ]
                    if review_to_remove is not None and review_index >= 0:
                        _ = self._review_requests.pop(review_index)
                    self._render_review_requests_table()
                    self._last_action_undoable = False
                    _ = self._do_remove_self_as_reviewer(
                        repo,
                        pr_number,
                        review_to_remove,
                        review_index,
                        removed_notifications,
                    )

            self.push_screen(  # pyright: ignore[reportCallIssue]
                ConfirmationModal(
                    title="Remove Self as Reviewer",
                    message=(
                        f"Remove yourself from {repo} PR #{pr_number} and unsubscribe?"
                    ),
                    confirm_label="Remove",
                ),
                handle_remove_reviewer_confirmation,  # pyright: ignore[reportArgumentType]
            )

    @work(exclusive=False)
    async def _do_remove_self_as_reviewer(
        self,
        repo: str,
        pr_number: int,
        removed_review: github.ReviewRequest | None,
        removed_index: int,
        removed_notifications: list[github.Notification],
    ) -> None:
        success = await asyncio.to_thread(
            github.remove_self_as_reviewer, repo, pr_number
        )
        if success:
            unsubscribed = await asyncio.to_thread(
                github.unsubscribe_from_pr, repo, pr_number
            )
            if unsubscribed:
                self.notify(f"Removed from PR #{pr_number} and unsubscribed")
            else:
                self.notify(
                    f"Removed from PR #{pr_number}, but failed to unsubscribe",
                    severity="warning",
                )
            for notif in removed_notifications:
                _ = await asyncio.to_thread(github.mark_notification_read, notif.id)
        else:
            # Rollback: restore the review request to its original position
            if removed_review is not None and removed_index >= 0:
                self._review_requests.insert(removed_index, removed_review)
            self._gh_notifications.extend(removed_notifications)
            self._render_review_requests_table()
            self.notify("Failed to remove self as reviewer", severity="error")

    def action_merge_pr(self) -> None:
        """Squash merge the selected approved PR."""
        focused = self.focused
        if not isinstance(focused, VimDataTable):
            return

        if focused.id != "my-prs-table":
            self.notify("Can only merge from My PRs", severity="warning")
            return

        if focused.row_count == 0:
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

        # Optimistic update: find and remove PR from list
        removed_pr: github.PullRequest | None = None
        removed_index: int = -1
        for idx, p in enumerate(self._my_prs):
            if p.url == url:
                removed_pr = p
                removed_index = idx
                break

        if removed_pr is not None:
            _ = self._my_prs.pop(removed_index)
            self._render_my_prs_table()

        self._last_action_undoable = False
        _ = self._do_merge_pr(pr.repository, pr.number, removed_pr, removed_index)

    @work(exclusive=False)
    async def _do_merge_pr(
        self,
        repo: str,
        pr_number: int,
        removed_pr: github.PullRequest | None,
        removed_index: int,
    ) -> None:
        success = await asyncio.to_thread(github.squash_merge_pr, repo, pr_number)
        if success:
            self.notify(f"Merged PR #{pr_number}")
        else:
            # Rollback: restore the PR to its original position
            if removed_pr is not None and removed_index >= 0:
                self._my_prs.insert(removed_index, removed_pr)
                self._render_my_prs_table()
            self.notify("Failed to merge PR", severity="error")

    def action_close_pr(self) -> None:
        """Close the selected PR without merging."""
        focused = self.focused
        if not isinstance(focused, VimDataTable):
            return

        if focused.id != "my-prs-table":
            self.notify("Can only close from My PRs", severity="warning")
            return

        if focused.row_count == 0:
            return

        cell_key = focused.coordinate_to_cell_key(Coordinate(focused.cursor_row, 0))
        if not cell_key.row_key or not cell_key.row_key.value:
            return

        url = str(cell_key.row_key.value)

        # Find PR and its index for optimistic update
        pr_to_close: github.PullRequest | None = None
        pr_index: int = -1
        for idx, p in enumerate(self._my_prs):
            if p.url == url:
                pr_to_close = p
                pr_index = idx
                break

        if not pr_to_close:
            return

        pr_title = (
            pr_to_close.title[:40] + "..."
            if len(pr_to_close.title) > 40
            else pr_to_close.title
        )

        def handle_close_confirmation(confirmed: bool) -> None:
            if confirmed:
                # Optimistic update: remove PR from list immediately
                if pr_index >= 0:
                    _ = self._my_prs.pop(pr_index)
                    self._render_my_prs_table()
                _ = self._do_close_pr(
                    pr_to_close.repository, pr_to_close.number, pr_to_close, pr_index
                )

        self.push_screen(  # pyright: ignore[reportCallIssue]
            ConfirmationModal(
                title="Close PR",
                message=f"Close '#{pr_to_close.number} {pr_title}'?",
                confirm_label="Close",
            ),
            handle_close_confirmation,  # pyright: ignore[reportArgumentType]
        )

    @work(exclusive=False)
    async def _do_close_pr(
        self,
        repo: str,
        pr_number: int,
        removed_pr: github.PullRequest | None,
        removed_index: int,
    ) -> None:
        success = await asyncio.to_thread(github.close_pr, repo, pr_number)
        if success:
            self.notify(f"Closed PR #{pr_number}")
        else:
            # Rollback: restore the PR to its original position
            if removed_pr is not None and removed_index >= 0:
                self._my_prs.insert(removed_index, removed_pr)
                self._render_my_prs_table()
            self.notify("Failed to close PR", severity="error")

    def action_run_gated_checks(self) -> None:
        """Approve the checks on my selected PR that are waiting for approval."""
        focused = self.focused
        if not isinstance(focused, VimDataTable) or focused.id != "my-prs-table":
            self.notify("Can only run checks from My PRs", severity="warning")
            return
        if focused.row_count == 0:
            return

        cell_key = focused.coordinate_to_cell_key(Coordinate(focused.cursor_row, 0))
        if not cell_key.row_key or not cell_key.row_key.value:
            return
        url = str(cell_key.row_key.value)
        pr = next((p for p in self._my_prs if p.url == url), None)
        if not pr:
            return

        if not pr.viewer_did_author:
            self.notify("Can only run checks on PRs you authored", severity="warning")
            return
        checks = list(pr.gated_checks)
        if not checks:
            self.notify("No checks waiting to be run", severity="warning")
            return

        check_lines = "\n".join(f"• {escape(c.name)}" for c in checks)

        def handle_confirmation(confirmed: bool) -> None:
            if not confirmed:
                return
            # Optimistic update: the checks are now running.
            previous_status = pr.ci_status
            pr.gated_checks = []
            pr.ci_status = "PENDING"
            self._render_my_prs_table()
            self._last_action_undoable = False
            _ = self._do_run_gated_checks(pr, checks, previous_status)

        self.push_screen(  # pyright: ignore[reportCallIssue]
            ConfirmationModal(
                title=f"Run checks on #{pr.number}",
                message=f"Approve and run {len(checks)} gated "
                + f"check{'s' if len(checks) != 1 else ''}?\n\n{check_lines}",
                confirm_label="Run",
            ),
            handle_confirmation,  # pyright: ignore[reportArgumentType]
        )

    @work(exclusive=False)
    async def _do_run_gated_checks(
        self,
        pr: github.PullRequest,
        checks: list[github.GatedCheck],
        previous_status: str | None,
    ) -> None:
        success = await asyncio.to_thread(github.approve_gated_checks, checks)
        if success:
            self.notify(f"Started {len(checks)} checks on PR #{pr.number}")
        else:
            # Rollback, unless a refresh has already replaced this PR.
            if any(p is pr for p in self._my_prs):
                pr.ci_status = previous_status
                pr.gated_checks = checks
                self._render_my_prs_table()
            self.notify("Failed to run checks", severity="error")

    def action_hide_pr(self) -> None:
        """Permanently hide the selected PR from the My PRs view."""
        focused = self.focused
        if not isinstance(focused, VimDataTable):
            return

        if focused.id != "my-prs-table":
            self.notify("Can only hide from My PRs", severity="warning")
            return

        if focused.row_count == 0:
            return

        cell_key = focused.coordinate_to_cell_key(Coordinate(focused.cursor_row, 0))
        if not cell_key.row_key or not cell_key.row_key.value:
            return

        url = str(cell_key.row_key.value)

        pr = next((p for p in self._my_prs if p.url == url), None)
        if not pr:
            return

        key = (pr.repository, pr.number)
        if key in HIDDEN_PRS:
            return

        # Optimistic update: hide immediately, then persist.
        HIDDEN_PRS.add(key)
        self._render_my_prs_table()
        self._last_action_undoable = False

        if _save_hidden_prs(HIDDEN_PRS):
            self.notify(f"Hid PR #{pr.number}")
        else:
            # Rollback: un-hide and re-render on persistence failure.
            HIDDEN_PRS.discard(key)
            self._render_my_prs_table()
            self.notify("Failed to hide PR", severity="error")

    def action_copy_pr_link(self) -> None:
        """Copy the selected PR's URL to the clipboard."""
        focused = self.focused
        if not isinstance(focused, VimDataTable):
            return

        if focused.id not in (
            "my-prs-table",
            "review-requests-table",
            "notifications-table",
        ):
            self.notify("Can only copy links from PR tables", severity="warning")
            return

        if focused.row_count == 0:
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
        if not isinstance(focused, VimDataTable):
            return

        if focused.id != "notifications-table":
            self.notify("Can only mark notifications as read", severity="warning")
            return

        if focused.row_count == 0:
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
        self._last_action_undoable = False
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
        anchor_key = self._get_selected_row_key(table)
        anchor_row = table.cursor_row

        def handle_result(result: dict[str, str] | None) -> None:
            if result:
                self._handle_todoist_task_created(result, anchor_key, anchor_row)

        self._create_todoist_modal.default_due_date = self._todoist_selected_date
        _ = self.push_screen("create-todoist-task", handle_result)

    def _handle_todoist_task_created(
        self, result: dict[str, str], anchor_key: str | None, anchor_row: int
    ) -> None:
        """Show the new task immediately above the row the cursor was on.

        The task gets its final ID now and is saved with the order shown here,
        so its row keeps the same key and position once the save lands.
        """
        content = result["content"]
        due_string = result["due_string"]
        description = result.get("description", "")

        # The list may have changed while the modal was open; insert relative to
        # the row the cursor was on rather than its old index.
        keys = [self._todoist_row_key(task) for task in self._todoist_tasks]
        index = (
            keys.index(anchor_key) if anchor_key in keys else min(anchor_row, len(keys))
        )
        now = datetime.now()
        task = sheets.new_task(
            str(uuid.uuid4()), content, due_string, description, now=now
        )

        orders: dict[str, int] | None = None
        shown_here = self._task_rows is not None and sheets.is_due_on(
            task.due_date, self._todoist_selected_date
        )
        if shown_here:
            task_ids = [t.id for t in self._todoist_tasks]
            task_ids.insert(index, task.id)
            orders = {task_id: rank for rank, task_id in enumerate(task_ids)}

        def on_saved(saved: bool) -> None:
            if not saved:
                self.notify(
                    "Task save not confirmed; refresh before retrying", severity="error"
                )
            elif shown_here:
                self.notify("Task created!")
            elif task.due_date:
                due = date.fromisoformat(task.due_date)
                self.notify(f"Task created for {due.strftime('%a %b %d')}")
            else:
                self.notify("Task created")

        edit = sheets.create_edit(
            task.id, content, due_string, description, day_orders=orders, now=now
        )
        _ = self._queue_task_edit(edit, on_saved)
        self._render_todoist_table(
            select_key=self._todoist_row_key(task) if shown_here else None
        )

    def action_edit_todoist_task(self) -> None:
        """Show modal to edit the selected Todoist task."""
        focused = self.focused
        if not isinstance(focused, VimDataTable) or focused.id != "todoist-table":
            self.notify("Select a Todoist task first", severity="warning")
            return

        if focused.row_count == 0:
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
            self._edit_todoist_task(task_id)

    def _edit_todoist_task(self, task_id: str) -> None:
        """Open the edit modal for a task.

        Uses the task data already loaded during the last refresh (content,
        description, due string, project) plus the background-cached project
        list, so the modal opens instantly with no network request. Falls back
        to fetching from the API only when the task isn't in memory.
        """
        task = next((t for t in self._todoist_tasks if t.id == task_id), None)
        if task is None:
            _ = self._prepare_edit_todoist_task(task_id)
            return
        self._open_edit_todoist_modal(
            task_id=task.id,
            content=task.content,
            description=task.description,
            project_id=task.project_id,
            due_string=task.due_string or "",
        )

    def _open_edit_todoist_modal(
        self,
        task_id: str,
        content: str,
        description: str,
        project_id: str | None,
        due_string: str,
    ) -> None:
        project_options = [(p.name, p.id) for p in self._todoist_projects]
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

    @work(exclusive=False)
    async def _prepare_edit_todoist_task(self, task_id: str) -> None:
        """Fallback: fetch task data from the API, then show the edit modal."""
        task_data = await self._task_request(sheets.get_task, task_id)
        if not task_data:
            self.notify("Failed to load task", severity="error")
            return

        if not self._todoist_projects:
            projects = await self._task_request(sheets.get_projects)
            if projects is not None:
                self._todoist_projects = projects

        content = cast(str, task_data.get("content", ""))
        description = cast(str, task_data.get("description", ""))
        project_id = cast(str | None, task_data.get("project_id"))
        due_raw = task_data.get("due")
        due_string = (
            cast(str, cast(dict[str, object], due_raw).get("string", ""))
            if due_raw
            else ""
        )
        self._open_edit_todoist_modal(
            task_id=task_id,
            content=content,
            description=description,
            project_id=project_id,
            due_string=due_string,
        )

    def _handle_todoist_task_edited(self, result: dict[str, str] | None) -> None:
        """Show the edited task at once and save it in the background."""
        if not result or len(result) <= 1:
            return
        edit = sheets.update_edit(
            result["task_id"],
            content=result.get("content"),
            description=result.get("description"),
            project_id=result.get("project_id"),
            due_string=result.get("due_string"),
        )
        self._edit_tasks(edit, "Edit task", "Task updated!", "Failed to update task")

    def action_open_todoist_in_browser(self) -> None:
        """Open the selected Todoist task in the web app."""
        focused = self.focused
        if not isinstance(focused, VimDataTable) or focused.id != "todoist-table":
            self.notify("Select a Todoist task first", severity="warning")
            return

        if focused.row_count == 0:
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
        _ = self._prepare_linear_issue_modal()

    @work(exclusive=False)
    async def _prepare_linear_issue_modal(self) -> None:
        """Load team data and show the Linear issue creation modal."""
        # These three fetches are independent, so run them concurrently.
        team_id, team_members, viewer_id = await asyncio.gather(
            asyncio.to_thread(linear.get_team_id),
            asyncio.to_thread(linear.get_team_members),
            asyncio.to_thread(linear.get_viewer_id),
        )
        if not team_id:
            self.notify("Failed to get team ID", severity="error")
            return

        # Store team_id for later use
        self._linear_team_id = team_id

        # Show modal
        _ = self.push_screen(
            CreateLinearIssueModal(team_members, viewer_id=viewer_id),
            self._handle_linear_issue_created,
        )

    def _handle_linear_issue_created(self, result: dict[str, str] | None) -> None:
        """Handle the result from the Linear issue creation modal."""
        if result:
            title = result["title"]
            state = result["state"]
            assignee_id = result.get("assignee_id")
            _ = self._do_create_linear_issue(title, state, assignee_id)

    @work(exclusive=False)
    async def _do_create_linear_issue(
        self, title: str, state: str, assignee_id: str | None
    ) -> None:
        team_id = self._linear_team_id
        if not team_id:
            self.notify("Team ID not available", severity="error")
            return

        success = await asyncio.to_thread(
            linear.create_issue, title, team_id, state, assignee_id
        )
        if success:
            self.notify("Issue created!")
            _ = self._refresh_linear()
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
        if not isinstance(focused, VimDataTable):
            return

        if focused.id != "linear-table":
            self.notify("Can only move Linear issues", severity="warning")
            return

        if focused.row_count == 0:
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
        row_region = focused._get_row_region(target_row)  # pyright: ignore[reportPrivateUsage]
        _ = focused.scroll_to_region(row_region, center=True, animate=False)

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

        if self._linear_pending_move is None:
            return

        issue_id, original_sort_order, target_row = self._linear_pending_move
        self._linear_pending_move = None

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
            self._last_action_undoable = True
        else:
            self.notify("Failed to save issue order", severity="error")
            _ = self._refresh_linear()


def _prefetch_tasks() -> None:
    """Resolve AWS credentials, connect, and read the task list in the background.

    This takes about a second from a cold start and needs nothing from
    Bitwarden, so it overlaps the vault prompt and app startup. The app's own
    first read then reuses the open connection and gets a cheap "not modified".
    Errors are left for that read to report.
    """
    try:
        _ = sheets.load()
    except Exception:  # noqa: BLE001, S110 - the app's own read reports errors
        pass


def main():
    threading.Thread(target=_prefetch_tasks, daemon=True).start()
    # Fetch secrets before Textual takes over the terminal, so Bitwarden can
    # prompt for the master password if the vault is locked.
    credentials.load_into_env()
    app = StatusDashboard()
    app.run()


if __name__ == "__main__":
    main()
