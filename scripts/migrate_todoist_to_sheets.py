"""Create the tasks spreadsheet and import a Todoist export into it.

Usage:
    uv run python scripts/migrate_todoist_to_sheets.py \
        --export ~/Desktop/todoist-export/sync-all.json --dry-run

    uv run python scripts/migrate_todoist_to_sheets.py \
        --export ~/Desktop/todoist-export/sync-all.json

Prints the new spreadsheet ID; put it in `.env` as TASKS_SPREADSHEET_ID.
Pass --spreadsheet-id to import into an existing sheet instead of creating one
(rows whose content and due date already exist are skipped).

Auth comes from the `gws` CLI, same as the runtime client.
"""

import argparse
import datetime as dt
import json
import pathlib
import subprocess
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from status_dashboard import dates  # noqa: E402
from status_dashboard.clients import sheets  # noqa: E402


def gws(args: list[str], payload: dict[str, Any] | None = None) -> dict[str, Any]:
    command = ["gws", *args]
    if payload is not None:
        command += ["--json", json.dumps(payload)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise SystemExit(f"gws failed: {result.stderr.strip()[:500]}")
    data = json.loads(result.stdout) if result.stdout.strip() else {}
    if "error" in data:
        raise SystemExit(f"gws API error: {data['error']}")
    return data


def create_spreadsheet(title: str) -> str:
    created = gws(
        ["sheets", "spreadsheets", "create"],
        {
            "properties": {"title": title},
            "sheets": [{"properties": {"title": sheets.SHEET_NAME}}],
        },
    )
    spreadsheet_id = created["spreadsheetId"]
    _ = gws(
        [
            "sheets",
            "spreadsheets",
            "values",
            "update",
            "--params",
            json.dumps(
                {
                    "spreadsheetId": spreadsheet_id,
                    "range": f"{sheets.SHEET_NAME}!A1:I1",
                    "valueInputOption": "RAW",
                }
            ),
        ],
        {"values": [sheets.HEADER]},
    )
    print(f"created spreadsheet {spreadsheet_id}")
    print(f"  {created['spreadsheetUrl']}")
    return spreadsheet_id


def existing_keys(spreadsheet_id: str) -> set[tuple[str, str]]:
    data = gws(
        [
            "sheets",
            "spreadsheets",
            "values",
            "get",
            "--params",
            json.dumps({"spreadsheetId": spreadsheet_id, "range": sheets.DATA_RANGE}),
        ]
    )
    rows: list[list[str]] = data.get("values", [])
    return {
        (
            row[sheets.COL_CONTENT] if len(row) > sheets.COL_CONTENT else "",
            row[sheets.COL_DUE][:10] if len(row) > sheets.COL_DUE else "",
        )
        for row in rows
        if row
    }


def due_and_recurrence(item: dict[str, Any]) -> tuple[str, str]:
    """Translate a Todoist due block into an ISO string plus recurrence text."""
    due = item.get("due")
    if not due:
        return "", ""

    raw = due.get("date", "")
    recurrence = due.get("string", "") if due.get("is_recurring") else ""

    if "T" in raw:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        local = parsed.astimezone() if parsed.tzinfo else parsed
        return local.isoformat(timespec="seconds"), recurrence
    return raw[:10], recurrence


def build_row(item: dict[str, Any], project: str, task_id: str) -> list[str]:
    due, recurrence = due_and_recurrence(item)
    return [
        task_id,
        item["content"],
        project,
        item.get("description", "") or "",
        due,
        recurrence,
        str(item.get("day_order") or 0),
        "TRUE" if item.get("checked") else "FALSE",
        "",
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--export", required=True, type=pathlib.Path)
    _ = parser.add_argument("--spreadsheet-id", help="import into an existing sheet")
    _ = parser.add_argument("--title", default="Status Dashboard Tasks")
    _ = parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    data = json.loads(pathlib.Path(args.export).expanduser().read_text())
    projects = {p["id"]: p["name"] for p in data["projects"]}
    items = [i for i in data["items"] if not i.get("is_deleted")]

    print(f"{len(items)} active tasks across {len(projects)} projects")

    unparseable = sorted(
        {
            i["due"]["string"]
            for i in items
            if (i.get("due") or {}).get("is_recurring")
            and dates.next_occurrence(i["due"]["string"], dt.datetime.now()) is None
        }
    )
    if unparseable:
        print("\nWARNING — recurrence rules the client cannot advance:")
        for rule in unparseable:
            print(f"  {rule!r}")
        print(
            "  These import with a due date but will complete instead of repeating.\n"
        )

    if args.dry_run:
        for item in items[:10]:
            due, recurrence = due_and_recurrence(item)
            suffix = f"  [{recurrence}]" if recurrence else ""
            project = projects.get(item.get("project_id"), "?")
            print(
                f"  {project:26} {item['content'][:48]:50} {due or '(no date)'}{suffix}"
            )
        print(f"  ... ({len(items)} total)")
        return

    spreadsheet_id = args.spreadsheet_id or create_spreadsheet(args.title)
    skip = existing_keys(spreadsheet_id) if args.spreadsheet_id else set()
    if skip:
        print(f"importing into existing sheet ({len(skip)} rows already present)")

    rows: list[list[str]] = []
    skipped = 0
    for item in items:
        due, _ = due_and_recurrence(item)
        if (item["content"], due[:10]) in skip:
            skipped += 1
            continue
        # Reuse the Todoist ID so the export stays traceable to the new rows.
        rows.append(
            build_row(item, projects.get(item.get("project_id"), ""), str(item["id"]))
        )

    if rows:
        _ = gws(
            [
                "sheets",
                "spreadsheets",
                "values",
                "append",
                "--params",
                json.dumps(
                    {
                        "spreadsheetId": spreadsheet_id,
                        "range": f"{sheets.SHEET_NAME}!A:I",
                        "valueInputOption": "RAW",
                        "insertDataOption": "INSERT_ROWS",
                    }
                ),
            ],
            {"values": rows},
        )

    print(f"\nimported {len(rows)}, skipped {skipped}")
    print(f"\nAdd to .env:\n  TASKS_SPREADSHEET_ID={spreadsheet_id}")


if __name__ == "__main__":
    main()
