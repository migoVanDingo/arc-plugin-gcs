"""GCSPlugin lifecycle: build/start/end/disabled paths."""
from __future__ import annotations

from typing import Any

import pytest

from arc_plugin_gcs.plugin import GCSPlugin, build


# ── build() validation ────────────────────────────────────────────────────


def test_build_parses_config(build_ctx):
    cfg = {
        "allowed_buckets": ["my-bucket"],
        "default_bucket": "my-bucket",
        "escalation_level": "destructive",
        "session_budget": {"max_api_calls": 100, "max_cost_usd": 0.5},
    }
    plugin = build(cfg, build_ctx)
    assert isinstance(plugin, GCSPlugin)
    assert plugin._allowed == ["my-bucket"]
    assert plugin._default == "my-bucket"
    assert plugin._escalation == "destructive"
    assert plugin._budget_caps.max_api_calls == 100
    assert plugin._budget_caps.max_cost_usd == 0.5


def test_build_rejects_bad_escalation_level(build_ctx):
    from arc_plugin_gcs.escalation import InvalidEscalationLevel
    with pytest.raises(InvalidEscalationLevel):
        build({"allowed_buckets": ["x"], "escalation_level": "typo"}, build_ctx)


def test_build_rejects_non_list_allowed_buckets(build_ctx):
    with pytest.raises(ValueError, match="must be a list"):
        build({"allowed_buckets": "my-bucket"}, build_ctx)


def test_build_rejects_negative_budget(build_ctx):
    with pytest.raises(ValueError, match="must be >= 0"):
        build({
            "allowed_buckets": ["x"],
            "session_budget": {"max_api_calls": -1},
        }, build_ctx)


def test_build_clamps_oversized_max_text_read(build_ctx):
    plugin = build({
        "allowed_buckets": ["x"],
        "max_text_read_bytes": 999_999_999,
    }, build_ctx)
    # Clamped to 10 MiB ceiling.
    assert plugin._max_text_read_bytes == 10 * 1024 * 1024


def test_build_clamps_oversized_signed_url_minutes(build_ctx):
    plugin = build({
        "allowed_buckets": ["x"],
        "max_signed_url_minutes": 999_999,
    }, build_ctx)
    assert plugin._max_signed_url_minutes == 1440


# ── on_session_start: empty allowlist fail-closed ─────────────────────────


def test_empty_allowed_buckets_disables_cleanly(bus, session_ctx, allow_gate):
    plugin = GCSPlugin(
        allowed_buckets=[],
        default_bucket=None,
        credentials_env="X",
        escalation_level="destructive",
        budget_caps=None,  # type: ignore[arg-type]  — won't reach budget path
        max_text_read_bytes=1024,
        max_list_results=10,
        max_signed_url_minutes=60,
        user_gate=allow_gate,
    )
    plugin.bind_bus(bus)
    plugin.on_session_start(session_ctx)
    assert plugin.provides_tools() == []
    types = bus.types()
    assert "gcs.disabled" in types
    disabled = [e for e in bus.emitted if e.type == "gcs.disabled"][0]
    assert "empty" in disabled.payload["reason"]


# ── on_session_start: auth missing disables cleanly ──────────────────────


def test_missing_auth_disables_cleanly(bus, session_ctx, allow_gate, monkeypatch, tmp_path):
    """No SA file, no ADC → plugin disabled with informative reason."""
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    # Patch google.auth.default to simulate "ADC not configured".
    import sys
    fake_auth = type(sys)("google.auth")
    def _broken(*a, **kw):
        raise RuntimeError("no ADC")
    fake_auth.default = _broken
    monkeypatch.setitem(sys.modules, "google.auth", fake_auth)

    from arc_plugin_gcs.budget import BudgetCaps
    plugin = GCSPlugin(
        allowed_buckets=["my-bucket"], default_bucket=None,
        credentials_env="GOOGLE_APPLICATION_CREDENTIALS",
        escalation_level="destructive",
        budget_caps=BudgetCaps(),
        max_text_read_bytes=1024, max_list_results=10, max_signed_url_minutes=60,
        user_gate=allow_gate,
    )
    plugin.bind_bus(bus)
    plugin.on_session_start(session_ctx)
    assert plugin.provides_tools() == []
    assert "gcs.disabled" in bus.types()


# ── on_session_start: happy path with patched auth ───────────────────────


def test_happy_path_constructs_all_10_tools(bus, session_ctx, allow_gate, monkeypatch, tmp_path):
    """Patch resolve_auth so plugin gets a fake client, then verify
    on_session_start builds 11 tools (10 advertised + the dual-named
    overview tools — count what's actually constructed)."""
    from arc_plugin_gcs.auth import AuthResolution
    from arc_plugin_gcs.budget import BudgetCaps
    from arc_plugin_gcs import plugin as plugin_mod
    from tests.conftest import FakeStorageClient

    def _fake_resolve(*, credentials_env):
        return AuthResolution(
            ok=True,
            source="service_account_file",
            reason="fake",
            client_factory=lambda: FakeStorageClient(),
        )

    monkeypatch.setattr(plugin_mod, "resolve_auth", _fake_resolve)

    plugin = GCSPlugin(
        allowed_buckets=["my-bucket"], default_bucket="my-bucket",
        credentials_env="X", escalation_level="destructive",
        budget_caps=BudgetCaps(),
        max_text_read_bytes=1_048_576, max_list_results=1000,
        max_signed_url_minutes=1440, user_gate=allow_gate,
    )
    plugin.bind_bus(bus)
    plugin.on_session_start(session_ctx)

    tools = plugin.provides_tools()
    # 11 tools — 10 named in the design plus gcs_recent (counted with overview),
    # but the constructor wires GCSList, GCSStat, GCSUpload, GCSDownload, GCSDelete,
    # GCSSignedURL, GCSReadText, GCSRecent, GCSSummarizeBucket, GCSDirs,
    # GCSEstimateStorageCost = 11 instances.
    assert len(tools) == 11
    names = {t.name for t in tools}
    assert names == {
        "gcs_list", "gcs_stat", "gcs_upload", "gcs_download", "gcs_delete",
        "gcs_signed_url", "gcs_read_text",
        "gcs_recent", "gcs_summarize_bucket", "gcs_dirs", "gcs_estimate_storage_cost",
    }
    assert "gcs.client_ready" in bus.types()


# ── on_session_end cleans up ──────────────────────────────────────────────


def test_session_end_clears_state(bus, session_ctx, allow_gate, monkeypatch):
    from arc_plugin_gcs.auth import AuthResolution
    from arc_plugin_gcs.budget import BudgetCaps
    from arc_plugin_gcs import plugin as plugin_mod
    from tests.conftest import FakeStorageClient

    fake_sdk = FakeStorageClient()
    monkeypatch.setattr(plugin_mod, "resolve_auth", lambda *, credentials_env: AuthResolution(
        ok=True, source="application_default", reason="fake",
        client_factory=lambda: fake_sdk,
    ))

    plugin = GCSPlugin(
        allowed_buckets=["x"], default_bucket=None,
        credentials_env="Y", escalation_level="destructive",
        budget_caps=BudgetCaps(),
        max_text_read_bytes=1024, max_list_results=10, max_signed_url_minutes=60,
        user_gate=allow_gate,
    )
    plugin.bind_bus(bus)
    plugin.on_session_start(session_ctx)
    assert len(plugin.provides_tools()) == 11
    plugin.on_session_end(session_ctx, outcome=None)
    assert plugin.provides_tools() == []
    assert fake_sdk.closed is True
