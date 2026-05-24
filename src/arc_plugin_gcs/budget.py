"""Session-scoped budget enforcement for the GCS plugin.

One SessionBudget per session. Tracks running totals of API calls,
bytes transferred, and estimated cost. On `try_reserve()` the budget
checks whether a candidate operation would exceed any cap; on
`commit()` it applies the actual usage (so failed API calls don't
consume slots).

Mirrors the per-spec quota pattern from 0020 — budgets are about cost
ceilings, separate from per-call user escalation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

CapName = Literal["api_calls", "bytes_transferred", "cost_usd"]


@dataclass(frozen=True)
class BudgetCaps:
    """User-configured ceilings. None means "no cap for this dimension"."""
    max_api_calls: int | None = None
    max_bytes_transferred: int | None = None
    max_cost_usd: float | None = None


@dataclass(frozen=True)
class BudgetDenial:
    """Returned by try_reserve when a cap would be exceeded."""
    cap: CapName
    used: float                # current usage on that axis
    ceiling: float             # configured ceiling
    candidate: float           # what this attempt would have added

    def message(self) -> str:
        unit = {
            "api_calls": "calls",
            "bytes_transferred": "bytes",
            "cost_usd": "$",
        }[self.cap]
        if self.cap == "cost_usd":
            return (
                f"session GCS budget exceeded: cost cap reached "
                f"(used ${self.used:.4f} of ${self.ceiling:.2f}, "
                f"this call adds ${self.candidate:.4f})"
            )
        return (
            f"session GCS budget exceeded: {self.cap} cap reached "
            f"(used {int(self.used)} {unit} of {int(self.ceiling)})"
        )


class SessionBudget:
    """In-memory budget tracker. One instance per arc session."""

    def __init__(self, caps: BudgetCaps) -> None:
        self._caps = caps
        self._api_calls = 0
        self._bytes_transferred = 0
        self._cost_usd = 0.0
        # Per-cap "have we emitted the budget_exceeded event yet?" so
        # subsequent denials of the same cap don't spam events.
        self._denial_emitted: set[CapName] = set()

    @property
    def api_calls_used(self) -> int:
        return self._api_calls

    @property
    def bytes_transferred(self) -> int:
        return self._bytes_transferred

    @property
    def cost_usd_used(self) -> float:
        return self._cost_usd

    def try_reserve(
        self,
        *,
        api_calls: int = 1,
        bytes_transferred: int = 0,
        cost_usd: float = 0.0,
    ) -> BudgetDenial | None:
        """Check whether this operation would exceed any cap.

        Returns None if allowed. Returns a BudgetDenial naming the cap
        otherwise. The candidate is NOT committed — caller must call
        `commit()` after the operation succeeds.
        """
        if self._caps.max_api_calls is not None:
            if self._api_calls + api_calls > self._caps.max_api_calls:
                return BudgetDenial(
                    cap="api_calls",
                    used=float(self._api_calls),
                    ceiling=float(self._caps.max_api_calls),
                    candidate=float(api_calls),
                )
        if self._caps.max_bytes_transferred is not None:
            if self._bytes_transferred + bytes_transferred > self._caps.max_bytes_transferred:
                return BudgetDenial(
                    cap="bytes_transferred",
                    used=float(self._bytes_transferred),
                    ceiling=float(self._caps.max_bytes_transferred),
                    candidate=float(bytes_transferred),
                )
        if self._caps.max_cost_usd is not None:
            if self._cost_usd + cost_usd > self._caps.max_cost_usd:
                return BudgetDenial(
                    cap="cost_usd",
                    used=self._cost_usd,
                    ceiling=self._caps.max_cost_usd,
                    candidate=cost_usd,
                )
        return None

    def commit(
        self,
        *,
        api_calls: int = 1,
        bytes_transferred: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        """Apply usage after a successful operation. Failed API calls
        should NOT commit — counters reflect actual consumption."""
        self._api_calls += api_calls
        self._bytes_transferred += bytes_transferred
        self._cost_usd += cost_usd

    def mark_denial_emitted(self, cap: CapName) -> bool:
        """Return True if this is the first denial of `cap` in the
        session (so the caller should emit the event). Idempotent for
        subsequent denials of the same cap."""
        if cap in self._denial_emitted:
            return False
        self._denial_emitted.add(cap)
        return True
