"""Measure task-pane latency as the user sees it, against simulated S3 latency.

Runs the real app headless on synthetic data. Each S3 request sleeps for
`--latency` seconds (default: a warm request measured from a laptop). Reports,
per action, how long until the table shows the result ("visible") and until
S3 holds it ("saved").

    uv run python scripts/bench_tasks.py [--latency 0.14] [--repeat 3]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import statistics
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

_ = os.environ.setdefault("TASKS_S3_URI", "s3://example-bucket/tasks.json")
_ = os.environ.setdefault("TASKS_AWS_CLI", "aws")
_ = sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_ = sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from status_dashboard import task_store  # noqa: E402
from status_dashboard.app import StatusDashboard, TodoistDataTable  # noqa: E402

KEY = "tasks.json"


class SlowS3:
    """An object store where every request takes `latency` seconds."""

    def __init__(self, data: bytes, latency: float) -> None:
        self.objects: dict[str, bytes] = {KEY: data}
        self.latency: float = latency
        self.lock: threading.Lock = threading.Lock()
        self.requests: int = 0

    @staticmethod
    def etag(data: bytes) -> str:
        return '"' + hashlib.md5(data).hexdigest() + '"'

    def wait(self) -> None:
        with self.lock:
            self.requests += 1
        time.sleep(self.latency)

    def rows(self) -> list[list[str]]:
        return json.loads(self.objects[KEY])["rows"]

    # AWS CLI subprocess transport (used by older versions).
    def run(
        self, command: list[str], **_: object
    ) -> subprocess.CompletedProcess[bytes]:
        self.wait()
        key = command[command.index("--key") + 1]
        if command[2] == "get-object":
            data = self.objects[key]
            Path(command[command.index("--key") + 2]).write_bytes(data)
            out = json.dumps({"ETag": self.etag(data)}).encode()
            return subprocess.CompletedProcess(command, 0, out, b"")
        body = Path(command[command.index("--body") + 1]).read_bytes()
        if (
            "--if-match" in command
            and self.etag(self.objects[key]) != command[command.index("--if-match") + 1]
        ):
            return subprocess.CompletedProcess(command, 1, b"", b"PreconditionFailed")
        self.objects[key] = body
        return subprocess.CompletedProcess(command, 0, b"{}", b"")

    # boto3-style client transport.
    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        from io import BytesIO

        self.wait()
        data = self.objects[kwargs["Key"]]
        if kwargs.get("IfNoneMatch") == self.etag(data):
            raise _client_error("304", "Not Modified")
        return {"Body": BytesIO(data), "ETag": self.etag(data)}

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self.wait()
        key, body = kwargs["Key"], kwargs["Body"]
        if "IfMatch" in kwargs and self.etag(self.objects[key]) != kwargs["IfMatch"]:
            raise _client_error("PreconditionFailed", "Precondition Failed")
        if "IfNoneMatch" in kwargs and key in self.objects:
            raise _client_error("PreconditionFailed", "Precondition Failed")
        self.objects[key] = body
        return {"ETag": self.etag(body)}


def _client_error(code: str, message: str) -> Exception:
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code, "Message": message}}, "op")


def synthetic_document(overdue: int) -> bytes:
    """Roughly the shape of a real list: mostly done tasks, a few dozen open."""
    rng = random.Random(0)
    today = date.today()
    rows: list[list[str]] = []
    for i in range(670):
        done = i >= 80
        if i < overdue:
            due = today - timedelta(days=1 + i % 3)
        elif i < 15:
            due = today
        else:
            due = today + timedelta(days=rng.randint(-200, 30))
        content = f"Task {i} " + "x" * rng.randint(10, 80)
        if i % 7 == 0:
            content += " https://example.com/thing"
        rows.append(
            [
                f"id-{i}",
                content,
                "",
                "notes " * rng.randint(0, 20),
                due.isoformat(),
                "every day" if i % 11 == 0 else "",
                str(i),
                "TRUE" if done else "FALSE",
                "2026-01-01T00:00:00+00:00" if done else "",
            ]
        )
    return json.dumps({"version": 1, "rows": rows}).encode()


def install(s3: SlowS3) -> list[Any]:
    patches: list[Any] = [
        patch.object(StatusDashboard, "_check_for_updates"),
        patch("status_dashboard.app.TODOIST_DUE_NOTIFICATIONS", False),
        patch("status_dashboard.clients.github.get_my_prs", return_value=[]),
        patch("status_dashboard.clients.github.get_review_requests", return_value=[]),
        patch("status_dashboard.clients.github.get_notifications", return_value=[]),
        patch("status_dashboard.clients.linear.get_my_issues", return_value=[]),
        patch("webbrowser.open"),
    ]
    if hasattr(task_store.TaskStore, "_aws"):
        patches.append(patch("status_dashboard.task_store.subprocess.run", s3.run))
    else:
        patches.append(patch.object(task_store, "make_client", lambda _region: s3))
        if hasattr(task_store, "reset_shared_store"):
            task_store.reset_shared_store()
    for p in patches:
        _ = p.start()
    return patches


def rows_shown(table: TodoistDataTable) -> list[str]:
    return [str(table.get_row_at(r)[-1]) for r in range(table.row_count)]


async def until(condition: Callable[[], bool], timeout: float = 60) -> None:
    deadline = time.perf_counter() + timeout
    while not condition():
        if time.perf_counter() > deadline:
            raise TimeoutError
        await asyncio.sleep(0.002)


async def measure(
    pilot: Any,
    keys: list[str],
    visible: Callable[[], bool],
    saved: Callable[[], bool],
) -> tuple[float, float]:
    start = time.perf_counter()
    for key in keys:
        await pilot.press(key)
    await until(visible)
    shown = time.perf_counter() - start
    await until(saved)
    return shown, time.perf_counter() - start


def open_ids(s3: SlowS3) -> set[str]:
    return {r[0] for r in s3.rows() if r[7] != "TRUE"}


def due(s3: SlowS3, task_id: str) -> str:
    return next(r[4] for r in s3.rows() if r[0] == task_id)


async def run_once(latency: float, overdue: int) -> dict[str, tuple[float, float]]:
    s3 = SlowS3(synthetic_document(overdue), latency)
    patches = install(s3)
    results: dict[str, tuple[float, float]] = {}
    today = date.today().isoformat()
    try:
        app = StatusDashboard()
        start = time.perf_counter()
        async with app.run_test(size=(140, 60)) as pilot:
            table = app.query_one("#todoist-table", TodoistDataTable)
            await until(lambda: any("Task" in r for r in rows_shown(table)))
            ready = time.perf_counter() - start
            results["startup: tasks shown"] = (ready, ready)
            _ = table.focus()
            await pilot.pause()

            # Yesterday and back.
            def showing(day: date) -> bool:
                target = day.isoformat()
                want = {
                    r[0]
                    for r in s3.rows()
                    if r[7] != "TRUE"
                    and (
                        r[4][:10] <= target
                        if day == date.today()
                        else r[4][:10] == target
                    )
                }
                have = {t.id for t in app._todoist_tasks}  # pyright: ignore[reportPrivateUsage]
                return bool(want) and have == want

            yesterday = date.today() - timedelta(days=1)
            results["h: show yesterday"] = await measure(
                pilot, ["h"], lambda: showing(yesterday), lambda: True
            )
            results["t: back to today"] = await measure(
                pilot, ["t"], lambda: showing(date.today()), lambda: True
            )

            overdue_ids = [f"id-{i}" for i in range(overdue)]
            results[f"T: reschedule {overdue} overdue"] = await measure(
                pilot,
                ["T"],
                lambda: all(
                    str(table.get_row_at(r)[1]) == "" for r in range(table.row_count)
                ),
                lambda: all(due(s3, i)[:10] == today for i in overdue_ids),
            )

            table.move_cursor(row=0)
            first = app._todoist_tasks[0].id  # pyright: ignore[reportPrivateUsage]
            results["c: complete one"] = await measure(
                pilot,
                ["c"],
                lambda: all(t.id != first for t in app._todoist_tasks),  # pyright: ignore[reportPrivateUsage]
                lambda: first not in open_ids(s3) or due(s3, first)[:10] > today,
            )

            three = [t.id for t in app._todoist_tasks[:3]]  # pyright: ignore[reportPrivateUsage]
            results["ccc: complete three"] = await measure(
                pilot,
                ["c", "c", "c"],
                lambda: not {t.id for t in app._todoist_tasks} & set(three),  # pyright: ignore[reportPrivateUsage]
                lambda: all(
                    i not in open_ids(s3) or due(s3, i)[:10] > today for i in three
                ),
            )

            nxt = app._todoist_tasks[0].id  # pyright: ignore[reportPrivateUsage]
            results["n: defer one"] = await measure(
                pilot,
                ["n"],
                lambda: all(t.id != nxt for t in app._todoist_tasks),  # pyright: ignore[reportPrivateUsage]
                lambda: due(s3, nxt)[:10] > today,
            )

            table.move_cursor(row=0)
            ids = [t.id for t in app._todoist_tasks]  # pyright: ignore[reportPrivateUsage]
            moved = ids[0]

            def order_of(task_id: str) -> int:
                return int(next(r[6] for r in s3.rows() if r[0] == task_id))

            results["JJ: move down twice"] = await measure(
                pilot,
                ["J", "J"],
                lambda: [t.id for t in app._todoist_tasks].index(moved) == 2,  # pyright: ignore[reportPrivateUsage]
                lambda: order_of(moved) > order_of(ids[2]),
            )

            with patch("webbrowser.open") as browser:
                linked = next(
                    i
                    for i, t in enumerate(app._todoist_tasks)  # pyright: ignore[reportPrivateUsage]
                    if "https://" in t.content
                )
                table.move_cursor(row=linked)
                results["o: open link"] = await measure(
                    pilot, ["o"], lambda: browser.called, lambda: True
                )

            table.move_cursor(row=0)
            undone = app._todoist_tasks[0].id  # pyright: ignore[reportPrivateUsage]
            await pilot.press("c")
            await until(
                lambda: undone not in open_ids(s3) or due(s3, undone)[:10] > today
            )
            await app.workers.wait_for_complete()
            results["z: undo complete"] = await measure(
                pilot,
                ["z"],
                lambda: any(t.id == undone for t in app._todoist_tasks),  # pyright: ignore[reportPrivateUsage]
                lambda: undone in open_ids(s3) and due(s3, undone)[:10] <= today,
            )
            await app.workers.wait_for_complete()
    finally:
        for p in patches:
            p.stop()
    results["(S3 requests)"] = (float(s3.requests), float(s3.requests))
    return results


async def main() -> None:
    parser = argparse.ArgumentParser()
    _ = parser.add_argument("--latency", type=float, default=0.14)
    _ = parser.add_argument("--repeat", type=int, default=3)
    _ = parser.add_argument("--overdue", type=int, default=8)
    args = parser.parse_args()
    runs = [await run_once(args.latency, args.overdue) for _ in range(args.repeat)]
    print(f"S3 latency {args.latency * 1000:.0f} ms, median of {args.repeat} runs")
    print(f"{'action':32} {'visible':>10} {'saved':>10}")
    for name in runs[0]:
        shown = statistics.median(r[name][0] for r in runs)
        saved = statistics.median(r[name][1] for r in runs)
        if name.startswith("("):
            print(f"{name:32} {shown:>10.0f}")
        else:
            print(f"{name:32} {shown * 1000:>8.0f}ms {saved * 1000:>8.0f}ms")


if __name__ == "__main__":
    asyncio.run(main())
