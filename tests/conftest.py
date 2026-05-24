"""Test fixtures.

Plugins should be testable WITHOUT importing arc. The fixtures here provide
the minimum shape of `arc.plugin_api` that a unit test needs:

  - StubBus: records events that the plugin/tool emits
  - SessionContext-like, PluginBuildContext-like: plain objects with the right
    attribute shape, no real arc dependency

If you've checked out arc next to your plugin (`pip install -e ../arc`), you
can also import the real classes from `arc.plugin_api`. These stubs let you
test in CI without that.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest


# ── Stub bus ──────────────────────────────────────────────────────────────
# The real arc EventBus has many concerns (dispatch, ordering, failure
# isolation). For unit tests we only need to capture what was emitted.

class StubBus:
    def __init__(self) -> None:
        self.emitted: list[Any] = []

    def emit(self, event: Any) -> None:
        self.emitted.append(event)

    def types(self) -> list[str]:
        return [getattr(e, "type", "?") for e in self.emitted]


# ── Stub context objects ──────────────────────────────────────────────────
# Mirror the attribute shape arc passes. Frozen dataclasses keep parity with
# the real types in arc.plugin_api.

@dataclass(frozen=True)
class StubSessionContext:
    session_id: str = "test-session-01"
    workspace: str = "/tmp/workspace"
    provider_name: str = "anthropic"
    provider_model: str = "claude-sonnet-4-6"
    started_at: str = "2026-01-01T00:00:00Z"


@dataclass(frozen=True)
class StubBuildContext:
    sessions_dir: Path = field(default_factory=lambda: Path("/tmp/sessions"))
    session_id: str = "test-session-01"
    config_snapshot_yaml: str | None = None
    user_gate: Any = None
    bus: Any = None


@pytest.fixture
def bus() -> StubBus:
    return StubBus()


@pytest.fixture
def session_ctx() -> StubSessionContext:
    return StubSessionContext()


@pytest.fixture
def build_ctx(bus: StubBus) -> StubBuildContext:
    return StubBuildContext(bus=bus)
