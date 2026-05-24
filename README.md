# arc-plugin-template

Template repo for building **out-of-tree arc plugins**: pip-installable
Python packages that arc auto-discovers via entry points and that contribute
hooks and/or tools to a session.

Use this template when you want to ship a plugin that's NOT part of arc's
main repo — because the integration is personal (a local DB), proprietary
(a private API), or just opinionated (a research backend you don't want to
foist on every arc user).

## What's in the box

```
arc-plugin-template/
├── pyproject.toml                  hatch build + arc.plugins entry point
├── src/arc_plugin_example/
│   ├── plugin.py                   ExamplePlugin + build() entry point
│   └── tools/example_tool.py       One tool implementing the Tool protocol
├── tests/
│   ├── conftest.py                 StubBus + StubBuildContext fixtures
│   ├── test_plugin.py              Plugin lifecycle, config, tool wiring
│   └── test_tool.py                Tool execution, validation, events
└── docs/PLUGIN_API.md              Pinned public-API surface
```

The example shows the **full** plugin pattern: a plugin that owns
session-scoped state and contributes tools bound to it. If your plugin only
ships stateless tools, strip out `on_session_start` and build the tools
directly in `build()`. See `docs/PLUGIN_API.md` for both shapes.

## Forking workflow

1. **Use the template on GitHub** (or clone + push to a fresh repo).

2. **Rename the package.** Pick a name like `arc-plugin-<thing>`:

   ```bash
   # Package directory
   git mv src/arc_plugin_example src/arc_plugin_<thing>

   # In pyproject.toml: name, packages, entry-point key + path
   #   name = "arc-plugin-<thing>"
   #   [project.entry-points."arc.plugins"]
   #   <thing> = "arc_plugin_<thing>.plugin:build"
   #   [tool.hatch.build.targets.wheel]
   #   packages = ["src/arc_plugin_<thing>"]
   ```

   Then update imports in `plugin.py`, `tools/`, and `tests/`.

3. **Replace the example.** Gut `ExamplePlugin` and `ExampleTool` and write
   your real plugin. Keep the structural patterns:
   - `build(config, build_ctx) -> Plugin` is the entry-point contract
   - `bind_bus(bus)` is optional but expected if you emit events
   - `on_session_start` / `on_session_end` own resource lifecycle
   - `provides_tools()` returns tools to merge into the registry
   - Tools raise `ToolError` for failures; never return error strings

4. **Run tests:**

   ```bash
   pip install -e ".[dev]"
   pytest
   ```

   With the `arc` source checked out next to your plugin, install it as
   editable too so `from arc.plugin_api import ...` resolves:

   ```bash
   pip install -e ../arc/v2  # or wherever your arc checkout lives
   ```

5. **Install into arc.** Once your plugin works in isolation, point arc at
   it:

   ```bash
   # In the arc checkout (or wherever you run arc from):
   pip install -e /path/to/arc-plugin-<thing>
   ```

   arc's entry-point loader will discover it on next start. Enable it in
   arc's config (`~/.arc/config.yml`):

   ```yaml
   plugins:
     enabled:
       - name: <thing>
         enabled: true
         config:
           # whatever your build(config, ...) reads
   ```

## Why a plugin, not just a script?

External plugins are the right shape when:

- The integration owns **session-scoped state** (open DB handle, loaded
  model, API session). Lifecycle hooks (`on_session_start` /
  `on_session_end`) give you the right place to acquire and release it.
- The integration **shouldn't ship with arc itself** — personal corpus,
  paid API, proprietary data.
- You want **graceful absence**: if the resource is missing, the plugin
  emits a `*.disabled` event and the session continues without it. Tools
  alone can't refuse to register themselves; plugins can.

If you just want to add a stateless tool to arc's built-in set, consider
upstreaming it instead.

## Compatibility

This template targets the arc plugin API at version **0.1** (`arc.plugin_api`
shim, entry-point discovery via `arc.plugins`, `provides_tools()`, optional
`bind_bus(bus)` on tools). See `docs/PLUGIN_API.md` for the pinned surface
and breakage policy.

## License

MIT — see `LICENSE`. Forks may relicense.
