"""Sharing / cross-provider tools: gcs_signed_url, gcs_read_text."""
from __future__ import annotations

from datetime import timedelta
from typing import Any, ClassVar

try:
    from arc.plugin_api import ToolError, ToolInputSchema
except ImportError:  # pragma: no cover
    from arc_plugin_gcs.tools._base import ToolError  # type: ignore[misc]

    class ToolInputSchema:  # type: ignore[no-redef]
        def __init__(self, properties, required):
            self.properties = properties
            self.required = required

        def to_json_schema(self):
            return {"type": "object", "properties": self.properties,
                    "required": self.required}

from arc_plugin_gcs import rates
from arc_plugin_gcs.tools import ToolContext
from arc_plugin_gcs.tools._base import gate_and_reserve, to_tool_error


# Content types treated as text-shaped for gcs_read_text. Binary types
# (image/*, video/*, application/octet-stream, etc.) are rejected.
_TEXT_CT_PREFIXES = ("text/",)
_TEXT_CT_EXACT = frozenset({
    "application/json",
    "application/xml",
    "application/yaml",
    "application/x-yaml",
    "application/javascript",
    "application/x-www-form-urlencoded",
    "application/x-sh",
    "application/x-toml",
})


def _is_text_content_type(ct: str | None) -> bool:
    if not ct:
        return False
    base = ct.split(";", 1)[0].strip().lower()
    if base in _TEXT_CT_EXACT:
        return True
    return any(base.startswith(p) for p in _TEXT_CT_PREFIXES)


# ── gcs_signed_url ────────────────────────────────────────────────────────


class GCSSignedURL:
    name: ClassVar[str] = "gcs_signed_url"
    description: ClassVar[str] = (
        "Generate a time-limited HTTPS URL for a GCS object. Use this as a "
        "cross-provider bridge — any LLM that accepts image/video URLs can "
        "consume a signed URL even if it lacks native GCS support."
    )

    def __init__(self, ctx: ToolContext) -> None:
        self._ctx = ctx

    @property
    def input_schema(self) -> ToolInputSchema:
        return ToolInputSchema(
            properties={
                "uri": {
                    "type": "string",
                    "description": "`gs://bucket/key` or bare path.",
                },
                "expires_in_minutes": {
                    "type": "integer",
                    "description": "URL lifetime in minutes (clamped to max_signed_url_minutes).",
                    "default": 60,
                    "minimum": 1,
                },
            },
            required=["uri"],
        )

    def execute(self, input: dict[str, Any]) -> str:
        try:
            parsed = self._ctx.client.parse_uri(
                str(input.get("uri", "")), require_object=True
            )
        except Exception as exc:
            raise to_tool_error(exc) from exc

        requested = int(input.get("expires_in_minutes", 60))
        if requested < 1:
            raise ToolError("`expires_in_minutes` must be >= 1")
        clamped = min(requested, self._ctx.max_signed_url_minutes)
        was_clamped = clamped < requested

        cost = rates.signed_url_cost()
        gate_and_reserve(
            self._ctx,
            operation="signed_url",
            uri=parsed.gs_uri,
            cost_usd=cost.cost_usd,
        )

        try:
            blob = self._ctx.client.blob(parsed)
            url = blob.generate_signed_url(
                version="v4",
                expiration=timedelta(minutes=clamped),
                method="GET",
            )
        except Exception as exc:
            raise to_tool_error(exc, uri=parsed.gs_uri) from exc

        self._ctx.budget.commit(api_calls=1, cost_usd=cost.cost_usd)

        # NOTE: URL itself MUST NOT appear in the event payload — it's
        # a credential. Only metadata.
        self._ctx.emit("gcs.signed_url.issued", payload={
            "uri": parsed.gs_uri,
            "expires_in_minutes": clamped,
            "cost_estimate_usd": cost.cost_usd,
            "bytes_transferred": cost.bytes_transferred,
        })

        if was_clamped:
            return (
                f"{url}\n"
                f"\n(requested {requested}min, clamped to "
                f"max_signed_url_minutes={self._ctx.max_signed_url_minutes})"
            )
        return url


# ── gcs_read_text ─────────────────────────────────────────────────────────


class GCSReadText:
    name: ClassVar[str] = "gcs_read_text"
    description: ClassVar[str] = (
        "Read a text-shaped GCS object directly into the tool output. "
        "Refuses binary content types (image, video, octet-stream, etc.) — "
        "use gcs_download for those. Capped at max_text_read_bytes."
    )

    def __init__(self, ctx: ToolContext) -> None:
        self._ctx = ctx

    @property
    def input_schema(self) -> ToolInputSchema:
        return ToolInputSchema(
            properties={
                "uri": {
                    "type": "string",
                    "description": "`gs://bucket/key` or bare path.",
                },
                "max_bytes": {
                    "type": "integer",
                    "description": "Truncate after this many bytes (capped by plugin config).",
                    "minimum": 1,
                },
            },
            required=["uri"],
        )

    def execute(self, input: dict[str, Any]) -> str:
        try:
            parsed = self._ctx.client.parse_uri(
                str(input.get("uri", "")), require_object=True
            )
        except Exception as exc:
            raise to_tool_error(exc) from exc

        requested = int(input.get("max_bytes", self._ctx.max_text_read_bytes))
        if requested < 1:
            raise ToolError("`max_bytes` must be >= 1")
        cap = min(requested, self._ctx.max_text_read_bytes)

        try:
            blob = self._ctx.client.blob(parsed)
            blob.reload()
        except Exception as exc:
            raise to_tool_error(exc, uri=parsed.gs_uri) from exc

        if not _is_text_content_type(blob.content_type):
            raise ToolError(
                f"content_type {blob.content_type!r} is not text-shaped; "
                f"use gcs_download for binary content"
            )

        size_bytes = int(blob.size or 0)
        bytes_to_read = min(size_bytes, cap)
        truncated = size_bytes > cap

        cost = rates.read_text_cost(bytes_to_read)
        gate_and_reserve(
            self._ctx,
            operation="read_text",
            uri=parsed.gs_uri,
            bytes_transferred=cost.bytes_transferred,
            cost_usd=cost.cost_usd,
        )

        try:
            if truncated:
                # Range read via download_as_bytes(start=, end=)
                data = blob.download_as_bytes(start=0, end=cap - 1)
                text = data.decode("utf-8", errors="replace")
            else:
                text = blob.download_as_text()
        except Exception as exc:
            raise to_tool_error(exc, uri=parsed.gs_uri) from exc

        self._ctx.budget.commit(
            api_calls=1,
            bytes_transferred=cost.bytes_transferred,
            cost_usd=cost.cost_usd,
        )

        self._ctx.emit("gcs.read_text.completed", payload={
            "uri": parsed.gs_uri,
            "bytes_read": bytes_to_read,
            "truncated": truncated,
            "cost_estimate_usd": cost.cost_usd,
            "bytes_transferred": cost.bytes_transferred,
        })

        suffix = (
            f"\n\n(truncated at {cap} bytes; full object is {size_bytes} bytes)"
            if truncated else ""
        )
        return text + suffix
