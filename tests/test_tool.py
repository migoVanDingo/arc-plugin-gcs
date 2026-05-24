from __future__ import annotations

import pytest

from arc.plugin_api import ToolError
from arc_plugin_example.plugin import build


@pytest.fixture
def started_plugin(build_ctx, session_ctx):
    plugin = build({"greeting": "hey", "max_shouts": 3}, build_ctx)
    plugin.on_session_start(session_ctx)
    return plugin


def test_tool_executes_and_returns_shout(started_plugin):
    tool = started_plugin.provides_tools()[0]
    out = tool.execute({"name": "Alice"})
    assert "Alice" in out
    assert "HEY" in out  # greeting is uppercased
    # The leading "(N)" is the per-session call counter
    assert out.startswith("(1)")


def test_tool_increments_session_counter(started_plugin):
    tool = started_plugin.provides_tools()[0]
    a = tool.execute({"name": "Alice"})
    b = tool.execute({"name": "Bob"})
    assert a.startswith("(1)") and b.startswith("(2)")


def test_tool_respects_shouts_param(started_plugin):
    tool = started_plugin.provides_tools()[0]
    out = tool.execute({"name": "Carol", "shouts": 3})
    # Three shouts → "Carol" appears three times
    assert out.count("Carol") == 3


def test_tool_rejects_empty_name(started_plugin):
    tool = started_plugin.provides_tools()[0]
    with pytest.raises(ToolError, match="non-empty"):
        tool.execute({"name": ""})


def test_tool_rejects_out_of_range_shouts(started_plugin):
    tool = started_plugin.provides_tools()[0]
    with pytest.raises(ToolError, match="must be between"):
        tool.execute({"name": "Dan", "shouts": 99})


def test_tool_emits_invoked_event(started_plugin, build_ctx):
    tool = started_plugin.provides_tools()[0]
    tool.execute({"name": "Eve", "shouts": 2})
    types = build_ctx.bus.types()
    assert "example_shout.invoked" in types


def test_input_schema_advertises_max_shouts(started_plugin):
    tool = started_plugin.provides_tools()[0]
    schema = tool.input_schema.to_json_schema()
    assert schema["properties"]["shouts"]["maximum"] == 3
    assert schema["required"] == ["name"]
