"""Tiered escalation policy for GCS tool calls.

Three levels, increasing strictness:

  destructive (default) — gcs_delete, overwrite-mode upload/download
  mutations             — above + new uploads + signed URL issuance
  all                   — above + every read

Each tool calls `should_escalate(operation)` to ask whether it needs
to invoke the UserGate before executing. Headless mode (NoOpGate)
will auto-deny whatever this returns True for.
"""
from __future__ import annotations

from typing import Literal

EscalationLevel = Literal["destructive", "mutations", "all"]

# Operation kinds — what each tool tells the policy.
Operation = Literal[
    # destructive
    "delete",
    "upload_overwrite",
    "download_overwrite",
    # mutations
    "upload_new",
    "download_new",   # writes a new file to the host — a mutation, not a read
    "signed_url",
    # reads
    "list",
    "stat",
    "read_text",
    "dirs",
    "recent",
    "summarize_bucket",
    "estimate_storage_cost",
]

_DESTRUCTIVE_OPS = frozenset({"delete", "upload_overwrite", "download_overwrite"})
_MUTATION_OPS = frozenset({"upload_new", "download_new", "signed_url"})
_READ_OPS = frozenset({"list", "stat", "read_text", "dirs", "recent",
                       "summarize_bucket", "estimate_storage_cost"})

ALL_OPS = _DESTRUCTIVE_OPS | _MUTATION_OPS | _READ_OPS


class InvalidEscalationLevel(ValueError):
    """Raised when the config has a typo'd escalation_level value."""


def validate_level(value: str) -> EscalationLevel:
    """Normalize + check the configured level. Raises on typo."""
    v = str(value).lower().strip()
    if v not in ("destructive", "mutations", "all"):
        raise InvalidEscalationLevel(
            f"invalid escalation_level={value!r}; "
            f"expected 'destructive', 'mutations', or 'all'"
        )
    return v  # type: ignore[return-value]


def should_escalate(operation: Operation, level: EscalationLevel) -> bool:
    """True if this operation needs UserGate confirmation at `level`."""
    if operation not in ALL_OPS:
        # Programmer error — every tool should pass a known op string.
        raise ValueError(f"unknown operation {operation!r}; known: {sorted(ALL_OPS)}")
    if level == "destructive":
        return operation in _DESTRUCTIVE_OPS
    if level == "mutations":
        return operation in _DESTRUCTIVE_OPS or operation in _MUTATION_OPS
    # level == "all"
    return True
