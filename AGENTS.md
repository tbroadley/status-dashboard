# Agent Instructions

## Project Overview

Status Dashboard is a terminal UI application (TUI) built with [Textual](https://textual.textualize.io/) that displays:
- GitHub pull requests (authored and review requests)
- Todoist tasks
- Linear issues

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
│   ├── linear.py             # Linear GraphQL API via httpx
│   └── todoist.py            # Todoist REST API via httpx
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
- `TODOIST_API_TOKEN`
- `LINEAR_API_KEY`
- `LINEAR_PROJECT`

Optional:
- `GITHUB_ORG` (defaults to `METR`)
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

No tests currently exist in this repository.
