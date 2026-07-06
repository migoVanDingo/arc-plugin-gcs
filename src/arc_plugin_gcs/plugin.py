"""GCSPlugin — the plugin class that arc loads via the entry-point group.

Session-scoped plugin shape: owns the storage Client, the session
budget, the escalation config, and constructs the 10 GCS tools in
on_session_start. Tools are contributed via provides_tools().

If auth resolution fails or allowed_buckets is empty, the plugin
disables itself cleanly — emits gcs.disabled and returns [] from
provides_tools(). The session keeps running without GCS access.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    from arc.plugin_api import (
        PluginBuildContext,
        RuntimeEvent,
        SessionContext,
        Tool,
        TurnOutcome,
    )
except ImportError:  # pragma: no cover — installable / inspectable without arc
    PluginBuildContext = Any  # type: ignore[misc, assignment]
    RuntimeEvent = Any        # type: ignore[misc, assignment]
    SessionContext = Any      # type: ignore[misc, assignment]
    Tool = Any                # type: ignore[misc, assignment]
    TurnOutcome = Any         # type: ignore[misc, assignment]

from arc_plugin_gcs.auth import resolve_auth
from arc_plugin_gcs.budget import BudgetCaps, SessionBudget
from arc_plugin_gcs.client import GCSClient, GCSClientError
from arc_plugin_gcs.escalation import (
    EscalationLevel,
    InvalidEscalationLevel,
    validate_level,
)
from arc_plugin_gcs.tools import ToolContext
from arc_plugin_gcs.tools.file_ops import (
    GCSDelete, GCSDownload, GCSList, GCSStat, GCSUpload,
)
from arc_plugin_gcs.tools.overview import (
    GCSDirs, GCSEstimateStorageCost, GCSRecent, GCSSummarizeBucket,
)
from arc_plugin_gcs.tools.sharing import GCSReadText, GCSSignedURL


class GCSPlugin:
    """Session-scoped GCS plugin. One instance per arc session."""

    name = "gcs"

    def __init__(
        self,
        *,
        allowed_buckets: list[str],
        default_bucket: str | None,
        credentials_env: str,
        escalation_level: EscalationLevel,
        budget_caps: BudgetCaps,
        max_text_read_bytes: int,
        max_list_results: int,
        max_signed_url_minutes: int,
        user_gate: Any,
        download_dir: Path = Path.home() / ".arc" / "downloads",
    ) -> None:
        self._allowed = list(allowed_buckets)
        self._default = default_bucket
        self._credentials_env = credentials_env
        self._escalation = escalation_level
        self._budget_caps = budget_caps
        self._max_text_read_bytes = max_text_read_bytes
        self._max_list_results = max_list_results
        self._max_signed_url_minutes = max_signed_url_minutes
        self._user_gate = user_gate
        self._download_dir = download_dir
        self._bus: Any = None
        self._client: GCSClient | None = None
        self._tools: list[Any] = []

    # ── Bus binding ────────────────────────────────────────────────────────

    def bind_bus(self, bus: Any) -> None:
        self._bus = bus

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def on_session_start(self, ctx: SessionContext) -> None:
        """Resolve auth, build the client wrapper, construct tools.

        Failures here = clean opt-out (gcs.disabled event, empty tool
        list). The session continues without GCS.
        """
        # Fail fast if no buckets configured.
        if not self._allowed:
            self._emit("gcs.disabled", payload={
                "reason": "allowed_buckets is empty; fail closed",
            }, severity="warn")
            return

        # Resolve auth.
        auth = resolve_auth(credentials_env=self._credentials_env)
        if not auth.ok:
            self._emit("gcs.disabled", payload={"reason": auth.reason}, severity="warn")
            return

        # Build SDK client.
        try:
            sdk_client = auth.client_factory()
            self._client = GCSClient(
                sdk_client=sdk_client,
                allowed_buckets=self._allowed,
                default_bucket=self._default,
            )
        except GCSClientError as exc:
            self._emit("gcs.disabled", payload={"reason": str(exc)}, severity="warn")
            return
        except Exception as exc:  # noqa: BLE001 — surface auth/network as disabled
            self._emit("gcs.disabled", payload={
                "reason": f"client construction failed: {type(exc).__name__}: {exc}",
            }, severity="warn")
            return

        caps = self._budget_caps
        budget_uncapped = (caps.max_api_calls is None
                           and caps.max_bytes_transferred is None
                           and caps.max_cost_usd is None)
        self._emit("gcs.client_ready", payload={
            "credential_source": auth.source,
            "allowed_buckets": list(self._allowed),
            "default_bucket": self._default,
            "escalation_level": self._escalation,
            "budget_uncapped": budget_uncapped,
        })
        if budget_uncapped:
            # The advertised cost/quota guard is inert until `session_budget`
            # is configured — say so loudly (M11).
            self._emit("gcs.budget.uncapped", payload={
                "reason": "no session_budget configured — GCS calls/bytes/cost "
                          "are uncapped this session",
            }, severity="warn")

        # Build the shared tool context.
        budget = SessionBudget(self._budget_caps)
        tool_ctx = ToolContext(
            client=self._client,
            budget=budget,
            escalation_level=self._escalation,
            user_gate=self._user_gate,
            max_text_read_bytes=self._max_text_read_bytes,
            max_list_results=self._max_list_results,
            max_signed_url_minutes=self._max_signed_url_minutes,
            download_dir=self._download_dir,
            bus=self._bus,
        )

        # Construct tools — all 10, in declaration order.
        self._tools = [
            GCSList(tool_ctx),
            GCSStat(tool_ctx),
            GCSUpload(tool_ctx),
            GCSDownload(tool_ctx),
            GCSDelete(tool_ctx),
            GCSSignedURL(tool_ctx),
            GCSReadText(tool_ctx),
            GCSRecent(tool_ctx),
            GCSSummarizeBucket(tool_ctx),
            GCSDirs(tool_ctx),
            GCSEstimateStorageCost(tool_ctx),
        ]

    def on_session_end(self, ctx: SessionContext, outcome: TurnOutcome | None) -> None:
        """Idempotent cleanup. Closes the SDK client if we have one."""
        if self._client is not None:
            self._client.close()
        self._client = None
        self._tools = []

    # ── Tool contribution ──────────────────────────────────────────────────

    def provides_tools(self) -> list[Any]:
        """Returns the tools constructed in on_session_start.

        Empty list when the plugin disabled itself (missing auth, no
        buckets configured, etc.).
        """
        return list(self._tools)

    # ── Internal ───────────────────────────────────────────────────────────

    def _emit(self, event_type: str, *, payload: dict, severity: str = "info") -> None:
        if self._bus is None:
            return
        try:
            self._bus.emit(RuntimeEvent(
                type=event_type, stage="plugin",
                severity=severity, payload=payload,
            ))
        except Exception:  # noqa: BLE001 — bus is best-effort
            pass


# ── Entry-point ────────────────────────────────────────────────────────────


def _validate_buckets(raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise ValueError("plugins.gcs.config.allowed_buckets must be a list")
    out: list[str] = []
    for x in raw:
        s = str(x).strip()
        if not s:
            raise ValueError("plugins.gcs.config.allowed_buckets contains an empty entry")
        out.append(s)
    return out


def _validate_budget(raw: Any) -> BudgetCaps:
    if raw is None:
        return BudgetCaps()
    if not isinstance(raw, dict):
        raise ValueError("plugins.gcs.config.session_budget must be a mapping")
    return BudgetCaps(
        max_api_calls=_optional_int(raw.get("max_api_calls"), "max_api_calls"),
        max_bytes_transferred=_optional_int(
            raw.get("max_bytes_transferred"), "max_bytes_transferred",
        ),
        max_cost_usd=_optional_float(raw.get("max_cost_usd"), "max_cost_usd"),
    )


def _optional_int(v: Any, field: str) -> int | None:
    if v is None:
        return None
    try:
        n = int(v)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"session_budget.{field} must be an integer or null") from exc
    if n < 0:
        raise ValueError(f"session_budget.{field} must be >= 0")
    return n


def _optional_float(v: Any, field: str) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"session_budget.{field} must be a number or null") from exc
    if f < 0:
        raise ValueError(f"session_budget.{field} must be >= 0")
    return f


def build(config: dict, build_ctx: PluginBuildContext) -> GCSPlugin:
    """Entry-point callable for the `arc.plugins` group.

    Validates and parses the plugin's config block, then constructs
    GCSPlugin. Validation errors raise — arc's plugin loader catches
    and surfaces them.
    """
    try:
        allowed_buckets = _validate_buckets(config.get("allowed_buckets"))
        default_bucket = config.get("default_bucket")
        if default_bucket is not None:
            default_bucket = str(default_bucket).strip() or None

        credentials_env = str(config.get("credentials_env", "GOOGLE_APPLICATION_CREDENTIALS"))

        escalation_level: EscalationLevel = validate_level(
            config.get("escalation_level", "destructive")
        )

        budget_caps = _validate_budget(config.get("session_budget"))

        max_text_read_bytes = int(config.get("max_text_read_bytes", 1_048_576))
        max_text_read_bytes = min(max_text_read_bytes, 10 * 1024 * 1024)

        max_list_results = int(config.get("max_list_results", 1000))
        max_list_results = min(max_list_results, 1000)

        max_signed_url_minutes = int(config.get("max_signed_url_minutes", 1440))
        max_signed_url_minutes = min(max_signed_url_minutes, 1440)

    except (InvalidEscalationLevel, ValueError) as exc:
        # Reraise so arc surfaces this at startup; the plugin doesn't
        # silently misbehave with bad config.
        raise

    # Downloads are confined to this dir (host-write safety). Config overrides;
    # default is <arc_home>/downloads, derived from the sessions dir.
    dl_cfg = config.get("download_dir")
    if dl_cfg:
        download_dir = Path(str(dl_cfg)).expanduser()
    else:
        sessions_dir = getattr(build_ctx, "sessions_dir", None)
        download_dir = (Path(sessions_dir).parent / "downloads") if sessions_dir \
            else Path.home() / ".arc" / "downloads"

    plugin = GCSPlugin(
        allowed_buckets=allowed_buckets,
        default_bucket=default_bucket,
        credentials_env=credentials_env,
        escalation_level=escalation_level,
        budget_caps=budget_caps,
        max_text_read_bytes=max_text_read_bytes,
        max_list_results=max_list_results,
        max_signed_url_minutes=max_signed_url_minutes,
        user_gate=getattr(build_ctx, "user_gate", None),
        download_dir=download_dir,
    )
    bus = getattr(build_ctx, "bus", None)
    if bus is not None:
        plugin.bind_bus(bus)
    return plugin
