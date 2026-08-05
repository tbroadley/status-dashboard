# Status Dashboard

Terminal UI (TUI) dashboard aggregating GitHub PRs, Google Sheets tasks, and Linear issues into a unified view.

## Architecture

**Technology Stack:**
- Python 3.11+ with Textual (TUI framework)
- httpx for async HTTP (Linear API)
- `gws` CLI subprocess for Google Sheets
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
│   ├── sheets.py       # Google Sheets tasks via `gws` CLI; replaced Todoist
│   └── linear.py       # Linear GraphQL API
├── credentials.py      # Fetches secrets from Bitwarden at startup
├── dates.py            # Due-string + recurrence parsing (was server-side in Todoist)
├── undo.py             # Undo action dataclasses and stack
└── widgets/
    └── create_modals.py  # Task/issue creation dialogs
```

## Configuration

Environment variables (`.env` or `$XDG_CONFIG_HOME/status-dashboard/.env`):
- `TASKS_SPREADSHEET_ID` - Required (Google Sheet ID; auth is handled by the `gws` CLI)
- `LINEAR_BW_ITEM` - Name of the Bitwarden item holding the Linear key (fetched at startup)
- `LINEAR_API_KEY` - Optional; set directly to bypass Bitwarden (env var always wins)
- `LINEAR_PROJECT` - Required (project name to display)
- `GITHUB_ORGS` - Optional comma-separated list of GitHub organizations (e.g., `METR,metr-middleman`)
- `GITHUB_ORG` - Optional single organization (deprecated, use `GITHUB_ORGS`; defaults to METR)
- `GITHUB_EXTRA_PR_REPOS` - Optional comma-separated list of extra repos to show authored or assigned PRs from (defaults to `ukgovernmentbeis/inspect_ai,meridianlabs-ai/inspect_scout`)
- `HIDDEN_REVIEW_REQUESTS` - Optional JSON array of [repo, pr_number]
- `TODOIST_DUE_NOTIFICATIONS` - Optional, enables desktop notifications when a task's due time arrives (default on; set to `false`/`0`/`off` to disable). Name retained for backwards compatibility.

App-managed state (not env vars):
- Hidden PRs: `$XDG_CONFIG_HOME/status-dashboard/hidden_prs.json` — JSON array of `[repo, pr_number]`. PRs hidden from the My PRs panel via the `H` keybinding. Delete entries (or the file) to un-hide.

Logs: `$XDG_STATE_HOME/status-dashboard/status-dashboard.log` (rotating, 1MB, 3 backups)

## Development

```bash
uv sync                   # Install dependencies
uv run status-dashboard   # Run the app
```

Run tests (stdlib `unittest`, not pytest):
```bash
uv run python -m unittest discover -s tests
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

## Credentials

`credentials.load_into_env()` runs in `main()` **before** the Textual app starts, so
Bitwarden can prompt for the master password while the terminal is still free.

- Resolution order per secret: existing env var wins, else `bw get password <item>`
- `MANAGED_SECRETS` maps an env var to the env var naming its Bitwarden item
- `bw unlock --raw` writes its prompt to **stderr** and the session key to **stdout**,
  so only stdout is captured — capturing stderr would hide the prompt
- `bw unlock` exits 0 even when aborted, so empty stdout is treated as failure
- Nothing here is fatal: a locked vault or missing item leaves the secret unset and the
  affected panel degrades, same as any other API failure
- Startup costs one `bw` call (~1.8s) when a secret needs fetching; secrets are held in
  memory only, never written back to disk

## API Client Patterns

All clients follow these conventions:
- Return `None`, empty list, or `False` on error (no exceptions raised to callers)
- Errors logged to file and stderr
- Timeouts: GitHub 30s, gws 30s, Linear 10s

**GitHub** (`clients/github.py`):
- Uses `gh api graphql` subprocess for queries
- Functions: `get_my_prs()`, `get_review_requests()`, `squash_merge_pr()`, `remove_self_as_reviewer()`

**Sheets** (`clients/sheets.py`):
- Shells out to the `gws` CLI (same pattern as `github.py` with `gh`); `gws` owns auth
- One `Tasks` sheet: `A id | B content | C project | D description | E due | F recurrence | G order | H done | I completed_at`
- Sheets addresses cells by position, so every mutation re-reads to resolve an ID to a row.
  **Never cache row numbers** — a delete shifts every row beneath it
- `deleteDimension` needs the numeric `sheetId` (not 0); `_get_sheet_id()` resolves and caches it
- Functions: `get_today_tasks()`, `complete_task()`, `defer_task()`, `create_task()`, `update_day_orders()`
- Recurrence is stored as rule text and advanced locally by `dates.next_occurrence()`;
  completing a recurring task rolls its due date forward instead of ticking Done
- Internal names (`_todoist_*` attributes, `todoist:` row keys, `todoist_*` actions) still
  say "todoist" — cosmetic only, not yet renamed

**Linear** (`clients/linear.py`):
- GraphQL API via httpx
- Functions: `get_project_issues()`, `set_issue_state()`, `create_issue()`, `assign_issue()`, `update_sort_order()`
- State mapping in `STATE_NAME_MAP`

## UI Conventions

- Row keys encode metadata: `todoist:{id}:{url}`, `linear:{id}:{team_id}:{url}`, etc.
- Cursor position preserved across refreshes via key matching
- Relative line numbers (vim-style) updated on cursor movement
- Toast notifications for user feedback on actions
- **Optimistic updates required**: Any feature that mutates state on a remote server (API call) must include an optimistic UI update — immediately reflect the change in the UI before the API response, then roll back on failure. This applies to all panels (Todoist, GitHub, Linear). Use the existing undo stack (`undo.py`) to support reversal. See existing patterns: task completion, PR merge, issue state changes, reordering.

## Textual Awaitable Methods

Many Textual widget methods return custom awaitable objects (`AwaitMount`, `AwaitRemove`, `AwaitComplete`) that can optionally be awaited:

- **If you don't await**: Textual will auto-await before the next message is processed
- **If you await**: The operation completes before the next line executes

**Default to awaiting.** Always await `clear()`, `remove()`, `mount()`, etc. when the same function does further work on that widget. Using `_ = widget.clear()` to silence the lint is dangerous - it's easy to miss that subsequent code depends on the operation completing.

```python
# WRONG - clear() hasn't completed yet when append() runs
def refresh_list(self):
    _ = goals_list.clear()  # Don't do this!
    goals_list.append(...)  # May append to un-cleared list!

# CORRECT - await ensures clear completes first
async def refresh_list(self):
    await goals_list.clear()
    goals_list.append(...)  # List is now empty
```

Only use `_ = ...` for true fire-and-forget cases where no subsequent code in the function touches that widget.

## Reusable Modal Screens

Modals installed via `install_screen()` are only composed/mounted when first pushed. Do NOT call `query_one()` on their widgets before `push_screen()` — it will throw `NoMatches`. Instead, use `on_screen_resume` to reset fields each time the screen is shown:

```python
# WRONG - widgets aren't mounted yet
self._modal.reset()  # calls query_one() internally → NoMatches
_ = self.push_screen("my-modal", callback)

# CORRECT - reset happens in lifecycle hook when widgets exist
class MyModal(ModalScreen):
    def on_screen_resume(self) -> None:
        self.query_one("#input", Input).value = ""
```

## Screenshot Test Harness

Headless TUI testing via `tests/screenshot.py`. Uses Textual's `run_test()` with mocked API clients and fake data (`tests/fake_data.py`). Output goes to `tests/screenshots/` (gitignored).

**One-off screenshots:**
```bash
uv run python tests/screenshot.py                              # Default view
uv run python tests/screenshot.py --keys "j j Tab" --size 120x55
uv run python tests/screenshot.py --scenario navigation        # Predefined scenario
```

**Stateful sessions** (replay-based — each run replays all prior keys then applies new ones):
```bash
uv run python tests/screenshot.py --session dev --size 120x55  # Start session
uv run python tests/screenshot.py --session dev --keys "j j j" # Send keys, appended to history
uv run python tests/screenshot.py --session dev --keys "Tab c"  # Send more keys
uv run python tests/screenshot.py --session dev                 # Re-render current state
uv run python tests/screenshot.py --session dev --reset          # Reset to initial state
```

Session state is stored in `tests/screenshots/sessions/<name>/`:
- `keys.txt` — newline-delimited key history (replayed each run)
- `screenshot.txt` — latest rendered output

Use `--size 120x55` to see all 6 panels (default 120x40 cuts off bottom panels).

**Panel navigation in tests:** Use `ctrl+shift+down` / `ctrl+shift+up` to switch panels (NOT `Tab` — that's Textual's default focus-next which doesn't follow the custom panel order).

## Error Handling

Graceful degradation: API failures result in stale data rather than crashes. The UI continues functioning and errors are logged.
