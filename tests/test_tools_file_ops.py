"""Tests for gcs_list, gcs_stat, gcs_upload, gcs_download, gcs_delete."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from arc_plugin_gcs.tools._base import ToolError
from arc_plugin_gcs.tools.file_ops import (
    GCSDelete, GCSDownload, GCSList, GCSStat, GCSUpload,
)


# ── gcs_list ──


def test_list_returns_formatted_rows(make_ctx, seed):
    seed(bucket="my-bucket", key="a.txt", content=b"x", updated=datetime(2026, 5, 24, tzinfo=timezone.utc))
    seed(bucket="my-bucket", key="b.txt", content=b"yy", updated=datetime(2026, 5, 23, tzinfo=timezone.utc))
    tool = GCSList(make_ctx())
    out = tool.execute({"prefix": "gs://my-bucket/", "max_results": 10})
    assert "gs://my-bucket/a.txt" in out
    assert "gs://my-bucket/b.txt" in out
    assert "2026-05-24" in out


def test_list_marks_truncation(make_ctx, seed, bus):
    for i in range(5):
        seed(bucket="my-bucket", key=f"o{i}.txt", content=b"x")
    tool = GCSList(make_ctx())
    out = tool.execute({"max_results": 3})
    assert "truncated" in out
    types = bus.types()
    assert "gcs.list.completed" in types
    # Find the event and check truncated flag
    e = [e for e in bus.emitted if e.type == "gcs.list.completed"][0]
    assert e.payload["truncated"] is True
    assert e.payload["returned"] == 3


def test_list_empty_returns_sentinel(make_ctx, bus):
    tool = GCSList(make_ctx())
    out = tool.execute({"prefix": "gs://my-bucket/empty/"})
    assert "no objects" in out
    assert "gcs.list.completed" in bus.types()


def test_list_disallowed_bucket_rejected(make_ctx):
    tool = GCSList(make_ctx())
    with pytest.raises(ToolError, match="not in the configured"):
        tool.execute({"prefix": "gs://other-bucket/"})


# ── gcs_stat ──


def test_stat_returns_json(make_ctx, seed, bus):
    seed(
        bucket="my-bucket", key="foo.txt",
        content=b"hello world", content_type="text/plain",
        updated=datetime(2026, 5, 24, tzinfo=timezone.utc),
    )
    tool = GCSStat(make_ctx())
    out = tool.execute({"uri": "gs://my-bucket/foo.txt"})
    data = json.loads(out)
    assert data["uri"] == "gs://my-bucket/foo.txt"
    assert data["size_bytes"] == 11
    assert data["content_type"] == "text/plain"
    assert "gcs.stat.completed" in bus.types()


def test_stat_missing_object_clear_error(make_ctx):
    tool = GCSStat(make_ctx())
    with pytest.raises(ToolError):
        tool.execute({"uri": "gs://my-bucket/never-existed"})


def test_stat_emits_cost_in_payload(make_ctx, seed, bus):
    seed(bucket="my-bucket", key="x.txt", content=b"x")
    GCSStat(make_ctx()).execute({"uri": "gs://my-bucket/x.txt"})
    e = [e for e in bus.emitted if e.type == "gcs.stat.completed"][0]
    assert "cost_estimate_usd" in e.payload
    assert "bytes_transferred" in e.payload


# ── gcs_upload ──


def test_upload_happy_path(make_ctx, tmp_path: Path, bus):
    local = tmp_path / "data.bin"
    local.write_bytes(b"payload-here")
    tool = GCSUpload(make_ctx())
    out = tool.execute({
        "local_path": str(local),
        "uri": "gs://my-bucket/uploaded/data.bin",
    })
    assert "uploaded" in out
    assert "gs://my-bucket/uploaded/data.bin" in out
    e = [e for e in bus.emitted if e.type == "gcs.upload.completed"][0]
    assert e.payload["size_bytes"] == len(b"payload-here")
    assert e.payload["was_overwrite"] is False


def test_upload_refuses_overwrite_by_default(make_ctx, tmp_path, seed):
    local = tmp_path / "f.bin"
    local.write_bytes(b"new")
    seed(bucket="my-bucket", key="exists.bin", content=b"old")
    tool = GCSUpload(make_ctx())
    with pytest.raises(ToolError, match="would overwrite"):
        tool.execute({
            "local_path": str(local),
            "uri": "gs://my-bucket/exists.bin",
        })


def test_upload_overwrite_routes_through_gate_allowed(make_ctx, tmp_path, seed, bus):
    """With allow_gate (default), overwrite=true succeeds."""
    local = tmp_path / "f.bin"
    local.write_bytes(b"new")
    seed(bucket="my-bucket", key="exists.bin", content=b"old")
    tool = GCSUpload(make_ctx())
    out = tool.execute({
        "local_path": str(local),
        "uri": "gs://my-bucket/exists.bin",
        "overwrite": True,
    })
    assert "overwrote" in out
    types = bus.types()
    assert "gcs.escalation.requested" in types
    assert "gcs.upload.completed" in types


def test_upload_overwrite_denied_by_deny_gate(make_ctx, tmp_path, seed, deny_gate):
    local = tmp_path / "f.bin"
    local.write_bytes(b"new")
    seed(bucket="my-bucket", key="exists.bin", content=b"old")
    tool = GCSUpload(make_ctx(gate=deny_gate))
    with pytest.raises(ToolError, match="denied by user gate"):
        tool.execute({
            "local_path": str(local),
            "uri": "gs://my-bucket/exists.bin",
            "overwrite": True,
        })


def test_upload_local_file_missing(make_ctx, tmp_path):
    tool = GCSUpload(make_ctx())
    with pytest.raises(ToolError, match="local file not found"):
        tool.execute({
            "local_path": str(tmp_path / "nope.bin"),
            "uri": "gs://my-bucket/x.bin",
        })


# ── gcs_download ──


def test_download_happy_path(make_ctx, tmp_path, seed, bus):
    seed(bucket="my-bucket", key="src.bin", content=b"the-bytes")
    ctx = make_ctx()
    ctx.download_dir = tmp_path
    out = GCSDownload(ctx).execute({
        "uri": "gs://my-bucket/src.bin",
        "local_path": "downloaded.bin",   # relative to download_dir
    })
    assert (tmp_path / "downloaded.bin").read_bytes() == b"the-bytes"
    assert "downloaded" in out
    e = [e for e in bus.emitted if e.type == "gcs.download.completed"][0]
    assert e.payload["size_bytes"] == 9


def test_download_refuses_overwrite_by_default(make_ctx, tmp_path, seed):
    seed(bucket="my-bucket", key="x.bin", content=b"data")
    (tmp_path / "exists.bin").write_bytes(b"OLD")
    ctx = make_ctx()
    ctx.download_dir = tmp_path
    with pytest.raises(ToolError, match="would overwrite"):
        GCSDownload(ctx).execute({
            "uri": "gs://my-bucket/x.bin",
            "local_path": "exists.bin",
        })


def test_download_overwrite_with_gate(make_ctx, tmp_path, seed):
    seed(bucket="my-bucket", key="x.bin", content=b"new")
    (tmp_path / "exists.bin").write_bytes(b"OLD")
    ctx = make_ctx()
    ctx.download_dir = tmp_path
    out = GCSDownload(ctx).execute({
        "uri": "gs://my-bucket/x.bin",
        "local_path": "exists.bin",
        "overwrite": True,
    })
    assert (tmp_path / "exists.bin").read_bytes() == b"new"
    assert "downloaded" in out


def test_download_rejects_path_escape(make_ctx, tmp_path, seed):
    """C6: absolute paths and `..` escapes are refused (host-write confinement)."""
    seed(bucket="my-bucket", key="x.bin", content=b"data")
    ctx = make_ctx()
    ctx.download_dir = tmp_path / "dl"
    # absolute paths and `..` escapes are rejected; a literal `~` is NOT expanded
    # (it becomes a harmless subdir), so it's confined, not an escape.
    for bad in ("/etc/passwd", "../escape.bin", "a/../../escape.bin"):
        with pytest.raises(ToolError):
            GCSDownload(ctx).execute({"uri": "gs://my-bucket/x.bin", "local_path": bad})


def test_download_new_is_a_gated_write_op(make_ctx, tmp_path, seed, deny_gate):
    """H10: a NEW-file download is a write (download_new) — a deny gate at the
    mutations level blocks it (it used to be mis-classified as a read)."""
    from arc_plugin_gcs.escalation import _MUTATION_OPS
    assert "download_new" in _MUTATION_OPS
    seed(bucket="my-bucket", key="x.bin", content=b"data")
    ctx = make_ctx(gate=deny_gate)
    ctx.download_dir = tmp_path
    ctx.escalation_level = "mutations"
    with pytest.raises(ToolError):
        GCSDownload(ctx).execute({"uri": "gs://my-bucket/x.bin", "local_path": "new.bin"})
    assert not (tmp_path / "new.bin").exists()


# ── gcs_delete ──


def test_delete_always_routes_through_gate(make_ctx, seed, bus):
    seed(bucket="my-bucket", key="goner.txt", content=b"bye")
    tool = GCSDelete(make_ctx())  # destructive tier (default) DOES gate delete
    tool.execute({"uri": "gs://my-bucket/goner.txt"})
    types = bus.types()
    assert "gcs.escalation.requested" in types
    assert "gcs.delete.completed" in types


def test_delete_denied_by_gate(make_ctx, seed, deny_gate, bus):
    seed(bucket="my-bucket", key="protected.txt", content=b"important")
    tool = GCSDelete(make_ctx(gate=deny_gate))
    with pytest.raises(ToolError, match="denied"):
        tool.execute({"uri": "gs://my-bucket/protected.txt"})
    # No completion event because the delete never happened.
    assert "gcs.delete.completed" not in bus.types()
    # But escalation events did fire.
    assert "gcs.escalation.denied" in bus.types()


def test_delete_missing_object_error(make_ctx):
    tool = GCSDelete(make_ctx())
    with pytest.raises(ToolError):
        tool.execute({"uri": "gs://my-bucket/never-existed"})
