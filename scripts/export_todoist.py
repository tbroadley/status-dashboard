"""One-off export of all Todoist data to JSON, for archiving before account deletion."""

import json
import os
import pathlib
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

SYNC_URL = "https://api.todoist.com/api/v1/sync"
COMPLETED_URL = "https://api.todoist.com/api/v1/tasks/completed"


def _token() -> str:
    token = os.environ.get("TODOIST_API_TOKEN") or os.environ.get("TODOIST_TOKEN")
    if not token:
        raise SystemExit("Set TODOIST_API_TOKEN or TODOIST_TOKEN")
    return token


def _post(url: str, data: dict[str, str]) -> Any:
    req = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(data).encode(),
        headers={"Authorization": f"Bearer {_token()}"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


def _get(url: str, params: dict[str, str]) -> Any:
    req = urllib.request.Request(
        f"{url}?{urllib.parse.urlencode(params)}",
        headers={"Authorization": f"Bearer {_token()}"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


def main() -> None:
    out_dir = pathlib.Path.home() / "Desktop" / "todoist-export"
    _ = out_dir.mkdir(parents=True, exist_ok=True)

    full = _post(SYNC_URL, {"sync_token": "*", "resource_types": json.dumps(["all"])})
    _ = (out_dir / "sync-all.json").write_text(
        json.dumps(full, indent=2, sort_keys=True)
    )

    summary = {k: len(v) for k, v in full.items() if isinstance(v, list)}
    print("active data:")
    for key, count in sorted(summary.items(), key=lambda kv: -kv[1]):
        if count:
            print(f"  {key:24} {count}")

    completed: list[Any] = []
    cursor: str | None = None
    while True:
        params = {"limit": "200"}
        if cursor:
            params["cursor"] = cursor
        try:
            page = _get(COMPLETED_URL, params)
        except urllib.error.HTTPError as exc:
            print(f"  (completed-task history unavailable: HTTP {exc.code})")
            break
        completed.extend(page.get("items", page.get("results", [])))
        cursor = page.get("next_cursor")
        if not cursor:
            break
    if completed:
        _ = (out_dir / "completed-tasks.json").write_text(
            json.dumps(completed, indent=2, sort_keys=True)
        )
        print(f"  {'completed_tasks':24} {len(completed)}")

    print(f"\nwrote {out_dir}")


if __name__ == "__main__":
    main()
