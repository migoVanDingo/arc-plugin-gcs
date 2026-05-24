"""GCS tool implementations.

Tools are grouped by concern:
  file_ops.py    — list, stat, upload, download, delete
  sharing.py     — signed_url, read_text
  overview.py    — recent, summarize_bucket, dirs, estimate_storage_cost

All tools share a ToolContext that carries the client wrapper, the
session budget, the escalation policy, the UserGate, and the bus.
The plugin constructs one ToolContext per session and threads it
into each tool.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from arc_plugin_gcs.budget import SessionBudget
from arc_plugin_gcs.client import GCSClient
from arc_plugin_gcs.escalation import EscalationLevel


@dataclass
class ToolContext:
    """Shared state every GCS tool needs at execute time."""
    client: GCSClient
    budget: SessionBudget
    escalation_level: EscalationLevel
    user_gate: Any                       # UserGate or NoOpGate
    max_text_read_bytes: int = 1_048_576
    max_list_results: int = 1000
    max_signed_url_minutes: int = 1440
    # Bus is bound after construction via bind_bus(); held here once set.
    bus: Any = None

    def emit(self, event_type: str, *, payload: dict, severity: str = "info") -> None:
        """Emit a gcs.* event if the bus is bound. Otherwise no-op
        (e.g., in unit tests that don't set up a bus)."""
        if self.bus is None:
            return
        try:
            from arc.plugin_api import RuntimeEvent
        except ImportError:
            return
        self.bus.emit(RuntimeEvent(
            type=event_type,
            stage="plugin",
            severity=severity,
            payload=payload,
        ))
