"""GCS rate table + per-operation cost calculation.

All rates are USD. Updated when GCS pricing changes (infrequent).
Source: https://cloud.google.com/storage/pricing (as of 2026-05).

The plugin treats these as estimates — users on contract pricing or
free-tier will see different actual bills. For real cost, use the
Cloud Billing API (deferred — see 0021 §Out).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


# ── Operations ────────────────────────────────────────────────────────────
# Class A: writes, lists, copies, signed-URL issuance.
# Class B: reads, stat.

CLASS_A_USD_PER_CALL: float = 5e-7    # $0.005 / 10,000
CLASS_B_USD_PER_CALL: float = 4e-8    # $0.0004 / 10,000

# ── Egress ────────────────────────────────────────────────────────────────
# Egress to internet (US/EU, multi-region) — covers downloads + read_text.
# Uploads to GCS are free per current pricing.

EGRESS_USD_PER_GB: float = 0.12


# ── Storage ───────────────────────────────────────────────────────────────
# Per-GB-month rates for gcs_estimate_storage_cost. Indexed by
# (region, storage_class). Add new entries here when needed.

Region = Literal["us-multi", "us-region", "eu-multi", "eu-region", "asia-multi", "asia-region"]
StorageClass = Literal["STANDARD", "NEARLINE", "COLDLINE", "ARCHIVE"]

STORAGE_RATE_USD_PER_GB_MONTH: dict[tuple[str, str], float] = {
    ("us-multi",   "STANDARD"): 0.026,
    ("us-region",  "STANDARD"): 0.020,
    ("us-multi",   "NEARLINE"): 0.010,
    ("us-region",  "NEARLINE"): 0.010,
    ("us-multi",   "COLDLINE"): 0.004,
    ("us-region",  "COLDLINE"): 0.004,
    ("us-multi",   "ARCHIVE"):  0.0012,
    ("us-region",  "ARCHIVE"):  0.0012,
    ("eu-multi",   "STANDARD"): 0.026,
    ("eu-region",  "STANDARD"): 0.020,
    ("eu-multi",   "NEARLINE"): 0.010,
    ("eu-multi",   "COLDLINE"): 0.004,
    ("eu-multi",   "ARCHIVE"):  0.0012,
    ("asia-multi", "STANDARD"): 0.026,
    ("asia-region","STANDARD"): 0.023,
    ("asia-multi", "NEARLINE"): 0.010,
    ("asia-multi", "COLDLINE"): 0.006,
    ("asia-multi", "ARCHIVE"):  0.0015,
}


# ── Per-call cost helpers ─────────────────────────────────────────────────


@dataclass(frozen=True)
class CallCost:
    """Result of a per-operation cost computation. Plain data — emitted
    in events as `cost_estimate_usd` and `bytes_transferred`."""
    cost_usd: float
    bytes_transferred: int


def list_cost() -> CallCost:
    """gcs_list / gcs_dirs / gcs_recent / gcs_summarize_bucket /
    gcs_estimate_storage_cost — one Class A op (pagination handled by
    the SDK counts as one logical op for budgeting)."""
    return CallCost(cost_usd=CLASS_A_USD_PER_CALL, bytes_transferred=0)


def stat_cost() -> CallCost:
    """gcs_stat — one Class B op, no bytes."""
    return CallCost(cost_usd=CLASS_B_USD_PER_CALL, bytes_transferred=0)


def upload_cost(size_bytes: int) -> CallCost:
    """gcs_upload — one Class A op. Upload egress is free per current
    pricing; only the API call cost counts."""
    return CallCost(cost_usd=CLASS_A_USD_PER_CALL, bytes_transferred=size_bytes)


def download_cost(size_bytes: int) -> CallCost:
    """gcs_download — one Class B op + egress charged per GB."""
    egress = (size_bytes / (1024 ** 3)) * EGRESS_USD_PER_GB
    return CallCost(
        cost_usd=CLASS_B_USD_PER_CALL + egress,
        bytes_transferred=size_bytes,
    )


def delete_cost() -> CallCost:
    """gcs_delete — one Class A op."""
    return CallCost(cost_usd=CLASS_A_USD_PER_CALL, bytes_transferred=0)


def signed_url_cost() -> CallCost:
    """gcs_signed_url — one Class A op for issuance."""
    return CallCost(cost_usd=CLASS_A_USD_PER_CALL, bytes_transferred=0)


def read_text_cost(bytes_read: int) -> CallCost:
    """gcs_read_text — one Class B op + egress per GB read."""
    egress = (bytes_read / (1024 ** 3)) * EGRESS_USD_PER_GB
    return CallCost(
        cost_usd=CLASS_B_USD_PER_CALL + egress,
        bytes_transferred=bytes_read,
    )


# ── Storage cost lookup ───────────────────────────────────────────────────


class UnknownRateError(ValueError):
    """Raised when (region, storage_class) isn't in the rate table."""


def monthly_storage_cost_usd(
    *,
    total_bytes: int,
    region: str,
    storage_class: str,
) -> float:
    """Estimated monthly storage cost for `total_bytes` at the given
    region+class. Returns USD as a float.

    Raises UnknownRateError if the (region, storage_class) pair isn't
    in the table — caller should map to a clear ToolError naming the
    known pairs.
    """
    key = (region, storage_class)
    rate = STORAGE_RATE_USD_PER_GB_MONTH.get(key)
    if rate is None:
        raise UnknownRateError(
            f"no rate for region={region!r} storage_class={storage_class!r}; "
            f"known pairs: {sorted(STORAGE_RATE_USD_PER_GB_MONTH.keys())}"
        )
    gb = total_bytes / (1024 ** 3)
    return gb * rate


# ── Formatting ────────────────────────────────────────────────────────────


def format_cost_usd(cost: float) -> str:
    """Human-readable USD for the TUI/log. Sub-cent values render as
    `<$0.0001` rather than a string of zeros."""
    if cost < 0.0001:
        return "<$0.0001"
    return f"${cost:.4f}"
