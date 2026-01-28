# Status Dashboard

Terminal UI (TUI) dashboard aggregating GitHub PRs, Todoist tasks, and Linear issues into a unified view.

## Architecture

**Technology Stack:**
- Python 3.11+ with Textual (TUI framework)
- httpx for async HTTP (Linear, Todoist APIs)
- GitHub CLI (`gh`) for GitHub API via subprocess

**Key Design Patterns:**
- Multi-panel layout with independent DataTable widgets
- Vim-style navigation (j/k/g/G, numeric prefixes like 5j)
- Async operations via `@work(exclusive=False)` to prevent UI blocking
- Optimistic UI updates with undo stack (15 actions max)
- Debounced API sync (0.5s) for reordering operations

## Project Structure

```
src/status_dashboard/
├── app.py              # Main app, UI layout, keybindings, action handlers
├── clients/
│   ├── github.py       # GitHub API via `gh` CLI subprocess (GraphQL)
│   ├── todoist.py      # Todoist REST + Sync API
│   └── linear.py       # Linear GraphQL API
├── undo.py             # Undo action dataclasses and stack
└── widgets/
    └── create_modals.py  # Task/issue creation dialogs
```

## Configuration

Environment variables (`.env` or `$XDG_CONFIG_HOME/status-dashboard/.env`):
- `TODOIST_API_TOKEN` - Required
- `LINEAR_API_KEY` - Required
- `LINEAR_PROJECT` - Required (project name to display)
- `GITHUB_ORGS` - Optional comma-separated list of GitHub organizations (e.g., `METR,metr-middleman`)
- `GITHUB_ORG` - Optional single organization (deprecated, use `GITHUB_ORGS`; defaults to METR)
- `HIDDEN_REVIEW_REQUESTS` - Optional JSON array of [repo, pr_number]

Logs: `$XDG_STATE_HOME/status-dashboard/status-dashboard.log` (rotating, 1MB, 3 backups)

## Development

```bash
uv sync                   # Install dependencies
uv run status-dashboard   # Run the app
```

Before committing:
```bash
uv run ruff check --fix .
uv run ruff format .
uv run basedpyright
```

## Key Keybindings

| Key | Action |
|-----|--------|
| j/k | Navigate down/up |
| g/G | Jump to top/bottom |
| Tab/Shift+Tab | Switch panels |
| r | Refresh all data |
| z | Undo last action |
| q | Quit |

Panel-specific bindings are shown in the footer.

## API Client Patterns

All clients follow these conventions:
- Return `None`, empty list, or `False` on error (no exceptions raised to callers)
- Errors logged to file and stderr
- Timeouts: GitHub 30s, Todoist 10-15s, Linear 10s

**GitHub** (`clients/github.py`):
- Uses `gh api graphql` subprocess for queries
- Functions: `get_my_prs()`, `get_review_requests()`, `squash_merge_pr()`, `remove_self_as_reviewer()`

**Todoist** (`clients/todoist.py`):
- REST API v2 for most operations, Sync API v9 for day_order
- Functions: `get_today_tasks()`, `complete_task()`, `defer_task()`, `create_task()`, `update_day_orders()`

**Linear** (`clients/linear.py`):
- GraphQL API via httpx
- Functions: `get_project_issues()`, `set_issue_state()`, `create_issue()`, `assign_issue()`, `update_sort_order()`
- State mapping in `STATE_NAME_MAP`

## UI Conventions

- Row keys encode metadata: `todoist:{id}:{url}`, `linear:{id}:{team_id}:{url}`, etc.
- Cursor position preserved across refreshes via key matching
- Relative line numbers (vim-style) updated on cursor movement
- Toast notifications for user feedback on actions

## Error Handling

Graceful degradation: API failures result in stale data rather than crashes. The UI continues functioning and errors are logged.
