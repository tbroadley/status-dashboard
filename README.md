# Status Dashboard

A terminal dashboard for tracking PRs, tasks stored in S3, and Linear issues.

## Setup

1. Copy `.env.example` to `.env`:
   ```bash
   cp .env.example .env
   ```

2. Configure the required environment variables in `.env`:

   | Variable | Required | Description |
   |----------|----------|-------------|
   | `TASKS_S3_URI` | Yes | S3 object URI for the shared task document (no built-in location) |
   | `TASKS_AWS_REGION` | No | AWS region override |
   | `LINEAR_BW_ITEM` | Yes | Bitwarden item holding the Linear API key |
   | `LINEAR_API_KEY` | No | Set directly to bypass Bitwarden |
   | `LINEAR_PROJECT` | Yes | Name of the Linear project to show issues from |
   | `GITHUB_ORGS` | No | Comma-separated list of GitHub organizations (e.g., `METR,metr-middleman`) |
   | `GITHUB_ORG` | No | Single GitHub organization (deprecated, use `GITHUB_ORGS` instead) |
   | `GITHUB_EXTRA_PR_REPOS` | No | Comma-separated list of extra repos to show authored or assigned PRs from (e.g., `owner/repo1,owner/repo2`) |
   | `HIDDEN_REVIEW_REQUESTS` | No | JSON array of `[repo, pr_number]` pairs to hide from review requests |
   | `AWS_AUTO_SSO_LOGIN` | No | Run `aws sso login` automatically when AWS credentials expire (default on; `false` to disable) |

3. Make AWS credentials available through the standard chain (for SSO: `aws sso login`), then install and run.
   When an SSO session expires, the dashboard runs `aws sso login` itself and posts a
   silent desktop notification; approve the sign-in in the browser and the request is retried:
   ```bash
   uv sync
   uv run status-dashboard
   ```

## Task storage

The task list is one S3 JSON document shared with Experience Sampling. There is
no Google API dependency. Configure `TASKS_S3_URI` in
`~/.config/status-dashboard/.env` (or under `XDG_CONFIG_HOME`); environment
variables take precedence. Keep bucket names, keys, account identifiers, and
credentials out of this repository. AWS credentials stay in the standard AWS
credential chain, not in the task document.

Use a private, encrypted bucket/prefix with appropriate IAM permissions. The
client needs `s3:GetObject` and `s3:PutObject` for the document and its
`<key>.history/` prefix, plus any KMS permissions required by the bucket. It does
not configure IAM or assert that a given location is private. Bucket versioning
is recommended; recovery snapshots work without version-read permissions.

Initialize explicitly, choosing **one**:

```bash
uv run task-store import-csv /path/to/tasks.csv  # an approved, user-provided export
uv run task-store init-empty                    # only for a new, empty list
uv run task-store check
```

Both initialization commands are create-only: an existing object cannot be
overwritten. CSV headers are `id,content,project,description,due,recurrence,order,done,completed_at`.
No migration tool fetches Google content. The JSON wire format is
`{"version":1,"rows":[...]}`, with nine string cells per row in that same order.
IDs must be nonempty and unique. Due dates are ISO dates or datetimes; recurrence
rules, ordering and completion timestamps retain their existing meanings.

Every edit applies to the latest known object and ETag, saves the previous
document to a uniquely named history object, then writes with `If-Match`.
The client keeps its last read or written version, so an edit usually needs no
read first, and refreshes send `If-None-Match` to skip unchanged downloads. If
that version is stale, the conditional write fails and the edit is redone on a
fresh read. Conflicts re-read and reapply the edit, up to six attempts with
jittered backoff. A failed backup prevents the write.
Missing, malformed, or inaccessible data is an error, never an empty list.
Reads/writes require connectivity and valid AWS credentials; offline edits are
not queued. Lost write responses trigger a readback; if the result cannot be
confirmed, the error explicitly warns that the save may have succeeded and to
refresh before retrying. The dashboard loads the whole list, so moving between
days needs no request; on read failure it keeps its last successful view.
Edits show immediately and save in the background; failed ones roll back. The
dashboard makes its writes one at a time, so older saves cannot overtake newer
ones, and edits made while a save is running go out together in the next one.
A refresh cannot undo a change that is still saving.

The dashboard keeps a private copy of the list (mode 0600) at
`$XDG_CACHE_HOME/status-dashboard/tasks.json` (default `~/.cache/...`) so it can
show tasks at startup before S3 answers; the panel title says "saved copy" until
then. The copy is never trusted over S3: reads still check it with `If-None-Match`,
and a write based on an out-of-date copy fails `If-Match` and is redone on a
fresh read. Delete the file to remove the local copy.

To recover after disk failure, reinstall and reconfigure the same URI. To undo
an unwanted edit, download a JSON snapshot from `<key>.history/` using your AWS
tools, then run `uv run task-store restore /path/to/snapshot.json --confirm`.
Restores archive the current document and abort on a concurrent edit. Use
`uv run task-store export /path/to/new-file.json` for a private local export.
History objects are retained until explicitly removed or expired by bucket
lifecycle policy; they are not immutable against an authorized deleter.

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

### Tasks
| Key | Action |
|-----|--------|
| `a` | Add new task |
| `d` | Delete task |
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

### My PRs
| Key | Action |
|-----|--------|
| `m` | Squash merge (approved PRs only) |
| `x` | Close |
| `H` | Hide |
| `a` | Run gated checks, after a confirmation listing them |

`pass*` in the CI column means every check has passed except ones waiting on a
deployment environment approval that you can give. On a PR you authored, `a`
approves those environments so the checks run.

### Review Requests
| Key | Action |
|-----|--------|
| `x` | Remove yourself as reviewer (also marks the PR's notifications read) |

### Notifications

Each PR appears in at most one table. A notification is hidden while its PR is
shown in My PRs or Review Requests (PRs hidden there still show notifications).
It is also hidden once settled: your latest review or comment is newer than any
human activity on the PR, or it is a review request on a merged or closed PR.
Review-request notifications that only reached you through a team (you are not
a requested reviewer yourself) are hidden unless you have commented on or
reviewed the PR.

The Review Requests ✓ means another human has reviewed the PR; reviews by bots
and by you don't count.
