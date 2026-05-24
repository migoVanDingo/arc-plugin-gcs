"""arc-plugin-gcs — Google Cloud Storage tools for arc agents.

Entry point:
    arc.plugins.gcs = "arc_plugin_gcs.plugin:build"

Public surface:
    GCSPlugin           — the plugin class (build() returns one)
    build               — the entry-point callable

Internal modules (not for out-of-tree consumption):
    auth, client, rates, budget, escalation, formatters, tools/*
"""
from arc_plugin_gcs.plugin import GCSPlugin, build

__version__ = "0.1.0"
__all__ = ["GCSPlugin", "build", "__version__"]
