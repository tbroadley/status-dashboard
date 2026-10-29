from __future__ import annotations

import datetime as dt
import json
import os
import random
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, TypeVar, cast, final
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from mypy_boto3_s3.type_defs import GetObjectOutputTypeDef, PutObjectOutputTypeDef

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
CONFLICT_BACKOFF = 0.25
CONFLICT_BACKOFF_CAP = 2.0
# S3 drops idle keep-alive connections after about five seconds, and a new
# TLS connection costs several round trips, so `warm` reconnects ahead of use.
IDLE_CONNECTION_SECONDS = 4.0


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


@dataclass(frozen=True)
class Snapshot:
    """A version of the document. Treat `rows` as read-only: snapshots are shared."""

    rows: Rows
    etag: str
    data: bytes


class S3Client(Protocol):
    def get_object(self, **kwargs: object) -> GetObjectOutputTypeDef: ...
    def put_object(self, **kwargs: object) -> PutObjectOutputTypeDef: ...
    def head_object(self, **kwargs: object) -> object: ...


def make_client(region: str) -> S3Client:
    """An S3 client on the standard AWS credential chain (env, profile, SSO)."""
    import boto3
    from botocore.config import Config

    config = Config(
        connect_timeout=10,
        read_timeout=20,
        retries={"mode": "standard", "total_max_attempts": 1},
        tcp_keepalive=True,
    )
    client = boto3.session.Session().client(  # pyright: ignore[reportUnknownMemberType]
        "s3", region_name=region or None, config=config
    )
    return cast("S3Client", cast(object, client))


class _NotModified(Exception):
    pass


_SIGN_IN_CODES = {
    "ExpiredToken",
    "ExpiredTokenException",
    "InvalidToken",
    "InvalidClientTokenId",
    "InvalidAccessKeyId",
    "SignatureDoesNotMatch",
}


def _translate(error: Exception) -> Exception:
    """Map a botocore error to a StoreError without leaking bucket details."""
    from botocore import exceptions as boto

    if isinstance(error, boto.ClientError):
        info = error.response.get("Error", {})
        code = str(info.get("Code", ""))
        status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
        if code == "304" or status == 304:
            return _NotModified()
        if code in ("PreconditionFailed", "ConditionalRequestConflict") or status in (
            409,
            412,
        ):
            return Conflict("The task list changed concurrently; refresh and retry.")
        if code in _SIGN_IN_CODES:
            return StoreError("AWS sign-in required; run `aws sso login` and retry.")
        if code in ("NoSuchKey", "404") or status == 404:
            return StoreError(
                "Task document missing; explicitly initialize or import it with task-store."
            )
        if code == "AccessDenied" or status == 403:
            return StoreError(
                "AWS denied task storage access; check your sign-in and configured location."
            )
        return RequestFailed(
            "S3 request failed; check storage configuration and connectivity."
        )
    if isinstance(
        error,
        (
            boto.NoCredentialsError,
            boto.PartialCredentialsError,
            boto.CredentialRetrievalError,
            boto.SSOError,
            boto.TokenRetrievalError,
            boto.UnauthorizedSSOTokenError,
            boto.SSOTokenLoadError,
        ),
    ):
        return StoreError("AWS sign-in required; run `aws sso login` and retry.")
    if isinstance(error, boto.ProfileNotFound):
        return StoreError("AWS profile not found; check your AWS configuration.")
    return RequestFailed(
        "AWS request could not complete; check connectivity and retry."
    )


@final
class TaskStore:
    """The task document in S3, with the last known version cached.

    One long-lived client keeps its connection open, so a request costs one
    round trip instead of a process launch plus a TLS handshake. Reads send the
    cached ETag and skip the download when nothing changed; writes start from
    the cached version and only re-read when S3 reports it is stale.
    """

    def __init__(self) -> None:
        self.bucket, self.key = parse_uri(os.environ.get("TASKS_S3_URI", ""))
        self.region = os.environ.get("TASKS_AWS_REGION", "")
        self._client: S3Client | None = None
        self._client_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self._snapshot: Snapshot | None = None
        self._snapshot_stamp = 0
        self._clock = 0
        self._last_request = 0.0

    @property
    def config(self) -> tuple[str, str, str]:
        return self.bucket, self.key, self.region

    def _get_client(self) -> S3Client:
        with self._client_lock:
            if self._client is None:
                try:
                    self._client = make_client(self.region)
                except Exception as error:  # noqa: BLE001 - botocore raises many types
                    raise _translate(error) from error
            return self._client

    def _call(self, request: Callable[[S3Client], _T]) -> _T:
        client = self._get_client()
        try:
            return request(client)
        except Exception as error:  # noqa: BLE001 - botocore raises many types
            translated = _translate(error)
            if isinstance(translated, StoreError) and "sign-in" in str(translated):
                # Pick up fresh credentials (e.g. after `aws sso login`) next time.
                with self._client_lock:
                    self._client = None
            raise translated from error
        finally:
            self._last_request = time.monotonic()

    def _tick(self) -> int:
        with self._cache_lock:
            self._clock += 1
            return self._clock

    def _remember(self, snapshot: Snapshot, stamp: int) -> None:
        """Cache `snapshot` unless a newer one is already cached.

        `stamp` orders versions: a read is as new as when it started, a write
        as new as when it finished.
        """
        with self._cache_lock:
            if stamp >= self._snapshot_stamp:
                self._snapshot, self._snapshot_stamp = snapshot, stamp

    @property
    def cached(self) -> Snapshot | None:
        return self._snapshot

    def warm(self) -> None:
        """Open a connection now if the last one has probably gone idle."""
        if time.monotonic() - self._last_request < IDLE_CONNECTION_SECONDS:
            return
        try:
            _ = self._call(lambda s3: s3.head_object(Bucket=self.bucket, Key=self.key))
        except StoreError:
            pass

    def read(self) -> Snapshot:
        stamp = self._tick()
        cached = self._snapshot

        def get(s3: S3Client) -> tuple[bytes, object]:
            kwargs: dict[str, object] = {"Bucket": self.bucket, "Key": self.key}
            if cached is not None:
                kwargs["IfNoneMatch"] = cached.etag
            response = s3.get_object(**kwargs)
            return response["Body"].read(), response.get("ETag")

        try:
            data, etag = self._call(get)
        except _NotModified:
            assert cached is not None
            return cached
        if not isinstance(etag, str) or not etag:
            raise StoreError("S3 response has no ETag; refusing an unsafe update.")
        snapshot = Snapshot(decode_document(data), etag, data)
        self._remember(snapshot, stamp)
        return snapshot

    def _put(self, key: str, data: bytes, condition: dict[str, str]) -> str:
        response = self._call(
            lambda s3: s3.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=data,
                ContentType="application/json",
                **condition,
            )
        )
        return str(response.get("ETag", ""))

    def _write_current(self, rows: Rows, data: bytes, condition: dict[str, str]) -> str:
        """Write the document; return its new ETag."""
        try:
            return self._put(self.key, data, condition)
        except RequestFailed as error:
            try:
                readback: Snapshot | None = self.read()
            except (StoreError, OSError):
                readback = None
            if readback is None or readback.rows != rows:
                raise StoreError(
                    "Save outcome unknown: it may have reached S3. Check AWS sign-in/connectivity "
                    + "and refresh the task list before retrying."
                ) from error
            return readback.etag

    def initialize(self, rows: Rows) -> None:
        _ = self._write_current(rows, encode_document(rows), {"IfNoneMatch": "*"})

    def update(
        self, edit: Callable[[Rows], bool], *, attempts: int = 6
    ) -> Snapshot | None:
        """Apply `edit` to the latest rows and write them conditionally.

        Returns the resulting version, or None if `edit` declined to change
        anything. The first try edits the cached version, saving a read; if
        that is stale the conditional put fails and the edit is redone on a
        fresh read. Another client writing between our read and write also
        fails the put; retry against a fresh read after a jittered, growing
        pause so writers that collided once are unlikely to collide again.
        """
        with self._write_lock:
            snapshot = self._snapshot
            attempt = 0
            while attempt < attempts:
                if snapshot is None:
                    if attempt:
                        time.sleep(
                            min(
                                CONFLICT_BACKOFF_CAP,
                                CONFLICT_BACKOFF * 2.0 ** (attempt - 1),
                            )
                            * random.uniform(0.5, 1.5)
                        )
                    snapshot = self.read()
                    from_cache = False
                else:
                    from_cache = True
                rows = [row.copy() for row in snapshot.rows]
                changed = edit(rows)
                if not changed or rows == snapshot.rows:
                    if from_cache:
                        # Only S3 can say the edit really is unneeded.
                        snapshot = None
                        continue
                    return snapshot if changed else None
                data = encode_document(rows)
                stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                history_key = f"{self.key}.history/{stamp}-{uuid.uuid4()}.json"
                _ = self._put(history_key, snapshot.data, {"IfNoneMatch": "*"})
                try:
                    etag = self._write_current(rows, data, {"IfMatch": snapshot.etag})
                except Conflict:
                    snapshot = None
                    if not from_cache:
                        attempt += 1
                    continue
                written = Snapshot(rows, etag, data)
                self._remember(written, self._tick())
                return written
        raise Conflict("The task list keeps changing; refresh and retry.")


_T = TypeVar("_T")
_shared: TaskStore | None = None
_shared_lock = threading.Lock()


def shared_store() -> TaskStore:
    """The process-wide store, so its connection and cached version are reused."""
    global _shared
    store = TaskStore()
    with _shared_lock:
        if _shared is None or _shared.config != store.config:
            _shared = store
        return _shared


def reset_shared_store() -> None:
    global _shared
    with _shared_lock:
        _shared = None
