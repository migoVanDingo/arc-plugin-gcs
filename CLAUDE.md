# arc-plugin-gcs — developer guide

External arc plugin that ships 10 Google Cloud Storage tools, escalation
tiers, session-scoped budgets, and per-call cost estimates. Designed
against `arc.plugin_api` v0.1.

| | |
|---|---|
| Source | ~1500 lines Python |
| Tests | 96 passing (offline; integration test is opt-in) |
| Runtime dep | `google-cloud-storage` |
| Plugin shape | Shape A — session-scoped (owns `storage.Client` + budget + escalation) |

## Read first

- **`README.md`** — user-facing: install, auth, config, tool reference.
- **`v2/_design/0021-gcs-plugin.md`** (in the arc v2 tree) — the design
  doc this plugin implements. Authoritative for every decision.

## Code map

```
src/arc_plugin_gcs/
  plugin.py              GCSPlugin (session-scoped) + build() entry point
  client.py              GCSClient — wraps storage.Client; URI parsing; allowlist
  auth.py                SA-JSON-or-ADC credential resolution
  rates.py               Per-op cost calculation + storage rate table
  budget.py              SessionBudget — quota / bytes / cost caps
  escalation.py          Tiered policy (destructive | mutations | all)
  formatters.py          Log-line formatters for gcs.* events
  tools/
    __init__.py          ToolContext (shared per-tool state)
    _base.py             gate_and_reserve + map_sdk_error + human_bytes
    file_ops.py          list, stat, upload, download, delete
    sharing.py           signed_url, read_text
    overview.py          recent, summarize_bucket, dirs, estimate_storage_cost
tests/
  conftest.py            FakeStorageClient + stub bus/gate fixtures
  test_*.py              one file per concern; ~96 tests
```

## The 9 modules + what they do

1. **`plugin.py`** — entry point. `build(config, build_ctx) -> GCSPlugin`.
   Validates config; constructs plugin; on_session_start resolves auth,
   builds 11 tool instances (10 user-facing + the bus binding); plugin
   contributes them via `provides_tools()`.

2. **`client.py`** — `GCSClient` wraps `google.cloud.storage.Client`.
   Owns URI parsing, default-bucket resolution, allowlist enforcement.
   Tools never touch the SDK directly — always via `client.blob(parsed)`
   or `client.sdk.list_blobs(...)` after `client.parse_uri()`.

3. **`auth.py`** — `resolve_auth()` returns `AuthResolution(ok, source,
   reason, client_factory)`. Pure function; no network. If `ok=False`
   the plugin disables itself.

4. **`rates.py`** — constants for Class A/B/egress costs and a storage
   rate table keyed by (region, storage_class). Per-op helpers
   (`list_cost()`, `download_cost(size)`, etc.) return `CallCost`
   objects (`cost_usd`, `bytes_transferred`).

5. **`budget.py`** — `SessionBudget` tracks running totals. `try_reserve()`
   is pre-flight (returns `BudgetDenial | None`); `commit()` applies usage
   AFTER a successful API call. Failed calls don't consume budget.

6. **`escalation.py`** — `validate_level(str) -> EscalationLevel` and
   `should_escalate(operation, level) -> bool`. Pure policy logic;
   no I/O.

7. **`formatters.py`** — `(logger_name, level, message)` triples for
   each `gcs.*` event type. arc's log_writer can use these to render
   `session.log` lines.

8. **`tools/__init__.py`** — `ToolContext` is the per-tool shared
   state: client + budget + escalation_level + user_gate + bus.
   Plugin constructs ONE per session, threads into every tool.

9. **`tools/_base.py`** — `gate_and_reserve(ctx, ...)` is the
   workhorse: escalates if needed, checks budget, raises ToolError
   on any denial. Every tool's `execute()` calls it before the SDK.
   Also exports `human_bytes` and `to_tool_error`.

## Per-tool template

Every tool follows the same shape — once you understand one, all 10
fall into place:

```python
class GCSWhatever:
    name: ClassVar[str] = "gcs_whatever"
    description: ClassVar[str] = "..."

    def __init__(self, ctx: ToolContext) -> None:
        self._ctx = ctx

    @property
    def input_schema(self) -> ToolInputSchema:
        return ToolInputSchema(properties=..., required=...)

    def execute(self, input: dict) -> str:
        # 1. Validate + parse user input
        # 2. parse_uri → ParsedURI (raises GCSClientError on bad input)
        # 3. compute cost via rates.<op>_cost(...)
        # 4. gate_and_reserve(...) — escalates + checks budget
        # 5. Make the SDK call inside try/except → to_tool_error
        # 6. budget.commit(...) on success
        # 7. emit gcs.<op>.completed event with cost in payload
        # 8. Return human-readable string
```

The order matters — gate THEN budget, both pre-flight, BEFORE the API
call. Commit AFTER the call returns. Don't skip the cost-in-event-payload
step; the TUI render and offline cost analysis depend on it.

## Two contracts authors must honor

1. **URL bodies NEVER in event payloads.** Signed URLs are credentials.
   `formatters.py` and the `signed_url.issued` event ONLY carry the URI
   + expires_in. Tests assert this in `test_tools_sharing.py`.

2. **Cost in events, NOT in tool output strings.** The agent's LLM
   doesn't need per-call cost in its context — it's a TUI/log/budget
   concern. Only `gcs_estimate_storage_cost` puts a number in the
   tool output (because that's literally what the tool computes).

## Testing without real GCS

The unit suite uses `FakeStorageClient` (in `tests/conftest.py`) — a
hand-rolled fake that mimics `google.cloud.storage.Client.list_blobs`,
`bucket().blob()`, `blob.upload_from_filename`, etc. Tests run in <2s
with no network.

`tests/test_integration_real.py` (opt-in via `ARC_GCS_TEST_BUCKET`)
exercises real GCS round-trips. Not run by default — add the env var
to your shell and run `pytest tests/test_integration_real.py` to
exercise it locally.

## Conventions

- **Use Edit/Write, not bash heredocs.** Repo-wide convention.
- **No multi-paragraph docstrings.** WHY-only when non-obvious;
  let names carry the WHAT.
- **No emojis in code, commit messages, or PR bodies.**
- **Tests after non-trivial changes** — `pytest` from the repo root.
- **New tool = new class in tools/<category>.py + a
  test_tools_<category>.py case + a formatter in formatters.py + a
  registration line in `plugin.py`.**

## When something breaks

- **"Plugin disabled" at startup** — check the `gcs.disabled` event's
  `reason`. Most commonly: no `allowed_buckets`, no auth, or
  invalid `escalation_level`.
- **`bucket 'X' not in allowed_buckets`** — typo, or the bucket isn't
  in `plugins.gcs.config.allowed_buckets`. Fail-closed is intentional.
- **Tool call errors with `cost_estimate_usd: 0` in event** — means
  pre-flight rejection (budget or gate). The actual SDK call didn't run.
- **Tests pass locally but fail in CI** — most likely env vars leaking.
  The `test_auth_resolution.py` tests `monkeypatch` env vars; if your
  CI environment exports `GOOGLE_APPLICATION_CREDENTIALS` globally, the
  "no SA" tests skip. That's expected.

## Pinned to v0.1

This plugin asserts against `arc.plugin_api` v0.1. When that bumps,
re-validate the plugin compiles cleanly and update `pyproject.toml`'s
classifiers + this file.
