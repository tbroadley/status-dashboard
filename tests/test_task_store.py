import hashlib
import json
import os
import subprocess
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

from status_dashboard.task_store import (
    Conflict,
    StoreError,
    TaskStore,
    decode_document,
    encode_document,
    parse_uri,
)


def row(
    task_id: str, content: str = "Task", due: str = "2026-09-15", order: int = 0
) -> list[str]:
    return [task_id, content, "", "", due, "", str(order), "FALSE", ""]


class FakeS3:
    def __init__(self, rows: list[list[str]] | None = None):
        self.objects: dict[str, bytes] = (
            {} if rows is None else {"tasks.json": encode_document(rows)}
        )
        self.commands: list[list[str]] = []
        self.fail: str | None = None
        self.fail_history: bool = False
        self.before_write: Callable[[], None] | None = None
        self.after_write: Callable[[], None] | None = None

    @staticmethod
    def etag(data: bytes) -> str:
        return '"' + hashlib.sha256(data).hexdigest() + '"'

    def run(
        self, command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        self.commands.append(command)
        key = command[command.index("--key") + 1]
        if self.fail:
            return subprocess.CompletedProcess(command, 1, b"", self.fail.encode())
        if command[2] == "get-object":
            if key not in self.objects:
                return subprocess.CompletedProcess(command, 1, b"", b"NoSuchKey")
            path = Path(command[command.index("--key") + 2])
            _ = path.write_bytes(self.objects[key])
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps({"ETag": self.etag(self.objects[key])}).encode(),
                b"",
            )
        if self.before_write and key == "tasks.json":
            self.before_write()
        if self.fail_history and ".history/" in key:
            return subprocess.CompletedProcess(command, 1, b"", b"AccessDenied")
        if "--if-none-match" in command and key in self.objects:
            return subprocess.CompletedProcess(command, 1, b"", b"PreconditionFailed")
        if "--if-match" in command:
            if (
                key not in self.objects
                or self.etag(self.objects[key])
                != command[command.index("--if-match") + 1]
            ):
                return subprocess.CompletedProcess(
                    command, 1, b"", b"PreconditionFailed"
                )
        path = Path(command[command.index("--body") + 1])
        assert path.stat().st_mode & 0o777 == 0o600
        self.objects[key] = path.read_bytes()
        if self.after_write and key == "tasks.json":
            self.after_write()
        return subprocess.CompletedProcess(command, 0, b"{}", b"")


class TestTaskStore(unittest.TestCase):
    @override
    def setUp(self):
        env = patch.dict(
            os.environ,
            {"TASKS_S3_URI": "s3://example-bucket/tasks.json", "TASKS_AWS_CLI": "aws"},
        )
        _ = cast(object, self.enterContext(env))

    def test_schema_rejects_bad_data(self):
        for document in [
            b"not json",
            b"{}",
            b'{"version":true,"rows":[]}',
            b'{"version":2,"rows":[]}',
            b'{"version":1,"rows":[["short"]]}',
            json.dumps({"version": 1, "rows": [row("same"), row("same")]}).encode(),
            json.dumps({"version": 1, "rows": [row(" ")]}).encode(),
        ]:
            with self.subTest(document=document), self.assertRaises(StoreError):
                _ = decode_document(document)
        self.assertEqual(
            decode_document(encode_document([row("a", "Unicode 📝")])),
            [row("a", "Unicode 📝")],
        )

    def test_configuration(self):
        self.assertEqual(
            parse_uri("s3://example-bucket/folder/a.json"),
            ("example-bucket", "folder/a.json"),
        )
        for uri in [
            "",
            "https://example.com/a",
            "s3://bucket",
            "s3://bucket/",
            "s3://a@bucket/key",
            "s3://bucket/key?query",
            "s3://bucket:port/key",
            "s3://[/key",
        ]:
            with self.subTest(uri=uri), self.assertRaises(StoreError):
                _ = parse_uri(uri)

    def test_backup_precedes_conditional_write(self):
        fake = FakeS3([row("a")])
        before = fake.objects["tasks.json"]
        with patch("status_dashboard.task_store.subprocess.run", side_effect=fake.run):
            self.assertTrue(
                TaskStore().update(lambda rows: (rows.append(row("b")) or True))
            )
        self.assertEqual(len(decode_document(fake.objects["tasks.json"])), 2)
        history = [data for key, data in fake.objects.items() if ".history/" in key]
        self.assertEqual(history, [before])
        self.assertIn("--if-none-match", fake.commands[1])
        self.assertIn("--if-match", fake.commands[2])

    def test_conflict_rereads_preserving_concurrent_edit(self):
        fake = FakeS3([row("a")])

        def concurrent_edit():
            fake.objects["tasks.json"] = encode_document([row("a", "Concurrent edit")])
            fake.before_write = None

        fake.before_write = concurrent_edit
        with patch("status_dashboard.task_store.subprocess.run", side_effect=fake.run):
            self.assertTrue(
                TaskStore().update(lambda rows: (rows.append(row("b")) or True))
            )
        rows = decode_document(fake.objects["tasks.json"])
        self.assertEqual(rows[0][1], "Concurrent edit")
        self.assertEqual([r[0] for r in rows], ["a", "b"])

    def test_persistent_conflict_is_bounded(self):
        fake = FakeS3([row("a")])
        counter = 0

        def conflict():
            nonlocal counter
            counter += 1
            fake.objects["tasks.json"] = encode_document([row("a", str(counter))])

        fake.before_write = conflict
        with (
            patch("status_dashboard.task_store.subprocess.run", side_effect=fake.run),
            self.assertRaises(Conflict),
        ):
            _ = TaskStore().update(lambda rows: (rows.append(row("b")) or True))
        self.assertEqual(counter, 3)

    def test_backup_failure_prevents_write(self):
        fake = FakeS3([row("a")])
        before = fake.objects["tasks.json"]
        fake.fail_history = True
        with (
            patch("status_dashboard.task_store.subprocess.run", side_effect=fake.run),
            self.assertRaises(StoreError),
        ):
            _ = TaskStore().update(lambda rows: (rows.append(row("b")) or True))
        self.assertEqual(fake.objects["tasks.json"], before)

    def test_missing_corrupt_or_inaccessible_never_initializes(self):
        for data in (None, b"broken", b"{}"):
            fake = FakeS3()
            if data is not None:
                fake.objects["tasks.json"] = data
            with (
                patch(
                    "status_dashboard.task_store.subprocess.run", side_effect=fake.run
                ),
                self.assertRaises(StoreError),
            ):
                _ = TaskStore().update(lambda rows: (rows.append(row("a")) or True))
            self.assertEqual(len(fake.commands), 1)
        fake = FakeS3([])
        fake.fail = "AccessDenied private-location secret-detail"
        with patch("status_dashboard.task_store.subprocess.run", side_effect=fake.run):
            with self.assertRaises(StoreError) as caught:
                _ = TaskStore().read()
        self.assertNotIn("private-location", str(caught.exception))
        self.assertNotIn("secret-detail", str(caught.exception))

    def test_explicit_initialize_cannot_overwrite(self):
        fake = FakeS3()
        with patch("status_dashboard.task_store.subprocess.run", side_effect=fake.run):
            TaskStore().initialize([row("a")])
            with self.assertRaises(Conflict):
                TaskStore().initialize([])
        self.assertEqual(len(decode_document(fake.objects["tasks.json"])), 1)

    def test_noop_needs_no_write(self):
        fake = FakeS3([])
        with patch("status_dashboard.task_store.subprocess.run", side_effect=fake.run):
            self.assertTrue(TaskStore().update(lambda _rows: True))
        self.assertEqual(len(fake.commands), 1)

    def test_lost_write_response_is_reconciled(self):
        fake = FakeS3([row("a")])

        def lose_response():
            raise subprocess.TimeoutExpired("aws", 40)

        fake.after_write = lose_response
        with patch("status_dashboard.task_store.subprocess.run", side_effect=fake.run):
            self.assertTrue(
                TaskStore().update(lambda rows: (rows.append(row("b")) or True))
            )
        self.assertEqual(
            [r[0] for r in decode_document(fake.objects["tasks.json"])], ["a", "b"]
        )
        self.assertEqual(len(fake.commands), 4)

    def test_unreconciled_write_is_explicitly_unknown(self):
        fake = FakeS3([row("a")])

        def disconnect():
            fake.fail = "connection reset"
            raise subprocess.TimeoutExpired("aws", 40)

        fake.after_write = disconnect
        with (
            patch("status_dashboard.task_store.subprocess.run", side_effect=fake.run),
            self.assertRaisesRegex(StoreError, "Save outcome unknown"),
        ):
            _ = TaskStore().update(lambda rows: (rows.append(row("b")) or True))
        self.assertEqual(len(fake.commands), 4)

    def test_sso_role_denial_is_not_expired_credentials(self):
        fake = FakeS3([])
        fake.fail = "AccessDenied for assumed-role/AWSReservedSSO_Example/user"
        with (
            patch("status_dashboard.task_store.subprocess.run", side_effect=fake.run),
            self.assertRaisesRegex(StoreError, "denied"),
        ):
            _ = TaskStore().read()

    def test_expired_credentials_and_missing_cli(self):
        fake = FakeS3([])
        fake.fail = "ExpiredToken"
        with (
            patch("status_dashboard.task_store.subprocess.run", side_effect=fake.run),
            self.assertRaisesRegex(StoreError, "aws sso login"),
        ):
            _ = TaskStore().read()
        with (
            patch(
                "status_dashboard.task_store.subprocess.run",
                side_effect=FileNotFoundError,
            ),
            self.assertRaisesRegex(StoreError, "AWS CLI not found"),
        ):
            _ = TaskStore().read()
