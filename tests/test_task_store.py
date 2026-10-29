import hashlib
import io
import json
import os
import time
import unittest
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import cast, override
from unittest.mock import patch

from botocore.exceptions import ClientError, NoCredentialsError, ReadTimeoutError

from status_dashboard import task_store
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


def client_error(code: str, status: int = 400) -> ClientError:
    return ClientError(
        {  # pyright: ignore[reportArgumentType]
            "Error": {"Code": code, "Message": "private-location secret-detail"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "Operation",
    )


class FakeS3:
    """An in-memory stand-in for the boto3 S3 client, with conditional writes."""

    def __init__(self, rows: list[list[str]] | None = None):
        self.objects: dict[str, bytes] = (
            {} if rows is None else {"tasks.json": encode_document(rows)}
        )
        self.calls: list[tuple[str, str, dict[str, object]]] = []
        self.fail: Exception | None = None
        self.fail_history: bool = False
        self.before_write: Callable[[], None] | None = None
        self.after_write: Callable[[], None] | None = None

    @staticmethod
    def etag(data: bytes) -> str:
        return '"' + hashlib.sha256(data).hexdigest() + '"'

    @contextmanager
    def installed(self) -> Iterator[None]:
        task_store.reset_shared_store()
        with patch("status_dashboard.task_store.make_client", return_value=self):
            yield
        task_store.reset_shared_store()

    def ops(self) -> list[str]:
        return [op for op, _key, _kwargs in self.calls]

    def get_object(self, **kwargs: object) -> dict[str, object]:
        key = str(kwargs["Key"])
        self.calls.append(("get", key, kwargs))
        if self.fail:
            raise self.fail
        if key not in self.objects:
            raise client_error("NoSuchKey", 404)
        data = self.objects[key]
        if kwargs.get("IfNoneMatch") == self.etag(data):
            raise client_error("304", 304)
        return {"Body": io.BytesIO(data), "ETag": self.etag(data)}

    def head_object(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("head", str(kwargs["Key"]), kwargs))
        return {}

    def put_object(self, **kwargs: object) -> dict[str, object]:
        key = str(kwargs["Key"])
        self.calls.append(("put", key, kwargs))
        if self.fail:
            raise self.fail
        if self.before_write and key == "tasks.json":
            self.before_write()
        if self.fail_history and ".history/" in key:
            raise client_error("AccessDenied", 403)
        if kwargs.get("IfNoneMatch") == "*" and key in self.objects:
            raise client_error("PreconditionFailed", 412)
        if "IfMatch" in kwargs and (
            key not in self.objects or self.etag(self.objects[key]) != kwargs["IfMatch"]
        ):
            raise client_error("PreconditionFailed", 412)
        data = cast(bytes, kwargs["Body"])
        self.objects[key] = data
        if self.after_write and key == "tasks.json":
            self.after_write()
        return {"ETag": self.etag(data)}


class TestTaskStore(unittest.TestCase):
    sleeps: list[float] = []

    @override
    def setUp(self):
        env = patch.dict(os.environ, {"TASKS_S3_URI": "s3://example-bucket/tasks.json"})
        _ = cast(object, self.enterContext(env))
        self.sleeps = []
        _ = cast(
            object,
            self.enterContext(
                patch(
                    "status_dashboard.task_store.time.sleep",
                    side_effect=self.sleeps.append,
                )
            ),
        )

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
        with fake.installed():
            self.assertTrue(
                TaskStore().update(lambda rows: (rows.append(row("b")) or True))
            )
        self.assertEqual(len(decode_document(fake.objects["tasks.json"])), 2)
        history = [data for key, data in fake.objects.items() if ".history/" in key]
        self.assertEqual(history, [before])
        self.assertEqual(fake.calls[1][2]["IfNoneMatch"], "*")
        self.assertIn("IfMatch", fake.calls[2][2])

    def test_writes_reuse_the_cached_version(self):
        fake = FakeS3([row("a")])
        with fake.installed():
            store = TaskStore()
            _ = store.read()
            written = store.update(lambda rows: (rows.append(row("b")) or True))
            assert written is not None
            self.assertEqual([r[0] for r in written.rows], ["a", "b"])
            _ = store.update(lambda rows: (rows.append(row("c")) or True))
            # One read, then two (backup, write) pairs with no re-read between.
            self.assertEqual(fake.ops(), ["get", "put", "put", "put", "put"])
            # An unchanged document is not downloaded again.
            self.assertEqual(
                store.read().rows, decode_document(fake.objects["tasks.json"])
            )
            self.assertEqual(fake.ops()[-1], "get")
            self.assertEqual(len(fake.calls), 6)

    def test_stale_cache_is_corrected_by_conditional_write(self):
        fake = FakeS3([row("a")])
        with fake.installed():
            store = TaskStore()
            _ = store.read()
            # Another client adds a task the cached version doesn't have.
            fake.objects["tasks.json"] = encode_document([row("a"), row("x")])
            _ = store.update(lambda rows: (rows.append(row("b")) or True))
            self.assertEqual(
                [r[0] for r in decode_document(fake.objects["tasks.json"])],
                ["a", "x", "b"],
            )
            self.assertEqual(self.sleeps, [])

            # An edit that finds nothing to do in the stale version checks S3.
            fake.objects["tasks.json"] = encode_document([row("a"), row("y")])

            def delete_y(rows: list[list[str]]) -> bool:
                before = len(rows)
                rows[:] = [r for r in rows if r[0] != "y"]
                return len(rows) != before

            self.assertIsNotNone(store.update(delete_y))
            self.assertEqual(
                [r[0] for r in decode_document(fake.objects["tasks.json"])], ["a"]
            )

    def test_conflict_rereads_preserving_concurrent_edit(self):
        fake = FakeS3([row("a")])

        def concurrent_edit():
            fake.objects["tasks.json"] = encode_document([row("a", "Concurrent edit")])
            fake.before_write = None

        fake.before_write = concurrent_edit
        with fake.installed():
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
        with fake.installed(), self.assertRaises(Conflict):
            _ = TaskStore().update(lambda rows: (rows.append(row("b")) or True))
        self.assertEqual(counter, 6)
        # Each retry waits longer (with jitter) before re-reading.
        self.assertEqual(len(self.sleeps), 5)
        self.assertLess(self.sleeps[0], self.sleeps[-1])

    def test_backup_failure_prevents_write(self):
        fake = FakeS3([row("a")])
        before = fake.objects["tasks.json"]
        fake.fail_history = True
        with fake.installed(), self.assertRaises(StoreError):
            _ = TaskStore().update(lambda rows: (rows.append(row("b")) or True))
        self.assertEqual(fake.objects["tasks.json"], before)

    def test_missing_corrupt_or_inaccessible_never_initializes(self):
        for data in (None, b"broken", b"{}"):
            fake = FakeS3()
            if data is not None:
                fake.objects["tasks.json"] = data
            with fake.installed(), self.assertRaises(StoreError):
                _ = TaskStore().update(lambda rows: (rows.append(row("a")) or True))
            self.assertEqual(len(fake.calls), 1)
        fake = FakeS3([])
        fake.fail = client_error("AccessDenied", 403)
        with fake.installed(), self.assertRaises(StoreError) as caught:
            _ = TaskStore().read()
        self.assertIn("denied", str(caught.exception))
        self.assertNotIn("private-location", str(caught.exception))
        self.assertNotIn("secret-detail", str(caught.exception))

    def test_explicit_initialize_cannot_overwrite(self):
        fake = FakeS3()
        with fake.installed():
            TaskStore().initialize([row("a")])
            with self.assertRaises(Conflict):
                TaskStore().initialize([])
        self.assertEqual(len(decode_document(fake.objects["tasks.json"])), 1)

    def test_noop_needs_no_write(self):
        fake = FakeS3([])
        with fake.installed():
            self.assertTrue(TaskStore().update(lambda _rows: True))
        self.assertEqual(len(fake.calls), 1)

    def test_lost_write_response_is_reconciled(self):
        fake = FakeS3([row("a")])

        def lose_response():
            raise ReadTimeoutError(endpoint_url="https://s3")

        fake.after_write = lose_response
        with fake.installed():
            self.assertTrue(
                TaskStore().update(lambda rows: (rows.append(row("b")) or True))
            )
        self.assertEqual(
            [r[0] for r in decode_document(fake.objects["tasks.json"])], ["a", "b"]
        )
        self.assertEqual(fake.ops(), ["get", "put", "put", "get"])

    def test_unreconciled_write_is_explicitly_unknown(self):
        fake = FakeS3([row("a")])

        def disconnect():
            fake.fail = ReadTimeoutError(endpoint_url="https://s3")
            raise ReadTimeoutError(endpoint_url="https://s3")

        fake.after_write = disconnect
        with (
            fake.installed(),
            self.assertRaisesRegex(StoreError, "Save outcome unknown"),
        ):
            _ = TaskStore().update(lambda rows: (rows.append(row("b")) or True))
        self.assertEqual(fake.ops(), ["get", "put", "put", "get"])

    def test_expired_credentials_ask_for_sign_in_and_get_a_new_client(self):
        for error in (client_error("ExpiredToken"), NoCredentialsError()):
            with self.subTest(error=type(error).__name__):
                fake = FakeS3([])
                fake.fail = error
                with (
                    patch(
                        "status_dashboard.task_store.make_client", return_value=fake
                    ) as make_client,
                ):
                    store = TaskStore()
                    with self.assertRaisesRegex(StoreError, "aws sso login"):
                        _ = store.read()
                    fake.fail = None
                    self.assertEqual(store.read().rows, [])
                    self.assertEqual(make_client.call_count, 2)

    def test_warm_only_reconnects_after_idle(self):
        fake = FakeS3([])
        with fake.installed():
            store = TaskStore()
            store.warm()
            store.warm()
            self.assertEqual(fake.ops(), ["head"])
            with patch(
                "status_dashboard.task_store.time.monotonic",
                return_value=time.monotonic() + task_store.IDLE_CONNECTION_SECONDS + 1,
            ):
                store.warm()
            self.assertEqual(fake.ops(), ["head", "head"])

    def test_shared_store_follows_configuration(self):
        task_store.reset_shared_store()
        first = task_store.shared_store()
        self.assertIs(task_store.shared_store(), first)
        with patch.dict(os.environ, {"TASKS_S3_URI": "s3://example-bucket/other.json"}):
            self.assertIsNot(task_store.shared_store(), first)
        task_store.reset_shared_store()
