"""Wrapper around google.cloud.storage.Client.

Owns three concerns the bare SDK doesn't:
  1. URI parsing — `gs://bucket/key` ↔ (bucket, key) tuples
  2. Default-bucket resolution — bare paths like `foo/bar.txt` resolve
     to the configured default bucket
  3. Bucket allowlist enforcement — every operation is checked before
     touching the SDK

URI parsing failures and allowlist denials raise GCSClientError, which
the tools catch and convert to ToolError with the original message.
"""
from __future__ import annotations

from dataclasses import dataclass


class GCSClientError(Exception):
    """Raised for URI parse failures, allowlist denials, etc.

    Tools catch this and re-raise as ToolError so the agent sees a
    structured failure.
    """


@dataclass(frozen=True)
class ParsedURI:
    """Normalized form of a GCS reference."""
    bucket: str
    key: str          # may be empty (bucket root)

    @property
    def gs_uri(self) -> str:
        if not self.key:
            return f"gs://{self.bucket}/"
        return f"gs://{self.bucket}/{self.key}"


class GCSClient:
    """The wrapper. One instance per plugin (one per session)."""

    def __init__(
        self,
        *,
        sdk_client,
        allowed_buckets: list[str],
        default_bucket: str | None,
    ) -> None:
        self._client = sdk_client
        self._allowed = frozenset(allowed_buckets)
        self._default = default_bucket
        if not self._allowed:
            raise GCSClientError(
                "allowed_buckets is empty — plugin must fail closed. "
                "Configure at least one bucket in plugins.gcs.config."
            )
        if default_bucket is not None and default_bucket not in self._allowed:
            raise GCSClientError(
                f"default_bucket={default_bucket!r} must be in allowed_buckets "
                f"({sorted(self._allowed)})"
            )

    # ── Underlying SDK access (kept narrow) ────────────────────────────────

    @property
    def sdk(self):
        """Exposed for tools that need the raw SDK (signed URLs, listing).
        Tools should still go through parse_uri + check_allowed first.
        """
        return self._client

    def bucket(self, name: str):
        """SDK bucket handle. Caller is expected to have passed `name`
        through `check_allowed` already."""
        return self._client.bucket(name)

    def blob(self, parsed: ParsedURI):
        if not parsed.key:
            raise GCSClientError(
                f"URI {parsed.gs_uri!r} refers to the bucket root; expected an object key"
            )
        return self._client.bucket(parsed.bucket).blob(parsed.key)

    # ── URI parsing + allowlist ────────────────────────────────────────────

    def parse_uri(self, uri: str, *, require_object: bool = False) -> ParsedURI:
        """Normalize a URI or bare path to (bucket, key).

        - `gs://bucket/path/to/file.ext` → ("bucket", "path/to/file.ext")
        - `gs://bucket/` or `gs://bucket` → ("bucket", "")
        - `path/to/file.ext` (no scheme) → uses default_bucket
        - `""` → bucket root of default_bucket

        Raises GCSClientError on malformed input or missing default_bucket.
        Also enforces the bucket allowlist before returning.
        """
        if uri is None:
            raise GCSClientError("URI must not be None")
        s = str(uri).strip()
        if s.startswith("gs://"):
            rest = s[len("gs://"):]
            if not rest:
                raise GCSClientError(f"malformed URI: {uri!r} has no bucket")
            if "/" not in rest:
                bucket, key = rest, ""
            else:
                bucket, key = rest.split("/", 1)
            if not bucket:
                raise GCSClientError(f"malformed URI: {uri!r} (empty bucket)")
        elif s.startswith("gs:/") and not s.startswith("gs://"):
            raise GCSClientError(
                f"malformed URI: {uri!r} (missing slash; expected 'gs://bucket/key')"
            )
        else:
            # Bare path → default bucket required.
            if self._default is None:
                raise GCSClientError(
                    f"relative path {uri!r} needs a default_bucket "
                    f"configured, or use a full gs:// URI"
                )
            bucket, key = self._default, s.lstrip("/")

        self.check_allowed(bucket)
        parsed = ParsedURI(bucket=bucket, key=key)
        if require_object and not parsed.key:
            raise GCSClientError(
                f"URI {parsed.gs_uri!r} refers to the bucket root; expected an object key"
            )
        return parsed

    def check_allowed(self, bucket: str) -> None:
        """Raise if `bucket` is not in the allowlist."""
        if bucket not in self._allowed:
            # Don't echo the full allowlist to the model (infra disclosure).
            raise GCSClientError(
                f"bucket {bucket!r} is not in the configured allowed_buckets"
            )

    def close(self) -> None:
        """Best-effort SDK client close. Idempotent."""
        client_close = getattr(self._client, "close", None)
        if callable(client_close):
            try:
                client_close()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass
