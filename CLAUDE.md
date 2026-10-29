# Status Dashboard

Terminal UI (TUI) dashboard aggregating GitHub PRs, S3-backed tasks, and Linear issues into a unified view.

## Architecture

**Technology Stack:**
- Python 3.11+ with Textual (TUI framework)
- httpx for async HTTP (Linear API)
- boto3 (one long-lived client) for the shared S3 task document
- GitHub CLI (`gh`) for GitHub API via subprocess

**Key Design Patterns:**
- Multi-panel layout with independent DataTable widgets
- Vim-style navigation (j/k/g/G, numeric prefixes like 5j)
- Async operations via `@work(exclusive=False)` to prevent UI blocking
- Optimistic UI updates with undo stack (15 actions max)
- Task edits are row functions applied to the screen at once and saved in the background

## Project Structure

```
src/status_dashboard/
├── app.py              # Main app, UI layout, keybindings, action handlers
├── clients/
│   ├── github.py       # GitHub API via `gh` CLI subprocess (GraphQL)
│   ├── tasks.py        # Task semantics over the shared S3 store
│   └── linear.py       # Linear GraphQL API
├── task_store.py       # Validated documents, conditional writes, recovery snapshots
├── task_store_cli.py   # Explicit create-only import, export, restore, and access check
├── credentials.py      # Fetches secrets from Bitwarden at startup
├── dates.py            # Due-string + recurrence parsing (was server-side in Todoist)
├── undo.py             # Undo action dataclasses and stack
└── widgets/
    └── create_modals.py  # Task/issue creation dialogs
```

## Configuration

Environment variables (`.env` or `$XDG_CONFIG_HOME/status-dashboard/.env`):
- `TASKS_S3_URI` - Required object URI; no default location (standard AWS credential chain owns auth)
- `TASKS_AWS_REGION` - Optional region override
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

GitHub and Linear return empty/false values on failure. Task operations raise a
safe `StoreError`, caught by `StatusDashboard._task_request`: show/log the error,
retain the last good view on failed reads, and roll back failed optimistic edits.
Raw AWS errors and object contents must not reach logs. Timeouts: GitHub 30s,
S3 10s connect + 20s read with no automatic retries, Linear 10s.

**GitHub** (`clients/github.py`):
- Uses `gh api graphql` subprocess for queries
- Functions: `get_my_prs()`, `get_review_requests()`, `squash_merge_pr()`, `approve_gated_checks()`, `remove_self_as_reviewer()`, `unsubscribe_from_pr()`
- Gated checks: check runs in `WAITING` on an environment approval the viewer can give.
  `get_my_prs()` fetches check contexts only for pending PRs, in a second batched query
  (fetching them in the main search times out). A pending PR whose only unfinished checks
  are gated gets `ci_status == AWAITING_APPROVAL`, shown as `pass*`.

**Tasks** (`clients/tasks.py`, `task_store.py`):
- `main()` starts a background read (`_prefetch_tasks`) before the Bitwarden step,
  so credential resolution and the TLS handshake overlap startup.
- No Google API calls. The standard AWS credential chain (env, profile, SSO) owns
  authentication; botocore is imported lazily, off the UI thread. A sign-in error
  drops the client so the next request picks up fresh credentials.
- Shared format: `{"version":1,"rows":[...]}` with nine string cells per row:
  `id, content, project, description, due, recurrence, order, done, completed_at`.
- `order` is one ranking across all rows, not a position within a day. Reordering a
  day permutes the listed tasks among the ranks they already hold (`_arrange`), so
  tasks not shown that day, e.g. completed recurring tasks, keep their relative order.
- Validate on every read; missing/corrupt documents fail closed. Never auto-initialize
  or substitute an empty list. `task-store` provides explicit create-only initialization/import.
- `task_store.shared_store()` is the process-wide store: one boto3 client (S3 drops
  idle connections after ~5s; `warm()` reconnects ahead of use, and the app calls it
  as the cursor moves in the task list) and the last read/written `Snapshot`.
  Snapshots are shared; never mutate their rows.
- Each edit starts from the cached version (or reads data+ETag), writes a unique
  pre-edit recovery object under `<key>.history/`, then uses `If-Match`. A stale
  cache fails the `If-Match` and the edit is redone on a fresh read (not counted
  as an attempt); an edit that finds nothing to do on the cache is also re-checked
  against a fresh read. Conflicts re-read and reapply by task ID, up to six
  attempts with jittered exponential backoff; a failed archive aborts the write.
  Reads send `If-None-Match` and reuse the cache on 304. Restore tries once.
- Uncertain write responses are reconciled with a readback; unresolved outcomes
  explicitly warn to refresh before retrying.
- Every mutation is a `tasks.Edit`: a function that changes rows in place and
  returns whether its task(s) exist (`complete_edit`, `defer_edit`,
  `reschedule_to_today_edit`, `arrange_edit`, ...). Edits must depend only on their
  arguments and the rows (capture `now` when building them), because each runs
  twice: on the rows on screen and on the stored rows when saved.
- Task view model: `_task_rows` (every stored row, as of the last read or save)
  plus `_task_pending` (queued/in-flight edits, applied in order on top);
  `_render_todoist_table` derives `_todoist_tasks` for the selected day. Queue a
  change with `_queue_task_edit`, or `_edit_tasks` for an undoable one: it pushes a
  `TaskEditAction` holding `tasks.revert(...)` immediately, and drops it if the
  save fails.
- `_save_task_edits` is the single writer: it sends everything queued as one
  `tasks.save` (one backup + one conditional put), which returns the stored rows,
  then drops the batch from `_task_pending` with no `await` in between. Because a
  save returns fresh rows, a read that overlaps a save (in flight or finished
  meanwhile) is dropped, not retried; an older read never replaces a newer one.
- Day navigation renders from `_task_rows` immediately, then refreshes once
  navigation settles. Due-time notifications use the loaded rows too.
- New tasks get their final ID from the app (`tasks.new_task` previews exactly what
  `create_edit` stores) and are saved together with their position in one write.
  The create/edit modals reject due strings that `dates.parse_due_string` can't parse.
- All location details belong in local config, never source/tests/PR text. Do not
  commit real task data. Tests use synthetic documents and a fake S3 client
  (`tests/test_task_store.FakeS3`). `scripts/bench_tasks.py` measures key-to-screen
  and key-to-saved latency against simulated S3 latency.
- Recurrence still advances locally using `dates.next_occurrence()`; completion
  rolls its due date forward rather than marking a recurring task done.
- Legacy UI names (`_todoist_*`, `todoist:` row keys, and the `sheets` import alias)
  are cosmetic; task persistence is exclusively S3. See README for setup/recovery.

**Linear** (`clients/linear.py`):
- GraphQL API via one shared `httpx.Client` (`_http()`), so connections are reused
- Answers that don't change during a session are cached in `_cache` once they
  succeed: workflow state IDs per team, the viewer ID, the project's team and ID
- Functions: `get_project_issues()`, `set_issue_state()`, `create_issue()`, `assign_issue()`, `update_sort_order()`
- State mapping in `STATE_NAME_MAP`

## UI Conventions

- Row keys encode metadata: `todoist:{id}:{url}`, `linear:{id}:{team_id}:{url}`, etc.
- Cursor position preserved across refreshes via key matching; if the selected task
  disappears, the cursor keeps its row index (the task below slides under it)
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
