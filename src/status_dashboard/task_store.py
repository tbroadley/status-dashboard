from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast, final
from urllib.parse import urlsplit

HEADER = [
    "id",
    "content",
    "project",
    "description",
    "due",
    "recurrence",
    "order",
    "done",
    "completed_at",
]
Rows = list[list[str]]


class StoreError(Exception):
    pass


class Conflict(StoreError):
    pass


class RequestFailed(StoreError):
    pass


def decode_document(data: bytes) -> Rows:
    try:
        raw = cast(object, json.loads(data))
        if not isinstance(raw, dict):
            raise ValueError
        doc = cast(dict[str, object], raw)
        if type(doc.get("version")) is not int or doc["version"] != 1:
            raise ValueError
        raw_rows = doc.get("rows")
        if not isinstance(raw_rows, list):
            raise ValueError
        rows: Rows = []
        ids: set[str] = set()
        for raw_row in cast(list[object], raw_rows):
            if not isinstance(raw_row, list):
                raise ValueError
            cells = cast(list[object], raw_row)
            if len(cells) != len(HEADER) or not all(isinstance(x, str) for x in cells):
                raise ValueError
            row = cast(list[str], cells)
            if not row[0].strip() or row[0] != row[0].strip() or row[0] in ids:
                raise ValueError
            ids.add(row[0])
            rows.append(row)
        return rows
    except (ValueError, TypeError, UnicodeError) as exc:
        raise StoreError("Invalid task document; refusing to replace it.") from exc


def encode_document(rows: Rows) -> bytes:
    data = json.dumps({"version": 1, "rows": rows}, ensure_ascii=False).encode("utf-8")
    _ = decode_document(data)
    return data


def parse_uri(uri: str) -> tuple[str, str]:
    try:
        parsed = urlsplit(uri)
    except ValueError as exc:
        raise StoreError("Set TASKS_S3_URI to an s3://bucket/key object URI.") from exc
    if (
        not uri.startswith("s3://")
        or not parsed.netloc
        or not parsed.path.strip("/")
        or "?" in uri
        or "#" in uri
        or "@" in parsed.netloc
        or any(c.isspace() for c in parsed.netloc)
        or ":" in parsed.netloc
    ):
        raise StoreError("Set TASKS_S3_URI to an s3://bucket/key object URI.")
    return parsed.netloc, parsed.path[1:]


@dataclass
class Snapshot:
    rows: Rows
    etag: str
    data: bytes


@final
class TaskStore:
    def __init__(self) -> None:
        self.bucket, self.key = parse_uri(os.environ.get("TASKS_S3_URI", ""))
        self.region = os.environ.get("TASKS_AWS_REGION", "")
        self.cli = os.environ.get("TASKS_AWS_CLI", "aws")

    def _aws(self, args: list[str]) -> dict[str, object]:
        command = [
            self.cli,
            "s3api",
            *args,
            "--output",
            "json",
            "--no-cli-pager",
            "--cli-connect-timeout",
            "10",
            "--cli-read-timeout",
            "20",
        ]
        if self.region:
            command += ["--region", self.region]
        env = {
            **os.environ,
            "AWS_PAGER": "",
            "AWS_CLI_AUTO_PROMPT": "off",
            "AWS_MAX_ATTEMPTS": "1",
        }
        try:
            result = subprocess.run(command, capture_output=True, timeout=40, env=env)
        except FileNotFoundError as exc:
            raise StoreError(
                "AWS CLI not found; install a current AWS CLI or set TASKS_AWS_CLI."
            ) from exc
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RequestFailed(
                "AWS request could not complete; check connectivity and retry."
            ) from exc
        if result.returncode:
            error = result.stderr.decode("utf-8", errors="replace").lower()
            if "preconditionfailed" in error or "conditionalrequestconflict" in error:
                raise Conflict("The task list changed concurrently; refresh and retry.")
            if any(
                s in error
                for s in (
                    "expiredtoken",
                    "sso session",
                    "sso token",
                    "token has expired",
                    "invalid_grant",
                    "invalidclienttokenid",
                    "unable to locate credentials",
                )
            ):
                raise StoreError("AWS sign-in required; run `aws sso login` and retry.")
            if "nosuchkey" in error:
                raise StoreError(
                    "Task document missing; explicitly initialize or import it with task-store."
                )
            if "accessdenied" in error:
                raise StoreError(
                    "AWS denied task storage access; check your sign-in and configured location."
                )
            if "unknown options" in error:
                raise StoreError(
                    "Update the AWS CLI: conditional S3 writes are required."
                )
            raise RequestFailed(
                "S3 request failed; check storage configuration and connectivity."
            )
        try:
            metadata = cast(object, json.loads(result.stdout))
            if not isinstance(metadata, dict):
                raise ValueError
            return cast(dict[str, object], metadata)
        except (ValueError, UnicodeError) as exc:
            raise RequestFailed("AWS CLI returned invalid metadata.") from exc

    def read(self) -> Snapshot:
        with tempfile.TemporaryDirectory(prefix="task-store-") as directory:
            path = Path(directory) / "document.json"
            metadata = self._aws(
                ["get-object", "--bucket", self.bucket, "--key", self.key, str(path)]
            )
            data = path.read_bytes()
        etag = metadata.get("ETag")
        if not isinstance(etag, str) or not etag:
            raise StoreError("S3 response has no ETag; refusing an unsafe update.")
        return Snapshot(decode_document(data), etag, data)

    def _put(self, key: str, data: bytes, condition: list[str]) -> None:
        with tempfile.TemporaryDirectory(prefix="task-store-") as directory:
            path = Path(directory) / "document.json"
            _ = path.write_bytes(data)
            path.chmod(0o600)
            _ = self._aws(
                [
                    "put-object",
                    "--bucket",
                    self.bucket,
                    "--key",
                    key,
                    "--body",
                    str(path),
                    "--content-type",
                    "application/json",
                    *condition,
                ]
            )

    def _write_current(self, rows: Rows, data: bytes, condition: list[str]) -> None:
        try:
            self._put(self.key, data, condition)
        except RequestFailed as error:
            try:
                confirmed = self.read().rows == rows
            except (StoreError, OSError):
                confirmed = False
            if not confirmed:
                raise StoreError(
                    "Save outcome unknown: it may have reached S3. Check AWS sign-in/connectivity "
                    + "and refresh the task list before retrying."
                ) from error

    def initialize(self, rows: Rows) -> None:
        self._write_current(rows, encode_document(rows), ["--if-none-match", "*"])

    def update(self, edit: Callable[[Rows], bool], *, attempts: int = 3) -> bool:
        for _ in range(attempts):
            snapshot = self.read()
            rows = [row.copy() for row in snapshot.rows]
            if not edit(rows):
                return False
            if rows == snapshot.rows:
                return True
            data = encode_document(rows)
            stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            history_key = f"{self.key}.history/{stamp}-{uuid.uuid4()}.json"
            self._put(history_key, snapshot.data, ["--if-none-match", "*"])
            try:
                self._write_current(rows, data, ["--if-match", snapshot.etag])
                return True
            except Conflict:
                continue
        raise Conflict("The task list keeps changing; refresh and retry.")
