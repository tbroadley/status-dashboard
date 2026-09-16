# Agent Instructions

## Project Overview

Status Dashboard is a terminal UI application (TUI) built with [Textual](https://textual.textualize.io/) that displays:
- GitHub pull requests (authored/assigned PRs and review requests)
- Tasks in a shared S3 JSON document
- Linear issues

## Tech Stack

- Python 3.11+
- Textual (TUI framework)
- httpx (HTTP client for Linear)
- AWS CLI (task storage; credentials managed outside the app)
- GitHub CLI (`gh`) for GitHub API access

## Project Structure

```
src/status_dashboard/
├── app.py                    # Main application, UI layout, keybindings
├── clients/
│   ├── github.py             # GitHub API via `gh` CLI subprocess
│   ├── linear.py             # Linear GraphQL API via httpx
│   └── tasks.py              # Task semantics over shared S3 persistence
├── task_store.py              # Conditional writes and recovery snapshots
├── task_store_cli.py          # Explicit import, initialization, and recovery
└── widgets/
    └── create_modals.py      # Modal dialogs for creating tasks/issues
```

## Running the App

```bash
uv sync
uv run status-dashboard
```

## Environment Variables

Required in `.env` file (see `.env.example`):
- `TASKS_S3_URI` (local configuration only; never commit the actual location)
- Optional `TASKS_AWS_REGION` and `TASKS_AWS_CLI`
- `LINEAR_API_KEY`
- `LINEAR_PROJECT`

Optional:
- `GITHUB_ORGS` (comma-separated list, e.g., `METR,metr-middleman`)
- `GITHUB_ORG` (single org, deprecated; defaults to `METR`)
- `GITHUB_EXTRA_PR_REPOS` (comma-separated extra repos for My PRs, defaults to `ukgovernmentbeis/inspect_ai,meridianlabs-ai/inspect_scout`)
- `HIDDEN_REVIEW_REQUESTS` (JSON array)

## Logging

Errors are logged to both stderr and a rotating log file at:
```
~/.local/state/status-dashboard/status-dashboard.log
```

The log file rotates at 1MB with 3 backups. View logs with:
```bash
cat ~/.local/state/status-dashboard/status-dashboard.log
tail -f ~/.local/state/status-dashboard/status-dashboard.log  # follow in real-time
```

Log level is WARNING, so only warnings and errors are recorded.

## Error Handling Patterns

GitHub/Linear return empty/false values on failure. Task operations raise `StoreError`;
`StatusDashboard._task_request` displays/logs a safe message and returns `None`.
Failed reads retain the last successful view; failed optimistic mutations roll back.
Never treat inaccessible or malformed S3 data as an empty list. Never log raw AWS
output, object contents, or private locations. See CLAUDE.md and README for the
shared schema, conditional-write protocol, and explicit initialization/recovery.

## Testing

Run the lightweight unittest coverage with:
```bash
uv run python -m unittest discover -s tests
```

### Visual Testing

For UI/CSS changes, take a screenshot before pushing to verify the layout:
```python
import asyncio
from status_dashboard.app import StatusDashboard

async def main():
    app = StatusDashboard()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.save_screenshot("/tmp/status-dashboard.svg")

asyncio.run(main())
```
Then convert to PNG and view the image to confirm the layout looks correct.

Tests must mock every external client, including `linear.get_my_issues`; otherwise
background requests can outlive the TUI test and race screen teardown.

## Gotchas

- Textual CSS does not support `max-height: none` or other keyword values for scalar properties. Use a large percentage like `max-height: 100%` instead.
