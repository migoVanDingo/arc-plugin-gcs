"""Log-line formatters for gcs.* events.

Used by arc's log_writer plugin (per 0008-foundation-logging). Pure
functions: turn one RuntimeEvent into one or more
(logger_name, level, message) tuples.

To wire these into log_writer, arc's formatter dispatch needs a
generic "discover formatter from event_type prefix" mechanism. For
now this file is consumed manually; future PRs may auto-register.
"""
from __future__ import annotations

import logging
from typing import Any

from arc_plugin_gcs.rates import format_cost_usd
from arc_plugin_gcs.tools._base import human_bytes


_LOGGER = "arc.gcs"


def _cost_segment(payload: dict[str, Any]) -> str:
    """Render the ' · $X.XXXX' trailer if cost_estimate_usd is present."""
    if "cost_estimate_usd" in payload:
        return f" · {format_cost_usd(payload['cost_estimate_usd'])} est"
    return ""


def fmt_disabled(e) -> list[tuple[str, int, str]]:
    p = e.payload
    return [(_LOGGER, logging.WARNING,
             f"  ⊘ gcs disabled: {p.get('reason', '?')}")]


def fmt_client_ready(e) -> list[tuple[str, int, str]]:
    p = e.payload
    return [(_LOGGER, logging.INFO,
             f"  → gcs ready ({p.get('credential_source', '?')}; "
             f"buckets={','.join(p.get('allowed_buckets', []))}; "
             f"escalation={p.get('escalation_level', '?')})")]


def fmt_list_completed(e) -> list[tuple[str, int, str]]:
    p = e.payload
    trunc = " (truncated)" if p.get("truncated") else ""
    return [(_LOGGER, logging.INFO,
             f"  ✓ gcs_list {p.get('prefix', '?')} → {p.get('returned', 0)} objects"
             f"{trunc}{_cost_segment(p)}")]


def fmt_stat_completed(e) -> list[tuple[str, int, str]]:
    p = e.payload
    return [(_LOGGER, logging.INFO,
             f"  ✓ gcs_stat {p.get('uri', '?')} "
             f"({human_bytes(int(p.get('size_bytes') or 0))} "
             f"{p.get('content_type', '?')}){_cost_segment(p)}")]


def fmt_upload_completed(e) -> list[tuple[str, int, str]]:
    p = e.payload
    tag = " [overwrote]" if p.get("was_overwrite") else ""
    return [(_LOGGER, logging.INFO,
             f"  ✓ gcs_upload → {p.get('uri', '?')} "
             f"({human_bytes(int(p.get('size_bytes') or 0))}){tag}{_cost_segment(p)}")]


def fmt_download_completed(e) -> list[tuple[str, int, str]]:
    p = e.payload
    tag = " [overwrote]" if p.get("was_overwrite") else ""
    return [(_LOGGER, logging.INFO,
             f"  ✓ gcs_download {p.get('uri', '?')} → "
             f"{p.get('local_path', '?')} "
             f"({human_bytes(int(p.get('size_bytes') or 0))}){tag}{_cost_segment(p)}")]


def fmt_delete_completed(e) -> list[tuple[str, int, str]]:
    p = e.payload
    return [(_LOGGER, logging.WARNING,
             f"  ✖ gcs_delete {p.get('uri', '?')} "
             f"({human_bytes(int(p.get('size_bytes') or 0))}){_cost_segment(p)}")]


def fmt_signed_url_issued(e) -> list[tuple[str, int, str]]:
    p = e.payload
    return [(_LOGGER, logging.INFO,
             f"  ✓ gcs_signed_url {p.get('uri', '?')} "
             f"(expires in {p.get('expires_in_minutes', '?')}m){_cost_segment(p)}")]


def fmt_read_text_completed(e) -> list[tuple[str, int, str]]:
    p = e.payload
    trunc = " (truncated)" if p.get("truncated") else ""
    return [(_LOGGER, logging.INFO,
             f"  ✓ gcs_read_text {p.get('uri', '?')} "
             f"→ {p.get('bytes_read', 0)} bytes{trunc}{_cost_segment(p)}")]


def fmt_recent_completed(e) -> list[tuple[str, int, str]]:
    p = e.payload
    return [(_LOGGER, logging.INFO,
             f"  ✓ gcs_recent {p.get('prefix', '?')} → "
             f"{p.get('returned', 0)} objects{_cost_segment(p)}")]


def fmt_summarize_bucket_completed(e) -> list[tuple[str, int, str]]:
    p = e.payload
    return [(_LOGGER, logging.INFO,
             f"  ✓ gcs_summarize_bucket {p.get('prefix', '?')} → "
             f"{p.get('n_objects', 0):,} objects, "
             f"{human_bytes(int(p.get('total_bytes') or 0))}{_cost_segment(p)}")]


def fmt_dirs_completed(e) -> list[tuple[str, int, str]]:
    p = e.payload
    return [(_LOGGER, logging.INFO,
             f"  ✓ gcs_dirs {p.get('prefix', '?')} → "
             f"{p.get('returned', 0)} directories{_cost_segment(p)}")]


def fmt_estimate_storage_cost_completed(e) -> list[tuple[str, int, str]]:
    p = e.payload
    return [(_LOGGER, logging.INFO,
             f"  ✓ gcs_estimate_storage_cost {p.get('prefix', '?')} → "
             f"{format_cost_usd(p.get('monthly_estimate_usd', 0))} / month "
             f"({p.get('region', '?')}/{p.get('storage_class', '?')})"
             f"{_cost_segment(p)}")]


def fmt_escalation_requested(e) -> list[tuple[str, int, str]]:
    p = e.payload
    return [(_LOGGER, logging.INFO,
             f"  ? gcs escalation requested: {p.get('operation', '?')} "
             f"on {p.get('uri', '?')}")]


def fmt_escalation_denied(e) -> list[tuple[str, int, str]]:
    p = e.payload
    return [(_LOGGER, logging.WARNING,
             f"  ⊘ gcs escalation denied: {p.get('operation', '?')} "
             f"on {p.get('uri', '?')}")]


def fmt_budget_exceeded(e) -> list[tuple[str, int, str]]:
    p = e.payload
    cap = p.get("cap", "?")
    return [(_LOGGER, logging.WARNING,
             f"  ⚠ gcs budget exceeded ({cap}): used {p.get('used', '?')} "
             f"of {p.get('ceiling', '?')}")]


# Dispatch table — arc's log_writer can register these against the
# matching event types. Format mirrors the one in arc's
# plugins/log_writer/formatter.py.
DISPATCH = {
    "gcs.disabled":                       fmt_disabled,
    "gcs.client_ready":                   fmt_client_ready,
    "gcs.list.completed":                 fmt_list_completed,
    "gcs.stat.completed":                 fmt_stat_completed,
    "gcs.upload.completed":               fmt_upload_completed,
    "gcs.download.completed":             fmt_download_completed,
    "gcs.delete.completed":               fmt_delete_completed,
    "gcs.signed_url.issued":              fmt_signed_url_issued,
    "gcs.read_text.completed":            fmt_read_text_completed,
    "gcs.recent.completed":               fmt_recent_completed,
    "gcs.summarize_bucket.completed":     fmt_summarize_bucket_completed,
    "gcs.dirs.completed":                 fmt_dirs_completed,
    "gcs.estimate_storage_cost.completed": fmt_estimate_storage_cost_completed,
    "gcs.escalation.requested":           fmt_escalation_requested,
    "gcs.escalation.denied":              fmt_escalation_denied,
    "gcs.budget_exceeded":                fmt_budget_exceeded,
}
