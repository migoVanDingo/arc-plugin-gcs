"""Authentication resolution for the GCS plugin.

Two paths:
  1. Service-account JSON pointed at by an env var (default
     `GOOGLE_APPLICATION_CREDENTIALS`).
  2. Application-default credentials (`gcloud auth application-default login`).

If neither resolves, the plugin disables itself at session start with
a clear reason. We never silently use anonymous client — that's how
embarrassing 'leaked-data' bugs happen.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

CredentialSource = Literal["service_account_file", "application_default"]


@dataclass(frozen=True)
class AuthResolution:
    """Result of trying both auth paths."""
    ok: bool
    source: CredentialSource | None
    reason: str
    # When ok=True, a callable that constructs a `storage.Client`.
    # Held as Any to avoid importing google.cloud.storage at module
    # load time (lets tests run without the real SDK if needed).
    client_factory: Any = None


def resolve_auth(*, credentials_env: str = "GOOGLE_APPLICATION_CREDENTIALS") -> AuthResolution:
    """Try SA-JSON first, then ADC.  Pure (no network)."""
    # Path 1: service-account JSON.
    sa_path_str = os.environ.get(credentials_env)
    if sa_path_str:
        sa_path = Path(sa_path_str)
        if not sa_path.exists():
            return AuthResolution(
                ok=False,
                source=None,
                reason=(
                    f"{credentials_env}={sa_path_str} is set but the file "
                    f"does not exist"
                ),
            )
        try:
            from google.cloud import storage
        except ImportError as exc:
            return AuthResolution(
                ok=False, source=None,
                reason=f"google-cloud-storage not installed: {exc}",
            )

        def _factory():
            return storage.Client.from_service_account_json(str(sa_path))

        return AuthResolution(
            ok=True,
            source="service_account_file",
            reason=f"using service account JSON at {sa_path}",
            client_factory=_factory,
        )

    # Path 2: ADC.
    try:
        from google.auth import default as adc_default
        from google.cloud import storage
    except ImportError as exc:
        return AuthResolution(
            ok=False, source=None,
            reason=f"google-cloud-storage not installed: {exc}",
        )

    try:
        adc_default()  # raises if ADC not configured
    except Exception as exc:  # noqa: BLE001 — defensive
        return AuthResolution(
            ok=False,
            source=None,
            reason=(
                f"{credentials_env} not set and application-default "
                f"credentials not configured ({type(exc).__name__}: {exc}). "
                f"Run `gcloud auth application-default login` or set "
                f"{credentials_env} to a service-account JSON path."
            ),
        )

    def _factory():
        return storage.Client()

    return AuthResolution(
        ok=True,
        source="application_default",
        reason="using application-default credentials",
        client_factory=_factory,
    )
