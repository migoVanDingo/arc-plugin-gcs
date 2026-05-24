# arc plugin API — public surface

This is the **only** stable import path external plugins may depend on:

```python
from arc.plugin_api import (
    # Tool surface
    Tool,
    ToolInputSchema,
    ToolError,

    # Plugin surface
    PluginBuildContext,
    SessionContext,
    TurnContext,
    TurnOutcome,
    UserInput,

    # Hook payloads (only if your plugin implements those hooks)
    Message,
    ToolSpec,
    ToolCall,
    ToolResult,
    ToolDenial,
    LLMRequest,
    LLMResponse,
    ContentBlock,
    Step,
    StepAssessment,

    # Events
    RuntimeEvent,
    EventType,
    Severity,

    # Pause/cancel control
    PauseRequested,
    Cancelled,
    PASS_THROUGH,
)
```

Importing from anywhere else (`arc.tools.base`, `arc.runtime.hooks`, etc.) is
**unsupported** — those modules can be refactored without notice.
`arc.plugin_api` is a thin re-export shim arc maintains specifically so
plugin authors have one path that won't move under them.

## API version

The shim exposes `arc.plugin_api.__api_version__` as a `(major, minor)`
tuple. **0.1** at time of writing.

- **Patch / minor bump (0.1 → 0.2):** additive — new symbols, new optional
  hook methods. Existing plugins keep working unchanged.
- **Major bump (0.x → 1.x):** breaking. Renames, signature changes,
  removed symbols. arc will emit a deprecation warning for at least one
  release before the bump.

If your plugin needs a specific minimum API version, assert it:

```python
from arc.plugin_api import __api_version__
assert __api_version__ >= (0, 1), "needs arc plugin API ≥ 0.1"
```

## Plugin shapes

arc supports two shapes via the same entry-point contract. Both register
under `[project.entry-points."arc.plugins"]`; arc figures out which shape
by inspecting the returned object.

### Shape A — session-scoped (the example template)

Plugin owns state that's expensive or stateful (DB connection, loaded model,
API client). Tools are bound to that state via the plugin.

```python
class MyPlugin:
    name = "my_plugin"

    def __init__(self, *, foo): ...
    def bind_bus(self, bus): ...                              # optional
    def on_session_start(self, ctx: SessionContext): ...      # acquire
    def on_session_end(self, ctx, outcome): ...               # release
    def provides_tools(self) -> list[Tool]: ...               # contribute

def build(config: dict, build_ctx: PluginBuildContext) -> MyPlugin:
    return MyPlugin(foo=config["foo"])
```

### Shape B — stateless tool pack

No session-scoped state. Just a bundle of tools that arc registers at boot.
Skip the lifecycle hooks; build the tools directly.

```python
class MyToolPack:
    name = "my_tools"

    def __init__(self, tools): self._tools = tools
    def provides_tools(self): return self._tools

def build(config: dict, build_ctx: PluginBuildContext) -> MyToolPack:
    return MyToolPack(tools=[
        ToolA.from_config(config.get("tool_a", {})),
        ToolB.from_config(config.get("tool_b", {})),
    ])
```

## Hook protocols

A plugin "implements a hook" by defining a method with the right name and
signature. arc's registry duck-types — there's no base class to inherit.
Implement zero, one, or many.

| Method | Fires | Return |
|---|---|---|
| `on_session_start(ctx)` | session boot | None |
| `on_session_end(ctx, outcome)` | session exit | None |
| `on_turn_start(ctx, user_input)` | user turn begins | `UserInput \| None` |
| `on_turn_end(ctx, outcome)` | turn finishes | None |
| `before_llm_call(ctx, req)` | before provider call | `LLMRequest \| None` |
| `after_llm_call(ctx, req, resp)` | after provider call | `LLMResponse \| None` |
| `before_tool_call(ctx, call)` | before tool exec | `ToolCall \| ToolDenial \| None` |
| `after_tool_call(ctx, call, result)` | after tool exec | `ToolResult \| None` |
| `pack_context(ctx, messages, query)` | building LLM messages | `list[Message] \| None` |
| `assess_step(ctx, step, result)` | step boundary | `StepAssessment \| None` |
| `on_event(ctx, event)` | every emitted event | None |
| `pause_check(ctx)` | cooperative yield | None (raise to pause/cancel) |

Returning `None` (or `PASS_THROUGH`) means "no change". Returning a
transformed value mutates the chain for downstream plugins.

## Tool protocol

Tools are structurally typed — implement the four members and you're a Tool.

```python
class MyTool:
    name: ClassVar[str] = "my_tool"
    description: ClassVar[str] = "Does the thing. Use it like X."

    @property
    def input_schema(self) -> ToolInputSchema: ...

    def execute(self, input: dict) -> str: ...

    def bind_bus(self, bus) -> None: ...  # OPTIONAL — only if you emit events
```

Tools that need configuration accept it via the plugin's `build()`, not via
`os.environ` or filesystem reads. Read config once, pass it in.

`ToolError` is the only way to signal a failed call — don't return
`"Error: ..."` strings. arc converts `ToolError` to a `ToolResult(ok=False)`
that the model sees and can react to.

## Event emission

Plugins and tools that want to emit structured events for observability
implement `bind_bus(bus)`. arc passes the bus during construction. Emit:

```python
from arc.plugin_api import RuntimeEvent

self._bus.emit(RuntimeEvent(
    type="myplugin.something_happened",   # any string; dotted convention
    payload={"k": "small searchable value"},
    content={"raw": "large or sensitive blob"},
    stage="plugin",                       # or "tool", "core"
))
```

Event types your plugin invents should be prefixed with the plugin name to
avoid colliding with arc's catalog (`EventType.*` for the built-in set).

## Quarantine policy

If your plugin raises during a hook, arc:

1. Logs the failure as a `plugin.hook.failed` event.
2. Treats this as one strike against the plugin.
3. Quarantines (disables) the plugin after `plugins.failure_threshold`
   strikes (default 3).

**Don't catch exceptions defensively** — let arc handle quarantine. The one
exception: if your plugin can gracefully degrade (e.g., the corpus file is
missing), catch at the *boundary* (`on_session_start`), emit a
`<plugin>.disabled` event, and return `[]` from `provides_tools()`. That's a
clean opt-out, not a failure.

## Tool collision

If two plugins both contribute a tool with the same name, arc raises at
startup. Choose names that namespace clearly (`briefbot_search`, not
`search`).

A plugin tool that collides with a built-in tool also raises — built-ins
don't silently win. Rename your tool or upstream it instead.
