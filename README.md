# arc-plugin-gcs

Google Cloud Storage tools for arc agents — list, upload, download,
signed URLs, cost/budget guards.

Designed against arc v2's plugin API (`arc.plugin_api` v0.1). See
[`v2/_design/0021-gcs-plugin.md`](../v2/_design/0021-gcs-plugin.md)
for the full design spec.

## What you get

Ten GCS tools the agent can call:

| Tool | Purpose |
|---|---|
| `gcs_list` | List objects under a prefix (recurses; capped at 1000). |
| `gcs_stat` | Full metadata for one object as JSON. |
| `gcs_upload` | Upload a local file. Implicit prefix creation. |
| `gcs_download` | Pull a GCS object to local disk. |
| `gcs_delete` | Delete an object. Always gated. |
| `gcs_signed_url` | Time-limited HTTPS URL (cross-provider bridge). |
| `gcs_read_text` | Pull a text-shaped object into context. |
| `gcs_recent` | N most-recently-modified under a prefix. |
| `gcs_summarize_bucket` | Totals + per-extension breakdown. |
| `gcs_dirs` | Immediate "subdirectories" via delimiter listing. |
| `gcs_estimate_storage_cost` | Monthly cost estimate from public rate card. |

Plus three orthogonal safety + cost mechanisms:

- **Bucket allowlist** — only buckets in `allowed_buckets` are reachable.
  Required (empty = plugin disables itself).
- **Tiered escalation** — `destructive` (default) gates only deletes
  and overwrites; `mutations` adds new uploads and signed URLs; `all`
  gates every read.
- **Session budgets** — per-session caps on total API calls, bytes
  transferred, and estimated cost. Catches runaway agents without
  requiring per-call confirmation.

Every `gcs.*.completed` event carries `cost_estimate_usd` and
`bytes_transferred` so logs and the TUI can render per-call cost
inline (matching arc's existing tokens/time display).

## Install

```bash
pip install arc-plugin-gcs
```

Or, while developing against a local arc checkout:

```bash
pip install -e /path/to/arc-plugin-gcs
pip install -e /path/to/arc/v2
```

## Authentication

Two paths, in priority order:

1. **Service-account JSON** — set the path in an environment variable
   (default `GOOGLE_APPLICATION_CREDENTIALS`):
   ```bash
   export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
   ```
2. **Application-default credentials** — run once on the host:
   ```bash
   gcloud auth application-default login
   ```

If neither resolves, the plugin disables itself with a clear reason
emitted as a `gcs.disabled` event. The session continues without
GCS access.

## Config

In your arc `config.yml`:

```yaml
plugins:
  enabled:
    - name: gcs
      enabled: true
      config:
        # REQUIRED. Only buckets in this list are reachable. Empty = plugin
        # disables itself (fail closed).
        allowed_buckets:
          - my-bucket
          - my-bucket-scratch

        # OPTIONAL. Bare paths in tool inputs resolve to this bucket.
        # `gcs_stat("recordings/foo.mp4")` → `gs://my-bucket/recordings/foo.mp4`.
        # Must appear in allowed_buckets.
        default_bucket: my-bucket

        # OPTIONAL. Env var holding the path to a service-account JSON.
        # Defaults to GOOGLE_APPLICATION_CREDENTIALS. Falls back to ADC
        # if the env var is unset.
        credentials_env: GOOGLE_APPLICATION_CREDENTIALS

        # OPTIONAL. Escalation tier:
        #   "destructive" (default) — delete + overwrite-mode upload/download gated
        #   "mutations"             — above + new uploads + signed URLs gated
        #   "all"                   — above + every read gated (paranoid mode)
        # Headless mode (no real user gate) auto-denies whatever this gates.
        escalation_level: destructive

        # OPTIONAL. Session-scoped budget caps. Any cap hit → all further
        # GCS tool calls fail with ToolError("session GCS budget exceeded").
        # Set any field to null to disable that cap.
        session_budget:
          max_api_calls: 500
          max_bytes_transferred: 1073741824   # 1 GiB
          max_cost_usd: 0.50                  # estimated from rate table

        # OPTIONAL. Caps on read sizes (anti-OOM, anti-cost).
        max_text_read_bytes: 1048576          # 1 MiB default, hard ceiling 10 MiB
        max_list_results: 1000                # hard ceiling on gcs_list

        # OPTIONAL. Signed-URL expiry ceiling (minutes). Default 24h.
        max_signed_url_minutes: 1440
```

## Tool reference

### `gcs_list(prefix="", max_results=100)`
List objects under a prefix with URI, size, and last-modified. Recurses
through implied "directories"; for one-level-deep listing use `gcs_dirs`.

### `gcs_stat(uri)`
JSON metadata: `size_bytes`, `content_type`, `updated`, `md5`,
`storage_class`, `etag`, custom `metadata`.

### `gcs_upload(local_path, uri, overwrite=false)`
Upload a local file. **Destination prefixes are created implicitly** —
upload to `gs://bucket/new/path/file.ext` works even if nothing else
exists under `new/path/`. GCS is flat; `/` in keys is just a character.

`overwrite=false` (default) refuses to clobber existing objects;
`overwrite=true` routes through the user gate before writing.

### `gcs_download(uri, local_path, overwrite=false)`
Pull an object to local disk. Same overwrite semantics as upload.

### `gcs_delete(uri)`
Delete an object. **Always gated** through the user gate regardless of
`escalation_level` — headless sessions can never delete.

### `gcs_signed_url(uri, expires_in_minutes=60)`
Generate a time-limited HTTPS URL. The cross-provider bridge — any
model that accepts image/video URLs (most do for vision) can consume
a signed URL even without native GCS support.

URL bodies are NEVER recorded in events (they're credentials).

### `gcs_read_text(uri, max_bytes=1048576)`
Pull a text-shaped object directly into the tool output. Refuses binary
content types (image, video, octet-stream, etc.). Truncates at
`max_bytes` or the plugin's configured ceiling, whichever is smaller.

### `gcs_recent(prefix="", n=10)`
The N most-recently-modified objects under a prefix, sorted desc by
`updated`. Cheap survey for "what changed today?".

### `gcs_summarize_bucket(prefix="", breakdown=true)`
Totals + optional per-extension breakdown. Use `breakdown=false` when
you just need "how much is in this bucket?".

### `gcs_dirs(prefix="", delimiter="/")`
Return the immediate "subdirectories" using delimiter listing. GCS
doesn't have real directories; this returns key prefixes that end at
the delimiter character.

### `gcs_estimate_storage_cost(prefix="", region="us-multi", storage_class="STANDARD")`
Estimated monthly storage cost from the public rate card. Not real
billing data — use your Billing console for that. Useful for "if I
keep this corpus in GCS, what's it costing?".

Recognized regions: `us-multi`, `us-region`, `eu-multi`, `eu-region`,
`asia-multi`, `asia-region`.
Recognized storage classes: `STANDARD`, `NEARLINE`, `COLDLINE`, `ARCHIVE`.

## Events

Every operation emits a `gcs.*.completed` event with `cost_estimate_usd`
and `bytes_transferred`. arc's log_writer renders these one-per-line in
`session.log`; the TUI can display per-call cost inline with the tool
output.

| Event | When |
|---|---|
| `gcs.disabled` | Plugin opted out at startup (no auth / no allowlist). |
| `gcs.client_ready` | Plugin authed and tools registered. |
| `gcs.list.completed` etc. | Tool finished successfully. |
| `gcs.escalation.requested` | Destructive op asked the user gate. |
| `gcs.escalation.denied` | User gate refused. |
| `gcs.budget_exceeded` | First session budget cap hit (one per cap). |

Failed tool calls are covered by arc's generic `tool.call.failed`
event — the plugin doesn't duplicate.

## Cost model

The plugin computes estimated cost per call from a small in-source
rate table (`src/arc_plugin_gcs/rates.py`):

- Class A op (writes, lists, copies, signed URLs): $0.0000005
- Class B op (reads, stat): $0.00000004
- Egress: $0.12 / GB
- Storage: $0.020–$0.026 / GB-month (standard, region-dependent)

These are public list prices. If you have contract pricing, the estimate
will be wrong but the relative ordering is still right.

The session-budget enforcer consumes these numbers; same numbers appear
in the events and (with arc TUI integration) the inline tool render.

## Development

```bash
pip install -e ".[dev]"
pytest
```

The unit suite runs without a real GCS bucket — uses a FakeStorageClient
fixture. The integration test (`tests/test_integration_real.py`) opts
in via `ARC_GCS_TEST_BUCKET=<your-test-bucket>` and exercises the full
round-trip against real GCS.

See `CLAUDE.md` for a code-map and developer conventions.

## License

MIT — see `LICENSE`.
