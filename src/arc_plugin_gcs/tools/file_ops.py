"""File operations: list, stat, upload, download, delete."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, ClassVar

try:
    from arc.plugin_api import ToolError, ToolInputSchema
except ImportError:  # pragma: no cover — tests run without arc installed
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
from arc_plugin_gcs.tools._base import gate_and_reserve, human_bytes, to_tool_error


# ── gcs_list ──────────────────────────────────────────────────────────────


class GCSList:
    name: ClassVar[str] = "gcs_list"
    description: ClassVar[str] = (
        "List GCS objects under a prefix. Returns one row per object with "
        "URI, size, and last-modified. Capped at max_results (hard ceiling "
        "1000). Recurses by default — for one-level-deep 'directories' use "
        "gcs_dirs instead."
    )

    def __init__(self, ctx: ToolContext) -> None:
        self._ctx = ctx

    @property
    def input_schema(self) -> ToolInputSchema:
        return ToolInputSchema(
            properties={
                "prefix": {
                    "type": "string",
                    "description": (
                        "Anchor prefix. Either `gs://bucket/path/` or a bare "
                        "`path/` (resolves to default bucket). Empty = bucket root."
                    ),
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max objects to return (1–1000).",
                    "minimum": 1,
                    "default": 100,
                },
            },
            required=[],
        )

    def execute(self, input: dict[str, Any]) -> str:
        prefix = str(input.get("prefix", "") or "")
        max_results = int(input.get("max_results", 100))
        max_results = max(1, min(max_results, self._ctx.max_list_results))

        try:
            parsed = self._ctx.client.parse_uri(prefix)
        except Exception as exc:
            raise to_tool_error(exc) from exc

        cost = rates.list_cost()
        gate_and_reserve(
            self._ctx,
            operation="list",
            uri=parsed.gs_uri,
            cost_usd=cost.cost_usd,
        )

        try:
            iterator = self._ctx.client.sdk.list_blobs(
                parsed.bucket,
                prefix=parsed.key or None,
                max_results=max_results,
            )
            blobs = list(iterator)
        except Exception as exc:
            raise to_tool_error(exc, uri=parsed.gs_uri) from exc

        self._ctx.budget.commit(
            api_calls=1, bytes_transferred=0, cost_usd=cost.cost_usd,
        )

        # Determine truncation: if SDK gave us exactly max_results, there
        # might be more. The SDK doesn't tell us authoritatively without
        # another call — conservative: mark truncated if at limit.
        truncated = len(blobs) >= max_results

        self._ctx.emit("gcs.list.completed", payload={
            "prefix": parsed.gs_uri,
            "returned": len(blobs),
            "truncated": truncated,
            "bucket": parsed.bucket,
            "cost_estimate_usd": cost.cost_usd,
            "bytes_transferred": cost.bytes_transferred,
        })

        if not blobs:
            return f"(no objects under {parsed.gs_uri})"
        lines: list[str] = []
        for b in blobs:
            size = human_bytes(int(b.size or 0))
            ts = b.updated.isoformat() if b.updated else "—"
            lines.append(f"gs://{parsed.bucket}/{b.name}    {size}    {ts}")
        suffix = (
            f"\n\n(showing {len(blobs)} of {len(blobs)} matching)"
            if not truncated
            else f"\n\n(showing {len(blobs)}; results truncated at max_results={max_results})"
        )
        return "\n".join(lines) + suffix


# ── gcs_stat ──────────────────────────────────────────────────────────────


class GCSStat:
    name: ClassVar[str] = "gcs_stat"
    description: ClassVar[str] = (
        "Full metadata for one GCS object: size, content_type, updated, md5, "
        "storage_class, custom metadata. Returns JSON."
    )

    def __init__(self, ctx: ToolContext) -> None:
        self._ctx = ctx

    @property
    def input_schema(self) -> ToolInputSchema:
        return ToolInputSchema(
            properties={
                "uri": {
                    "type": "string",
                    "description": "`gs://bucket/key` or bare path (default bucket).",
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

        cost = rates.stat_cost()
        gate_and_reserve(
            self._ctx,
            operation="stat",
            uri=parsed.gs_uri,
            cost_usd=cost.cost_usd,
        )

        try:
            blob = self._ctx.client.blob(parsed)
            blob.reload()
        except Exception as exc:
            raise to_tool_error(exc, uri=parsed.gs_uri) from exc

        self._ctx.budget.commit(api_calls=1, cost_usd=cost.cost_usd)

        meta = {
            "uri": parsed.gs_uri,
            "size_bytes": int(blob.size or 0),
            "content_type": blob.content_type,
            "updated": blob.updated.isoformat() if blob.updated else None,
            "md5": blob.md5_hash,
            "storage_class": blob.storage_class,
            "etag": blob.etag,
            "metadata": dict(blob.metadata) if blob.metadata else {},
        }

        self._ctx.emit("gcs.stat.completed", payload={
            "uri": parsed.gs_uri,
            "size_bytes": meta["size_bytes"],
            "content_type": meta["content_type"],
            "cost_estimate_usd": cost.cost_usd,
            "bytes_transferred": cost.bytes_transferred,
        })
        return json.dumps(meta, indent=2)


# ── gcs_upload ────────────────────────────────────────────────────────────


class GCSUpload:
    name: ClassVar[str] = "gcs_upload"
    description: ClassVar[str] = (
        "Upload a local file to GCS. Refuses to overwrite by default; pass "
        "overwrite=true to replace an existing object (gated via UserGate). "
        "Destination prefixes are created implicitly — upload to "
        "gs://bucket/new/path/file.ext works even if no other object under "
        "'new/path/' exists. GCS is flat; '/' in keys is just a character."
    )

    def __init__(self, ctx: ToolContext) -> None:
        self._ctx = ctx

    @property
    def input_schema(self) -> ToolInputSchema:
        return ToolInputSchema(
            properties={
                "local_path": {
                    "type": "string",
                    "description": "Path to the local file (absolute or workspace-relative).",
                },
                "uri": {
                    "type": "string",
                    "description": "Destination `gs://bucket/key` or bare path. Prefix is created implicitly.",
                },
                "overwrite": {
                    "type": "boolean",
                    "description": "If true and destination exists, overwrite (requires user confirmation).",
                    "default": False,
                },
            },
            required=["local_path", "uri"],
        )

    def execute(self, input: dict[str, Any]) -> str:
        local_path_str = str(input.get("local_path", "")).strip()
        if not local_path_str:
            raise ToolError("`local_path` is required and must be non-empty")
        local = Path(local_path_str).expanduser()
        if not local.exists():
            raise ToolError(f"local file not found: {local_path_str}")
        if not local.is_file():
            raise ToolError(f"local path is not a file: {local_path_str}")

        try:
            parsed = self._ctx.client.parse_uri(
                str(input.get("uri", "")), require_object=True
            )
        except Exception as exc:
            raise to_tool_error(exc) from exc

        overwrite = bool(input.get("overwrite", False))
        size_bytes = local.stat().st_size

        try:
            blob = self._ctx.client.blob(parsed)
            exists = blob.exists()
        except Exception as exc:
            raise to_tool_error(exc, uri=parsed.gs_uri) from exc

        if exists and not overwrite:
            raise ToolError(
                f"would overwrite existing object {parsed.gs_uri!r}; "
                f"pass overwrite=true to confirm"
            )

        operation = "upload_overwrite" if (exists and overwrite) else "upload_new"
        cost = rates.upload_cost(size_bytes)
        gate_and_reserve(
            self._ctx,
            operation=operation,
            uri=parsed.gs_uri,
            bytes_transferred=cost.bytes_transferred,
            cost_usd=cost.cost_usd,
        )

        try:
            blob.upload_from_filename(str(local))
            blob.reload()
        except Exception as exc:
            raise to_tool_error(exc, uri=parsed.gs_uri) from exc

        self._ctx.budget.commit(
            api_calls=1,
            bytes_transferred=cost.bytes_transferred,
            cost_usd=cost.cost_usd,
        )

        self._ctx.emit("gcs.upload.completed", payload={
            "local_path": str(local),
            "uri": parsed.gs_uri,
            "size_bytes": size_bytes,
            "md5": blob.md5_hash,
            "was_overwrite": exists,
            "cost_estimate_usd": cost.cost_usd,
            "bytes_transferred": cost.bytes_transferred,
        })

        action = "uploaded (overwrote)" if exists else "uploaded"
        return (
            f"{action} {local} ({human_bytes(size_bytes)}) → {parsed.gs_uri}\n"
            f"md5: {blob.md5_hash}  etag: {blob.etag}"
        )


# ── gcs_download ──────────────────────────────────────────────────────────


class GCSDownload:
    name: ClassVar[str] = "gcs_download"
    description: ClassVar[str] = (
        "Download a GCS object to a local file. Refuses to overwrite by "
        "default; pass overwrite=true to replace existing files (gated)."
    )

    def __init__(self, ctx: ToolContext) -> None:
        self._ctx = ctx

    @property
    def input_schema(self) -> ToolInputSchema:
        return ToolInputSchema(
            properties={
                "uri": {
                    "type": "string",
                    "description": "Source `gs://bucket/key` or bare path.",
                },
                "local_path": {
                    "type": "string",
                    "description": "Destination on local disk (absolute or workspace-relative).",
                },
                "overwrite": {
                    "type": "boolean",
                    "description": "If true and destination exists, overwrite (requires user confirmation).",
                    "default": False,
                },
            },
            required=["uri", "local_path"],
        )

    def execute(self, input: dict[str, Any]) -> str:
        local_path_str = str(input.get("local_path", "")).strip()
        if not local_path_str:
            raise ToolError("`local_path` is required and must be non-empty")
        local = Path(local_path_str).expanduser()
        try:
            parsed = self._ctx.client.parse_uri(
                str(input.get("uri", "")), require_object=True
            )
        except Exception as exc:
            raise to_tool_error(exc) from exc

        overwrite = bool(input.get("overwrite", False))
        if local.exists() and not overwrite:
            raise ToolError(
                f"would overwrite local file {local!s}; "
                f"pass overwrite=true to confirm"
            )
        if not local.parent.exists():
            raise ToolError(f"parent directory does not exist: {local.parent}")

        try:
            blob = self._ctx.client.blob(parsed)
            blob.reload()  # need size for cost calc; this is its own Class B op
            size_bytes = int(blob.size or 0)
        except Exception as exc:
            raise to_tool_error(exc, uri=parsed.gs_uri) from exc

        operation = "download_overwrite" if local.exists() else "stat"
        # The download itself is a separate op, but we treat the whole
        # logical operation as one budget item.
        cost = rates.download_cost(size_bytes)
        gate_and_reserve(
            self._ctx,
            operation=operation if local.exists() else "stat",
            uri=parsed.gs_uri,
            bytes_transferred=cost.bytes_transferred,
            cost_usd=cost.cost_usd,
        )

        was_overwrite = local.exists()
        try:
            blob.download_to_filename(str(local))
        except Exception as exc:
            raise to_tool_error(exc, uri=parsed.gs_uri) from exc

        self._ctx.budget.commit(
            api_calls=1,
            bytes_transferred=cost.bytes_transferred,
            cost_usd=cost.cost_usd,
        )

        self._ctx.emit("gcs.download.completed", payload={
            "uri": parsed.gs_uri,
            "local_path": str(local),
            "size_bytes": size_bytes,
            "was_overwrite": was_overwrite,
            "cost_estimate_usd": cost.cost_usd,
            "bytes_transferred": cost.bytes_transferred,
        })

        action = "downloaded (overwrote)" if was_overwrite else "downloaded"
        return f"{action} {parsed.gs_uri} ({human_bytes(size_bytes)}) → {local}"


# ── gcs_delete ────────────────────────────────────────────────────────────


class GCSDelete:
    name: ClassVar[str] = "gcs_delete"
    description: ClassVar[str] = (
        "Delete a GCS object. ALWAYS gated through UserGate regardless of "
        "escalation_level — destructive operation."
    )

    def __init__(self, ctx: ToolContext) -> None:
        self._ctx = ctx

    @property
    def input_schema(self) -> ToolInputSchema:
        return ToolInputSchema(
            properties={
                "uri": {
                    "type": "string",
                    "description": "`gs://bucket/key` or bare path of the object to delete.",
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

        # Get size first so the delete event payload is informative.
        try:
            blob = self._ctx.client.blob(parsed)
            blob.reload()
            size_bytes = int(blob.size or 0)
        except Exception as exc:
            raise to_tool_error(exc, uri=parsed.gs_uri) from exc

        cost = rates.delete_cost()
        gate_and_reserve(
            self._ctx,
            operation="delete",
            uri=parsed.gs_uri,
            cost_usd=cost.cost_usd,
        )

        try:
            blob.delete()
        except Exception as exc:
            raise to_tool_error(exc, uri=parsed.gs_uri) from exc

        self._ctx.budget.commit(api_calls=1, cost_usd=cost.cost_usd)

        self._ctx.emit("gcs.delete.completed", payload={
            "uri": parsed.gs_uri,
            "size_bytes": size_bytes,
            "cost_estimate_usd": cost.cost_usd,
            "bytes_transferred": cost.bytes_transferred,
        })
        return f"deleted {parsed.gs_uri} ({human_bytes(size_bytes)})"
