"""Overview tools: gcs_recent, gcs_summarize_bucket, gcs_dirs, gcs_estimate_storage_cost.

These tools return cheap-to-compute survey data so the agent doesn't
have to page through tens of thousands of objects to understand a
bucket.
"""
from __future__ import annotations

from collections import Counter
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
from arc_plugin_gcs.rates import UnknownRateError, format_cost_usd
from arc_plugin_gcs.tools import ToolContext
from arc_plugin_gcs.tools._base import gate_and_reserve, human_bytes, to_tool_error


# ── gcs_recent ────────────────────────────────────────────────────────────


class GCSRecent:
    name: ClassVar[str] = "gcs_recent"
    description: ClassVar[str] = (
        "List the N most-recently-modified objects under a prefix. "
        "Cheap survey for 'what did I just upload?' or 'what changed today?'"
    )

    def __init__(self, ctx: ToolContext) -> None:
        self._ctx = ctx

    @property
    def input_schema(self) -> ToolInputSchema:
        return ToolInputSchema(
            properties={
                "prefix": {
                    "type": "string",
                    "description": "`gs://bucket/path/` or bare path. Empty = default bucket root.",
                },
                "n": {
                    "type": "integer",
                    "description": "Number of recent objects to return (1–100).",
                    "default": 10,
                    "minimum": 1,
                    "maximum": 100,
                },
            },
            required=[],
        )

    def execute(self, input: dict[str, Any]) -> str:
        prefix = str(input.get("prefix", "") or "")
        n = max(1, min(int(input.get("n", 10)), 100))

        try:
            parsed = self._ctx.client.parse_uri(prefix)
        except Exception as exc:
            raise to_tool_error(exc) from exc

        cost = rates.list_cost()
        gate_and_reserve(
            self._ctx, operation="recent", uri=parsed.gs_uri,
            cost_usd=cost.cost_usd,
        )

        try:
            iterator = self._ctx.client.sdk.list_blobs(
                parsed.bucket,
                prefix=parsed.key or None,
                max_results=self._ctx.max_list_results,
            )
            blobs = list(iterator)
        except Exception as exc:
            raise to_tool_error(exc, uri=parsed.gs_uri) from exc

        self._ctx.budget.commit(api_calls=1, cost_usd=cost.cost_usd)

        blobs.sort(key=lambda b: b.updated or 0, reverse=True)
        top = blobs[:n]

        self._ctx.emit("gcs.recent.completed", payload={
            "prefix": parsed.gs_uri,
            "returned": len(top),
            "cost_estimate_usd": cost.cost_usd,
            "bytes_transferred": cost.bytes_transferred,
        })

        if not top:
            return f"(no objects under {parsed.gs_uri})"
        lines = []
        for b in top:
            size = human_bytes(int(b.size or 0))
            ts = b.updated.isoformat() if b.updated else "—"
            lines.append(f"gs://{parsed.bucket}/{b.name}    {size}    {ts}")
        return "\n".join(lines) + f"\n\n({len(top)} most-recent objects under {parsed.gs_uri})"


# ── gcs_summarize_bucket ──────────────────────────────────────────────────


class GCSSummarizeBucket:
    name: ClassVar[str] = "gcs_summarize_bucket"
    description: ClassVar[str] = (
        "Aggregate survey of objects under a prefix: total count, total "
        "size, optional breakdown by file extension. Avoids listing every "
        "object into context when you just need 'is there video content here?'"
    )

    def __init__(self, ctx: ToolContext) -> None:
        self._ctx = ctx

    @property
    def input_schema(self) -> ToolInputSchema:
        return ToolInputSchema(
            properties={
                "prefix": {
                    "type": "string",
                    "description": "`gs://bucket/path/` or bare path. Empty = default bucket root.",
                },
                "breakdown": {
                    "type": "boolean",
                    "description": "Include per-extension table. Set false for just totals.",
                    "default": True,
                },
            },
            required=[],
        )

    def execute(self, input: dict[str, Any]) -> str:
        prefix = str(input.get("prefix", "") or "")
        breakdown = bool(input.get("breakdown", True))

        try:
            parsed = self._ctx.client.parse_uri(prefix)
        except Exception as exc:
            raise to_tool_error(exc) from exc

        cost = rates.list_cost()
        gate_and_reserve(
            self._ctx, operation="summarize_bucket", uri=parsed.gs_uri,
            cost_usd=cost.cost_usd,
        )

        try:
            iterator = self._ctx.client.sdk.list_blobs(
                parsed.bucket, prefix=parsed.key or None,
            )
            blobs = list(iterator)
        except Exception as exc:
            raise to_tool_error(exc, uri=parsed.gs_uri) from exc

        self._ctx.budget.commit(api_calls=1, cost_usd=cost.cost_usd)

        n = len(blobs)
        total_bytes = sum(int(b.size or 0) for b in blobs)
        oldest = min((b.updated for b in blobs if b.updated), default=None)
        newest = max((b.updated for b in blobs if b.updated), default=None)

        self._ctx.emit("gcs.summarize_bucket.completed", payload={
            "prefix": parsed.gs_uri,
            "n_objects": n,
            "total_bytes": total_bytes,
            "breakdown_included": breakdown,
            "cost_estimate_usd": cost.cost_usd,
            "bytes_transferred": cost.bytes_transferred,
        })

        if n == 0:
            return f"(no objects under {parsed.gs_uri})"

        lines = [f"{parsed.gs_uri} — {n:,} objects, {human_bytes(total_bytes)} total"]
        if breakdown:
            ext_counts: Counter[str] = Counter()
            ext_bytes: Counter[str] = Counter()
            for b in blobs:
                if "." in b.name.rsplit("/", 1)[-1]:
                    ext = "." + b.name.rsplit(".", 1)[-1].lower()
                else:
                    ext = "(no ext)"
                ext_counts[ext] += 1
                ext_bytes[ext] += int(b.size or 0)
            lines.append("")
            lines.append("By extension:")
            for ext, count in sorted(
                ext_counts.items(), key=lambda kv: -ext_bytes[kv[0]],
            ):
                lines.append(
                    f"  {ext:<8} {count:>5}  {human_bytes(ext_bytes[ext])}"
                )

        lines.append("")
        if oldest:
            lines.append(f"Oldest: {oldest.isoformat()}")
        if newest:
            lines.append(f"Newest: {newest.isoformat()}")
        return "\n".join(lines)


# ── gcs_dirs ──────────────────────────────────────────────────────────────


class GCSDirs:
    name: ClassVar[str] = "gcs_dirs"
    description: ClassVar[str] = (
        "Return the immediate 'subdirectories' under a prefix using GCS's "
        "delimiter convention. GCS keys are flat — these are key prefixes "
        "that end at the delimiter. Useful for navigation when the agent "
        "wants to know 'what folders exist in this bucket?'"
    )

    def __init__(self, ctx: ToolContext) -> None:
        self._ctx = ctx

    @property
    def input_schema(self) -> ToolInputSchema:
        return ToolInputSchema(
            properties={
                "prefix": {
                    "type": "string",
                    "description": "`gs://bucket/path/` or bare path. Empty = default bucket root.",
                },
                "delimiter": {
                    "type": "string",
                    "description": "Path separator. Almost always `/`.",
                    "default": "/",
                },
            },
            required=[],
        )

    def execute(self, input: dict[str, Any]) -> str:
        prefix = str(input.get("prefix", "") or "")
        delimiter = str(input.get("delimiter", "/")) or "/"

        try:
            parsed = self._ctx.client.parse_uri(prefix)
        except Exception as exc:
            raise to_tool_error(exc) from exc

        cost = rates.list_cost()
        gate_and_reserve(
            self._ctx, operation="dirs", uri=parsed.gs_uri,
            cost_usd=cost.cost_usd,
        )

        try:
            iterator = self._ctx.client.sdk.list_blobs(
                parsed.bucket,
                prefix=parsed.key or None,
                delimiter=delimiter,
            )
            # Consume to populate `prefixes` attribute.
            _ = list(iterator)
            prefixes = sorted(iterator.prefixes or [])
        except Exception as exc:
            raise to_tool_error(exc, uri=parsed.gs_uri) from exc

        self._ctx.budget.commit(api_calls=1, cost_usd=cost.cost_usd)

        self._ctx.emit("gcs.dirs.completed", payload={
            "prefix": parsed.gs_uri,
            "returned": len(prefixes),
            "delimiter": delimiter,
            "cost_estimate_usd": cost.cost_usd,
            "bytes_transferred": cost.bytes_transferred,
        })

        if not prefixes:
            return f"no directories under {parsed.gs_uri}"
        lines = [f"gs://{parsed.bucket}/{p}" for p in prefixes]
        return (
            "\n".join(lines)
            + f"\n\n({len(prefixes)} directories under {parsed.gs_uri})"
        )


# ── gcs_estimate_storage_cost ─────────────────────────────────────────────


class GCSEstimateStorageCost:
    name: ClassVar[str] = "gcs_estimate_storage_cost"
    description: ClassVar[str] = (
        "Estimate monthly storage cost for objects under a prefix using "
        "the public GCS rate card. NOT consulted from Billing API; result "
        "is an estimate, not a billed amount."
    )

    def __init__(self, ctx: ToolContext) -> None:
        self._ctx = ctx

    @property
    def input_schema(self) -> ToolInputSchema:
        return ToolInputSchema(
            properties={
                "prefix": {
                    "type": "string",
                    "description": "`gs://bucket/path/` or bare path. Empty = default bucket root.",
                },
                "region": {
                    "type": "string",
                    "description": (
                        "Storage region key. One of: us-multi, us-region, "
                        "eu-multi, eu-region, asia-multi, asia-region."
                    ),
                    "default": "us-multi",
                },
                "storage_class": {
                    "type": "string",
                    "description": "One of: STANDARD, NEARLINE, COLDLINE, ARCHIVE.",
                    "default": "STANDARD",
                },
            },
            required=[],
        )

    def execute(self, input: dict[str, Any]) -> str:
        prefix = str(input.get("prefix", "") or "")
        region = str(input.get("region", "us-multi"))
        storage_class = str(input.get("storage_class", "STANDARD")).upper()

        try:
            parsed = self._ctx.client.parse_uri(prefix)
        except Exception as exc:
            raise to_tool_error(exc) from exc

        cost = rates.list_cost()
        gate_and_reserve(
            self._ctx, operation="estimate_storage_cost", uri=parsed.gs_uri,
            cost_usd=cost.cost_usd,
        )

        try:
            iterator = self._ctx.client.sdk.list_blobs(
                parsed.bucket, prefix=parsed.key or None,
            )
            blobs = list(iterator)
        except Exception as exc:
            raise to_tool_error(exc, uri=parsed.gs_uri) from exc

        self._ctx.budget.commit(api_calls=1, cost_usd=cost.cost_usd)

        n = len(blobs)
        total_bytes = sum(int(b.size or 0) for b in blobs)

        try:
            monthly = rates.monthly_storage_cost_usd(
                total_bytes=total_bytes,
                region=region,
                storage_class=storage_class,
            )
        except UnknownRateError as exc:
            raise ToolError(str(exc)) from exc

        # Look up the per-GB rate for the output rendering.
        rate_per_gb = rates.STORAGE_RATE_USD_PER_GB_MONTH[(region, storage_class)]

        self._ctx.emit("gcs.estimate_storage_cost.completed", payload={
            "prefix": parsed.gs_uri,
            "n_objects": n,
            "total_bytes": total_bytes,
            "region": region,
            "storage_class": storage_class,
            "monthly_estimate_usd": monthly,
            "cost_estimate_usd": cost.cost_usd,
            "bytes_transferred": cost.bytes_transferred,
        })

        return (
            f"{parsed.gs_uri} — {human_bytes(total_bytes)} across {n:,} objects\n"
            f"  storage class:  {storage_class}\n"
            f"  region:         {region}\n"
            f"  rate:           ${rate_per_gb:.4f} / GB-month\n"
            f"  monthly est:    {format_cost_usd(monthly)}  "
            f"(storage only; egress + ops not included)\n"
            f"\n"
            f"Note: estimated from public rate card, not Billing API. "
            f"Actual cost may differ based on contract pricing, free tier, "
            f"or volume discounts."
        )
