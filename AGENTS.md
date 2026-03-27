# Agent Instructions

## Project Overview

Status Dashboard is a terminal UI application (TUI) built with [Textual](https://textual.textualize.io/) that displays:
- GitHub pull requests (authored/assigned PRs and review requests)
- Todoist tasks

## Tech Stack

- Python 3.11+
- Textual (TUI framework)
- httpx (HTTP client for Linear and Todoist APIs)
- GitHub CLI (`gh`) for GitHub API access

## Project Structure

```
src/status_dashboard/
├── app.py                    # Main application, UI layout, keybindings
├── clients/
│   ├── github.py             # GitHub API via `gh` CLI subprocess
│   └── todoist.py            # Todoist REST API via httpx
└── widgets/
    └── create_modals.py      # Modal dialogs for creating tasks
```

## Running the App

```bash
uv sync
uv run status-dashboard
```

## Environment Variables

Required in `.env` file (see `.env.example`):
- `TODOIST_API_TOKEN`
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

All API clients return `None`, empty collections, or `False` on failure rather than raising exceptions. This allows the UI to degrade gracefully. Errors are logged via the standard `logging` module.

## Testing

Run the lightweight unittest coverage with:
```bash
uv run python -m unittest discover
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

## Gotchas

- Textual CSS does not support `max-height: none` or other keyword values for scalar properties. Use a large percentage like `max-height: 100%` instead.
