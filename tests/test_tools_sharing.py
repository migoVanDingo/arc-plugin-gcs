"""Tests for gcs_signed_url and gcs_read_text."""
from __future__ import annotations

import pytest

from arc_plugin_gcs.tools._base import ToolError
from arc_plugin_gcs.tools.sharing import GCSReadText, GCSSignedURL


# ── gcs_signed_url ──


def test_signed_url_returns_https(make_ctx, seed):
    seed(bucket="my-bucket", key="video.mp4", content=b"...", content_type="video/mp4")
    tool = GCSSignedURL(make_ctx())
    out = tool.execute({"uri": "gs://my-bucket/video.mp4", "expires_in_minutes": 30})
    assert out.startswith("https://")
    assert "my-bucket" in out


def test_signed_url_event_omits_url_body(make_ctx, seed, bus):
    """URL must NOT be in the event payload — credentials don't get logged."""
    seed(bucket="my-bucket", key="x.bin", content=b"...")
    GCSSignedURL(make_ctx()).execute({"uri": "gs://my-bucket/x.bin"})
    e = [e for e in bus.emitted if e.type == "gcs.signed_url.issued"][0]
    for value in e.payload.values():
        if isinstance(value, str):
            assert "X-Goog-Algorithm" not in value, "signed URL leaked into payload!"


def test_signed_url_expiry_clamped(make_ctx, seed):
    seed(bucket="my-bucket", key="x.bin", content=b"...")
    tool = GCSSignedURL(make_ctx())
    out = tool.execute({
        "uri": "gs://my-bucket/x.bin",
        "expires_in_minutes": 99999,  # well above 24h ceiling
    })
    assert "clamped" in out


def test_signed_url_rejects_zero_expiry(make_ctx, seed):
    seed(bucket="my-bucket", key="x.bin", content=b"...")
    tool = GCSSignedURL(make_ctx())
    with pytest.raises(ToolError):
        tool.execute({"uri": "gs://my-bucket/x.bin", "expires_in_minutes": 0})


# ── gcs_read_text ──


def test_read_text_returns_content(make_ctx, seed):
    seed(
        bucket="my-bucket", key="notes.txt",
        content=b"line 1\nline 2", content_type="text/plain",
    )
    tool = GCSReadText(make_ctx())
    out = tool.execute({"uri": "gs://my-bucket/notes.txt"})
    assert "line 1" in out
    assert "line 2" in out


def test_read_text_rejects_binary(make_ctx, seed):
    seed(
        bucket="my-bucket", key="image.png",
        content=b"\x89PNG\r\n", content_type="image/png",
    )
    tool = GCSReadText(make_ctx())
    with pytest.raises(ToolError, match="not text-shaped"):
        tool.execute({"uri": "gs://my-bucket/image.png"})


def test_read_text_truncates_long_content(make_ctx, seed):
    big = b"x" * 10_000
    seed(bucket="my-bucket", key="big.txt", content=big, content_type="text/plain")
    tool = GCSReadText(make_ctx())
    out = tool.execute({"uri": "gs://my-bucket/big.txt", "max_bytes": 100})
    assert "truncated" in out
    body, _, _ = out.partition("\n\n(")
    assert len(body) == 100


def test_read_text_accepts_json_content_type(make_ctx, seed):
    seed(
        bucket="my-bucket", key="d.json",
        content=b'{"k":1}', content_type="application/json",
    )
    tool = GCSReadText(make_ctx())
    out = tool.execute({"uri": "gs://my-bucket/d.json"})
    assert '"k":1' in out


def test_read_text_event_has_bytes_read(make_ctx, seed, bus):
    seed(bucket="my-bucket", key="t.txt", content=b"abc", content_type="text/plain")
    GCSReadText(make_ctx()).execute({"uri": "gs://my-bucket/t.txt"})
    e = [e for e in bus.emitted if e.type == "gcs.read_text.completed"][0]
    assert e.payload["bytes_read"] == 3
    assert "cost_estimate_usd" in e.payload
