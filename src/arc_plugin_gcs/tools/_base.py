"""Internal helpers shared by GCS tool implementations.

Three concerns:
  - human-readable byte formatting
  - the gate-then-budget-then-execute decorator pattern
  - the SDK-error → ToolError mapper

Kept private (underscore prefix) — out-of-tree code should not import
from here.
"""
from __future__ import annotations

from typing import Any

try:
    from arc.plugin_api import ToolError
except ImportError:  # pragma: no cover — tests run without arc installed
    class ToolError(Exception):  # type: ignore[no-redef]
        pass

from arc_plugin_gcs.budget import SessionBudget
from arc_plugin_gcs.client import GCSClientError
from arc_plugin_gcs.escalation import Operation, should_escalate
from arc_plugin_gcs.tools import ToolContext


# ── Formatting ────────────────────────────────────────────────────────────


_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def human_bytes(n: int) -> str:
    """`1234567` → `1.2 MB`. Two significant digits."""
    if n <= 0:
        return "0 B"
    f = float(n)
    i = 0
    while f >= 1024 and i < len(_UNITS) - 1:
        f /= 1024
        i += 1
    if i == 0:
        return f"{int(f)} {_UNITS[i]}"
    if f >= 100:
        return f"{int(f)} {_UNITS[i]}"
    return f"{f:.1f} {_UNITS[i]}"


# ── Gate + budget orchestration ───────────────────────────────────────────


def gate_and_reserve(
    ctx: ToolContext,
    *,
    operation: Operation,
    uri: str,
    api_calls: int = 1,
    bytes_transferred: int = 0,
    cost_usd: float = 0.0,
) -> None:
    """Pre-flight: escalate via UserGate if the tier requires it, then
    check the session budget. Either may raise ToolError.

    Caller commits the budget after the SDK call returns successfully.
    """
    # Escalation.
    if should_escalate(operation, ctx.escalation_level):
        ctx.emit(
            "gcs.escalation.requested",
            payload={"operation": operation, "uri": uri},
        )
        allowed = _ask_gate(ctx.user_gate, operation=operation, uri=uri)
        if not allowed:
            ctx.emit(
                "gcs.escalation.denied",
                payload={"operation": operation, "uri": uri},
                severity="warn",
            )
            raise ToolError(
                f"GCS {operation} on {uri!r} denied by user gate"
            )

    # Budget.
    denial = ctx.budget.try_reserve(
        api_calls=api_calls,
        bytes_transferred=bytes_transferred,
        cost_usd=cost_usd,
    )
    if denial is not None:
        if ctx.budget.mark_denial_emitted(denial.cap):
            ctx.emit(
                "gcs.budget_exceeded",
                payload={
                    "cap": denial.cap,
                    "used": denial.used,
                    "ceiling": denial.ceiling,
                    "candidate": denial.candidate,
                },
                severity="warn",
            )
        raise ToolError(denial.message())


def _ask_gate(gate: Any, *, operation: str, uri: str) -> bool:
    """Adapter onto UserGate's confirm API. arc's UserGate exposes
    `confirm(prompt, *, scope_id) -> bool` (see arc.user_gate)."""
    if gate is None:
        # No gate at all — same effect as NoOpGate: deny.
        return False
    confirm = getattr(gate, "confirm", None)
    if not callable(confirm):
        # Unknown gate shape — fail closed.
        return False
    try:
        return bool(confirm(
            f"GCS {operation} on {uri}. Allow?",
            scope_id=f"gcs:{operation}:{uri}",
        ))
    except TypeError:
        # Older gate without scope_id kwarg — best-effort.
        try:
            return bool(confirm(f"GCS {operation} on {uri}. Allow?"))
        except Exception:  # noqa: BLE001
            return False
    except Exception:  # noqa: BLE001 — defensive
        return False


def map_sdk_error(exc: BaseException, *, uri: str | None = None) -> ToolError:
    """Convert a google-cloud-storage exception to a clear ToolError."""
    name = type(exc).__name__
    msg = str(exc)
    if name == "NotFound" or "404" in msg:
        if uri:
            return ToolError(f"no such object: {uri}")
        return ToolError(f"GCS NotFound: {msg}")
    if name == "Forbidden" or "403" in msg:
        if uri:
            return ToolError(
                f"permission denied on {uri}; check service account roles"
            )
        return ToolError(f"GCS Forbidden: {msg}")
    if name == "BadRequest" or "400" in msg:
        return ToolError(f"GCS bad request: {msg}")
    return ToolError(f"GCS {name}: {msg}")


def to_tool_error(exc: BaseException, *, uri: str | None = None) -> ToolError:
    """Public adapter — converts any storage/client error to ToolError."""
    if isinstance(exc, GCSClientError):
        return ToolError(str(exc))
    return map_sdk_error(exc, uri=uri)
