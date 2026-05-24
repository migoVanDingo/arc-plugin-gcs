"""Test fixtures.

The plugin can be unit-tested without google-cloud-storage installed
because every place we touch the SDK goes through GCSClient or a
small surface of `blob.*` / `client.list_blobs(...)`. A FakeStorageClient
that mimics that surface lets every test run offline + deterministic.

For integration tests against a real bucket, see test_integration_real.py
(opt-in via ARC_GCS_TEST_BUCKET).
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest


# ── Fake storage client ───────────────────────────────────────────────────


@dataclass
class FakeBlob:
    """Mimics google.cloud.storage.Blob's relevant attribute surface."""
    name: str
    size: int = 0
    content_type: str | None = None
    updated: datetime | None = None
    md5_hash: str | None = "fakemd5=="
    storage_class: str = "STANDARD"
    etag: str = "fake-etag"
    metadata: dict[str, Any] | None = None
    # Stored content used by download_*; None means "doesn't exist"
    _content: bytes | None = None
    _bucket_name: str = ""

    def exists(self) -> bool:
        return self._content is not None

    def reload(self) -> None:
        # In the fake, reload is a no-op — attributes are already set.
        if self._content is None:
            from arc_plugin_gcs.tools._base import ToolError
            # Raise a NotFound-ish error so tools surface it as ToolError
            class NotFound(Exception):
                pass
            raise NotFound(f"404 No such object: {self._bucket_name}/{self.name}")

    def upload_from_filename(self, path: str) -> None:
        with open(path, "rb") as f:
            data = f.read()
        self._content = data
        self.size = len(data)
        self.md5_hash = "fakemd5-uploaded"
        if not self.updated:
            self.updated = datetime.now(timezone.utc)

    def download_to_filename(self, path: str) -> None:
        if self._content is None:
            raise FileNotFoundError(f"no blob content: {self.name}")
        with open(path, "wb") as f:
            f.write(self._content)

    def download_as_text(self, encoding: str = "utf-8") -> str:
        return (self._content or b"").decode(encoding, errors="replace")

    def download_as_bytes(self, *, start: int | None = None, end: int | None = None) -> bytes:
        data = self._content or b""
        if start is None and end is None:
            return data
        s = start or 0
        # end is INCLUSIVE in GCS — matches what the SDK does.
        e = (end + 1) if end is not None else len(data)
        return data[s:e]

    def delete(self) -> None:
        self._content = None

    def generate_signed_url(self, *, version: str, expiration: timedelta, method: str) -> str:
        return (
            f"https://storage.googleapis.com/{self._bucket_name}/{self.name}"
            f"?X-Goog-Algorithm=FAKE&X-Goog-Expires={int(expiration.total_seconds())}"
        )


@dataclass
class FakeBucket:
    name: str
    blobs: dict[str, FakeBlob] = field(default_factory=dict)

    def blob(self, key: str) -> FakeBlob:
        if key not in self.blobs:
            self.blobs[key] = FakeBlob(name=key, _bucket_name=self.name)
        return self.blobs[key]


class FakeBlobIterator:
    """Mimics the iterator returned by client.list_blobs.

    The real iterator carries `.prefixes` (a set populated when
    list_blobs is called with `delimiter=`). We mirror that.
    """
    def __init__(self, blobs: list[FakeBlob], prefixes: set[str] | None = None) -> None:
        self._blobs = blobs
        self.prefixes = prefixes or set()

    def __iter__(self):
        return iter(self._blobs)


@dataclass
class FakeStorageClient:
    """Replacement for google.cloud.storage.Client in unit tests."""
    buckets: dict[str, FakeBucket] = field(default_factory=dict)
    closed: bool = False

    def bucket(self, name: str) -> FakeBucket:
        if name not in self.buckets:
            self.buckets[name] = FakeBucket(name=name)
        return self.buckets[name]

    def list_blobs(
        self,
        bucket_name: str,
        *,
        prefix: str | None = None,
        max_results: int | None = None,
        delimiter: str | None = None,
    ) -> FakeBlobIterator:
        bucket = self.buckets.get(bucket_name, FakeBucket(name=bucket_name))
        candidates = [b for b in bucket.blobs.values() if b._content is not None]
        if prefix:
            candidates = [b for b in candidates if b.name.startswith(prefix)]

        if delimiter:
            # Split into "direct" objects and implied prefixes.
            direct: list[FakeBlob] = []
            implied: set[str] = set()
            base = prefix or ""
            for b in candidates:
                remainder = b.name[len(base):]
                if delimiter in remainder:
                    # Implied subdirectory.
                    sub = remainder.split(delimiter, 1)[0] + delimiter
                    implied.add(base + sub)
                else:
                    direct.append(b)
            if max_results is not None:
                direct = direct[:max_results]
            return FakeBlobIterator(direct, prefixes=implied)

        candidates.sort(key=lambda b: b.name)
        if max_results is not None:
            candidates = candidates[:max_results]
        return FakeBlobIterator(candidates)

    def close(self) -> None:
        self.closed = True


# ── Stub bus / build context (mirror arc's shapes) ────────────────────────


class StubBus:
    def __init__(self) -> None:
        self.emitted: list[Any] = []

    def emit(self, event: Any) -> None:
        self.emitted.append(event)

    def types(self) -> list[str]:
        return [getattr(e, "type", "?") for e in self.emitted]


@dataclass(frozen=True)
class StubSessionContext:
    session_id: str = "ses_test"
    workspace: str = "/tmp"
    provider_name: str = "anthropic"
    provider_model: str = "claude-haiku-4-5"
    started_at: str = "2026-05-24T00:00:00Z"


class AllowGate:
    """UserGate stub that allows everything."""
    def confirm(self, prompt: str, *, scope_id: str | None = None) -> bool:
        return True


class DenyGate:
    """UserGate stub that denies everything (mirrors NoOpGate)."""
    def confirm(self, prompt: str, *, scope_id: str | None = None) -> bool:
        return False


class RecordingGate:
    """Records confirm calls; configurable verdict."""
    def __init__(self, verdict: bool = True) -> None:
        self.calls: list[dict] = []
        self.verdict = verdict

    def confirm(self, prompt: str, *, scope_id: str | None = None) -> bool:
        self.calls.append({"prompt": prompt, "scope_id": scope_id})
        return self.verdict


@dataclass
class StubBuildContext:
    bus: Any = None
    user_gate: Any = None
    sessions_dir: str = "/tmp/sessions"
    session_id: str = "ses_test"
    config_snapshot_yaml: str | None = None


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def fake_sdk() -> FakeStorageClient:
    return FakeStorageClient()


@pytest.fixture
def bus() -> StubBus:
    return StubBus()


@pytest.fixture
def allow_gate() -> AllowGate:
    return AllowGate()


@pytest.fixture
def deny_gate() -> DenyGate:
    return DenyGate()


@pytest.fixture
def session_ctx() -> StubSessionContext:
    return StubSessionContext()


@pytest.fixture
def build_ctx(bus, allow_gate) -> StubBuildContext:
    return StubBuildContext(bus=bus, user_gate=allow_gate)


@pytest.fixture
def make_client(fake_sdk):
    """Construct a GCSClient pre-loaded with fake_sdk + sensible defaults."""
    from arc_plugin_gcs.client import GCSClient

    def _make(
        *,
        allowed_buckets: list[str] | None = None,
        default_bucket: str | int | None = "_SENTINEL",
    ) -> GCSClient:
        buckets = allowed_buckets or ["my-bucket", "scratch"]
        # If caller didn't pin a default, infer "my-bucket" when it's in the
        # allowlist; otherwise None. Avoids conflict with custom allowlists.
        if default_bucket == "_SENTINEL":
            default_bucket = "my-bucket" if "my-bucket" in buckets else None
        return GCSClient(
            sdk_client=fake_sdk,
            allowed_buckets=buckets,
            default_bucket=default_bucket,
        )

    return _make


@pytest.fixture
def make_ctx(make_client, bus, allow_gate):
    """Construct a ToolContext wired to fakes."""
    from arc_plugin_gcs.budget import BudgetCaps, SessionBudget
    from arc_plugin_gcs.tools import ToolContext

    def _make(
        *,
        escalation_level: str = "destructive",
        budget_caps: BudgetCaps | None = None,
        gate=None,
        client=None,
    ) -> ToolContext:
        return ToolContext(
            client=client or make_client(),
            budget=SessionBudget(budget_caps or BudgetCaps()),
            escalation_level=escalation_level,  # type: ignore[arg-type]
            user_gate=gate if gate is not None else allow_gate,
            bus=bus,
        )

    return _make


def seed_blob(
    fake_sdk: FakeStorageClient,
    *,
    bucket: str,
    key: str,
    content: bytes = b"hi",
    content_type: str = "text/plain",
    updated: datetime | None = None,
) -> FakeBlob:
    """Helper: put a blob in the fake client. Returns the FakeBlob."""
    b = fake_sdk.bucket(bucket).blob(key)
    b._content = content
    b.size = len(content)
    b.content_type = content_type
    b.updated = updated or datetime.now(timezone.utc)
    return b


@pytest.fixture
def seed(fake_sdk):
    """Curried seed_blob bound to the fixture's fake_sdk."""
    def _seed(**kwargs) -> FakeBlob:
        return seed_blob(fake_sdk, **kwargs)
    return _seed
