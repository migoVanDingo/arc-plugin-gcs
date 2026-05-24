"""ExampleTool — a minimal Tool implementing the arc Tool protocol.

Tools are dumb on purpose. They:
  - declare a JSON-Schema-shaped `input_schema`
  - implement `execute(input: dict) -> str`
  - raise `ToolError` on failure (don't return error strings)

Policy (rate-limiting, escalation, paging, redaction) lives in plugin hooks,
NOT in tools. See arc's `_architecture/tool-authoring.md` upstream.

This example demonstrates:
  - reading from config via plugin construction (not via os.environ)
  - holding a back-reference to the plugin for session-scoped state
  - emitting a structured event via the optional `bind_bus` pattern
  - raising ToolError with a model-actionable message
"""
from __future__ import annotations

from typing import Any, ClassVar

from arc.plugin_api import RuntimeEvent, ToolError, ToolInputSchema


class ExampleTool:
    name: ClassVar[str] = "example_shout"
    description: ClassVar[str] = (
        "Greet someone enthusiastically. Returns the greeting "
        "shouted N times. Demonstrates the arc plugin tool contract."
    )

    def __init__(self, *, greeting: str, max_shouts: int, state: Any) -> None:
        self._greeting = greeting
        self._max_shouts = max_shouts
        self._state = state  # the ExamplePlugin instance, for per-session counter
        self._bus: Any = None

    def bind_bus(self, bus: Any) -> None:
        """Optional. Tools that need to emit structured events implement this.
        arc's tool registry calls bind_bus on tools that define it.
        """
        self._bus = bus

    @property
    def input_schema(self) -> ToolInputSchema:
        return ToolInputSchema(
            properties={
                "name": {
                    "type": "string",
                    "description": "Who to greet.",
                },
                "shouts": {
                    "type": "integer",
                    "description": f"How many times to shout (1–{self._max_shouts}).",
                    "minimum": 1,
                    "maximum": self._max_shouts,
                    "default": 1,
                },
            },
            required=["name"],
        )

    def execute(self, input: dict[str, Any]) -> str:
        name = str(input.get("name", "")).strip()
        if not name:
            # ToolError surfaces to the model as a failed tool call. The model
            # sees the message and can adapt (e.g. retry with a name).
            raise ToolError("`name` is required and must be non-empty")

        shouts = int(input.get("shouts", 1))
        if shouts < 1 or shouts > self._max_shouts:
            raise ToolError(
                f"`shouts` must be between 1 and {self._max_shouts}, got {shouts}"
            )

        call_n = self._state.tick_call_count()
        body = " ".join(f"{self._greeting.upper()}, {name}!" for _ in range(shouts))

        if self._bus is not None:
            self._bus.emit(RuntimeEvent(
                type="example_shout.invoked",
                payload={"name": name, "shouts": shouts, "call_n": call_n},
                stage="tool",
            ))

        return f"({call_n}) {body}"
