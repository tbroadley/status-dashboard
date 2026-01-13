# Status Dashboard

A terminal dashboard for tracking PRs, Todoist tasks, and Linear issues.

## Setup

1. Copy `.env.example` to `.env`:
   ```bash
   cp .env.example .env
   ```

2. Configure the required environment variables in `.env`:

   | Variable | Required | Description |
   |----------|----------|-------------|
   | `TODOIST_API_TOKEN` | Yes | Your Todoist API token |
   | `LINEAR_API_KEY` | Yes | Your Linear API key |
   | `LINEAR_PROJECT` | Yes | Name of the Linear project to show issues from |
   | `GITHUB_ORG` | No | GitHub organization (defaults to `METR`) |
   | `HIDDEN_REVIEW_REQUESTS` | No | JSON array of `[repo, pr_number]` pairs to hide from review requests |

3. Install dependencies and run:
   ```bash
   uv sync
   uv run status-dashboard
   ```

## Keybindings

| Key | Action |
|-----|--------|
| `Tab` | Move to next panel |
| `Shift+Tab` | Move to previous panel |
| `1-4` | Focus panel (My PRs, Reviews, Todoist, Linear) |
| `↑/↓` | Navigate items in panel |
| `Enter` | Open selected item in browser |
| `r` | Refresh all panels |
| `R` | Restart app |
| `c` | Complete selected task/issue |
| `q` | Quit |

### Todoist
| Key | Action |
|-----|--------|
| `a` | Add new task |
| `n` | Defer task to next working day |

### Linear
| Key | Action |
|-----|--------|
| `i` | Create new issue |
| `b` | Move to Backlog |
| `t` | Move to Todo |
| `p` | Move to In Progress |
| `v` | Move to In Review |
| `d` | Move to Done |

### Review Requests
| Key | Action |
|-----|--------|
| `x` | Remove yourself as reviewer |
