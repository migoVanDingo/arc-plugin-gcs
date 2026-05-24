from __future__ import annotations

from arc_plugin_example.plugin import ExamplePlugin, build


def test_build_constructs_plugin_from_config(build_ctx):
    plugin = build({"greeting": "howdy", "max_shouts": 5}, build_ctx)
    assert isinstance(plugin, ExamplePlugin)
    assert plugin.name == "example"
    # bus binding happens in build()
    assert plugin._bus is build_ctx.bus


def test_build_applies_defaults(build_ctx):
    plugin = build({}, build_ctx)
    assert plugin._greeting == "hello"
    assert plugin._max_shouts == 3


def test_on_session_start_emits_ready_and_provides_tools(build_ctx, session_ctx):
    plugin = build({"greeting": "hi", "max_shouts": 2}, build_ctx)
    plugin.on_session_start(session_ctx)

    assert "example.ready" in build_ctx.bus.types()
    tools = plugin.provides_tools()
    assert len(tools) == 1
    assert tools[0].name == "example_shout"


def test_on_session_end_drops_tools(build_ctx, session_ctx):
    plugin = build({}, build_ctx)
    plugin.on_session_start(session_ctx)
    assert plugin.provides_tools()
    plugin.on_session_end(session_ctx, outcome=None)
    assert plugin.provides_tools() == []


def test_tick_call_count_is_per_session(build_ctx, session_ctx):
    plugin = build({}, build_ctx)
    plugin.on_session_start(session_ctx)
    assert plugin.tick_call_count() == 1
    assert plugin.tick_call_count() == 2
    assert plugin.tick_call_count() == 3
