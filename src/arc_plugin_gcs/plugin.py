"""ExamplePlugin — demonstrates the full plugin shape.

This single file shows every pattern an external plugin can use:

  1. A *builder* function (`build`) — what arc's entry-point loader calls.
  2. Lifecycle hooks (`on_session_start`, `on_session_end`) — for opening and
     closing session-scoped resources (DB handles, model loads, caches).
  3. `provides_tools()` — contributes one or more tools that are bound to the
     plugin's session-scoped state.
  4. `bind_bus(bus)` — receive the event bus so the plugin (or its tools) can
     emit structured events for observability.
  5. Graceful failure — if construction can't succeed (missing config,
     unreachable resource), emit a `*.disabled` event and return no tools.
     Don't raise; arc's plugin quarantine handles that path but it's noisier
     than a clean opt-out.

If your plugin contributes only stateless tools (no DB handle, no session
state), you can drop `on_session_start` / `on_session_end` and build the
tools directly in `build()`. See PLUGIN_API.md §"Stateless tool packages".
"""
from __future__ import annotations

from typing import Any

# Public arc plugin API — the one stable import path. See PLUGIN_API.md.
from arc.plugin_api import (
    PluginBuildContext,
    SessionContext,
    Tool,
    TurnOutcome,
)

from arc_plugin_example.tools.example_tool import ExampleTool


class ExamplePlugin:
    """Owns session-scoped state and contributes tools bound to it.

    The plugin instance lives for one session. arc constructs it via `build()`,
    fires `on_session_start` once, then asks `provides_tools()` for tools to
    merge into the registry. Tools see the plugin's state through closure or
    constructor injection (this template uses constructor injection).
    """

    # The name arc uses in events, logs, and config. Must match the entry-point
    # key in pyproject.toml.
    name = "example"

    def __init__(self, *, greeting: str, max_shouts: int) -> None:
        self._greeting = greeting
        self._max_shouts = max_shouts
        self._bus: Any = None
        self._tools: list[Tool] = []
        # Example session-scoped state: a per-session call counter that the
        # tool reads + increments. In briefbot this would be a sqlite3.Connection.
        self._call_count = 0

    # ── Bus binding ────────────────────────────────────────────────────────
    # Optional. If present, arc calls bind_bus before any hook fires. Store
    # the bus on self and pass it down to tools that want to emit events.

    def bind_bus(self, bus: Any) -> None:
        self._bus = bus

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def on_session_start(self, ctx: SessionContext) -> None:
        # In a real plugin: open the DB, load the model, fetch the API token.
        # If that fails, emit a `*.disabled` event and leave self._tools = [].
        try:
            tool = ExampleTool(
                greeting=self._greeting,
                max_shouts=self._max_shouts,
                state=self,  # tool calls self.tick_call_count() on each invoke
            )
            if self._bus is not None:
                tool.bind_bus(self._bus)
            self._tools = [tool]
            self._emit("example.ready", {"session_id": ctx.session_id,
                                         "greeting": self._greeting,
                                         "tools": [t.name for t in self._tools]})
        except Exception as exc:  # noqa: BLE001 — graceful opt-out is the point
            self._tools = []
            self._emit("example.disabled", {"reason": str(exc)})

    def on_session_end(self, ctx: SessionContext, outcome: TurnOutcome | None) -> None:
        # In a real plugin: close DB connections, flush caches, release file
        # locks. Idempotent — arc may call this even if on_session_start
        # short-circuited.
        self._tools = []

    # ── Tool contribution ──────────────────────────────────────────────────

    def provides_tools(self) -> list[Tool]:
        """Return tools this plugin contributes for this session.

        Called by arc after on_session_start. Returning [] is normal when the
        plugin gracefully disabled itself (missing resource, bad config).
        """
        return list(self._tools)

    # ── Internal ───────────────────────────────────────────────────────────

    def tick_call_count(self) -> int:
        """Tool calls this when invoked. Demonstrates per-session state."""
        self._call_count += 1
        return self._call_count

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        """Emit a structured event if the bus is bound."""
        if self._bus is None:
            return
        from arc.plugin_api import RuntimeEvent
        self._bus.emit(RuntimeEvent(type=event_type, payload=payload, stage="plugin"))


# ── Entry point ────────────────────────────────────────────────────────────
# Referenced from pyproject.toml:
#   [project.entry-points."arc.plugins"]
#   example = "arc_plugin_example.plugin:build"
#
# `config` is the plugin's config dict from arc's config.yml under
# `plugins.enabled[*].config` for this plugin.
# `build_ctx` is a PluginBuildContext (bus, sessions_dir, session_id, user_gate).

def build(config: dict, build_ctx: PluginBuildContext) -> ExamplePlugin:
    plugin = ExamplePlugin(
        greeting=str(config.get("greeting", "hello")),
        max_shouts=int(config.get("max_shouts", 3)),
    )
    if build_ctx.bus is not None:
        plugin.bind_bus(build_ctx.bus)
    return plugin
