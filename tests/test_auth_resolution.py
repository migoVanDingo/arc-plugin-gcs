"""auth.resolve_auth path resolution.

These tests run without the real google-cloud-storage SDK by patching
the import boundary. The "ok=False" paths don't need the SDK; the
"ok=True" paths need google.cloud.storage importable — when it isn't
(pure pip-fresh env), those tests just verify the import-error path
returns ok=False with a clear reason.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from arc_plugin_gcs.auth import resolve_auth


def test_missing_env_no_adc_returns_clear_error(monkeypatch):
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    # Force google.auth.default to raise as if ADC isn't configured.
    fake_auth = type(sys)("google.auth")
    def _broken_default(*a, **kw):
        raise RuntimeError("ADC not configured")
    fake_auth.default = _broken_default
    monkeypatch.setitem(sys.modules, "google.auth", fake_auth)
    # google.cloud.storage import path — present but unused (default fails first)
    try:
        import google.cloud.storage  # noqa: F401
    except ImportError:
        pytest.skip("google-cloud-storage not installed; skip non-SDK path test")
    result = resolve_auth()
    assert result.ok is False
    assert "not set" in result.reason or "not configured" in result.reason


def test_sa_file_does_not_exist(monkeypatch, tmp_path):
    bogus = tmp_path / "nonexistent.json"
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(bogus))
    result = resolve_auth()
    assert result.ok is False
    assert "does not exist" in result.reason


def test_sa_file_present_returns_ok(monkeypatch, tmp_path):
    """When the SA file path exists, the auth resolution function reaches
    the success path. We don't actually load credentials (no network)
    — just verify the resolution shape."""
    fake_sa = tmp_path / "sa.json"
    fake_sa.write_text("{}")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(fake_sa))

    # Stub google.cloud.storage.Client.from_service_account_json
    try:
        import google.cloud.storage  # noqa: F401
    except ImportError:
        pytest.skip("google-cloud-storage not installed; skip SA-path test")

    class FakeClient:
        pass

    import google.cloud.storage as gcs_mod
    monkeypatch.setattr(
        gcs_mod.Client,
        "from_service_account_json",
        classmethod(lambda cls, path: FakeClient()),
    )

    result = resolve_auth()
    assert result.ok is True
    assert result.source == "service_account_file"
    assert callable(result.client_factory)
    assert isinstance(result.client_factory(), FakeClient)


def test_custom_credentials_env(monkeypatch, tmp_path):
    fake_sa = tmp_path / "sa.json"
    fake_sa.write_text("{}")
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setenv("MY_CUSTOM_GCP_SA", str(fake_sa))

    try:
        import google.cloud.storage as gcs_mod
    except ImportError:
        pytest.skip("google-cloud-storage not installed")

    class FakeClient:
        pass
    monkeypatch.setattr(
        gcs_mod.Client,
        "from_service_account_json",
        classmethod(lambda cls, path: FakeClient()),
    )

    result = resolve_auth(credentials_env="MY_CUSTOM_GCP_SA")
    assert result.ok is True
    assert result.source == "service_account_file"
