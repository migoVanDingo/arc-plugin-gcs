"""Tests for gcs_recent, gcs_summarize_bucket, gcs_dirs, gcs_estimate_storage_cost."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from arc_plugin_gcs.tools._base import ToolError
from arc_plugin_gcs.tools.overview import (
    GCSDirs, GCSEstimateStorageCost, GCSRecent, GCSSummarizeBucket,
)


# ── gcs_recent ──


def test_recent_sorts_by_updated_desc(make_ctx, seed):
    base = datetime(2026, 5, 24, tzinfo=timezone.utc)
    seed(bucket="my-bucket", key="old.txt", content=b"x", updated=base - timedelta(days=10))
    seed(bucket="my-bucket", key="new.txt", content=b"x", updated=base)
    seed(bucket="my-bucket", key="mid.txt", content=b"x", updated=base - timedelta(days=5))
    tool = GCSRecent(make_ctx())
    out = tool.execute({"n": 3})
    # Most recent listed first
    lines = [l for l in out.splitlines() if "gs://" in l]
    assert "new.txt" in lines[0]
    assert "mid.txt" in lines[1]
    assert "old.txt" in lines[2]


def test_recent_n_caps_output(make_ctx, seed):
    base = datetime(2026, 5, 24, tzinfo=timezone.utc)
    for i in range(20):
        seed(bucket="my-bucket", key=f"o{i}.txt", content=b"x",
             updated=base - timedelta(hours=i))
    tool = GCSRecent(make_ctx())
    out = tool.execute({"n": 5})
    # Each result line has "gs://my-bucket/<key>"; the trailer also mentions
    # the bucket. Count actual object rows.
    object_lines = [l for l in out.splitlines() if l.startswith("gs://my-bucket/")]
    assert len(object_lines) == 5


def test_recent_empty(make_ctx):
    tool = GCSRecent(make_ctx())
    out = tool.execute({})
    assert "no objects" in out


# ── gcs_summarize_bucket ──


def test_summarize_with_breakdown(make_ctx, seed, bus):
    seed(bucket="my-bucket", key="a.mp4", content=b"x" * 100, content_type="video/mp4")
    seed(bucket="my-bucket", key="b.mp4", content=b"x" * 200, content_type="video/mp4")
    seed(bucket="my-bucket", key="c.jpg", content=b"x" * 50, content_type="image/jpeg")
    tool = GCSSummarizeBucket(make_ctx())
    out = tool.execute({"breakdown": True})
    assert "3" in out  # total objects
    assert ".mp4" in out
    assert ".jpg" in out
    assert "By extension" in out
    e = [e for e in bus.emitted if e.type == "gcs.summarize_bucket.completed"][0]
    assert e.payload["n_objects"] == 3
    assert e.payload["total_bytes"] == 350


def test_summarize_without_breakdown(make_ctx, seed):
    seed(bucket="my-bucket", key="a.mp4", content=b"x" * 100, content_type="video/mp4")
    tool = GCSSummarizeBucket(make_ctx())
    out = tool.execute({"breakdown": False})
    assert "By extension" not in out
    # Still includes total
    assert "1" in out


def test_summarize_empty(make_ctx):
    tool = GCSSummarizeBucket(make_ctx())
    out = tool.execute({"prefix": "gs://my-bucket/"})
    assert "no objects" in out


# ── gcs_dirs ──


def test_dirs_returns_delimited_prefixes(make_ctx, seed):
    seed(bucket="my-bucket", key="photos/a.jpg", content=b"x")
    seed(bucket="my-bucket", key="photos/b.jpg", content=b"x")
    seed(bucket="my-bucket", key="videos/x.mp4", content=b"x")
    seed(bucket="my-bucket", key="research/p.pdf", content=b"x")
    tool = GCSDirs(make_ctx())
    out = tool.execute({})
    assert "photos/" in out
    assert "videos/" in out
    assert "research/" in out


def test_dirs_one_level_deep_only(make_ctx, seed):
    """The agent under a/ should NOT see a/b/ as a direct subdir if /b/
    contains an object — it should see just `a/b/`."""
    seed(bucket="my-bucket", key="a/x.txt", content=b"x")
    seed(bucket="my-bucket", key="a/b/y.txt", content=b"x")
    tool = GCSDirs(make_ctx())
    out = tool.execute({"prefix": "gs://my-bucket/a/"})
    assert "a/b/" in out


def test_dirs_empty_prefix_returns_sentinel(make_ctx):
    tool = GCSDirs(make_ctx())
    out = tool.execute({"prefix": "gs://my-bucket/nope/"})
    assert "no directories" in out


# ── gcs_estimate_storage_cost ──


def test_estimate_us_multi_standard(make_ctx, seed):
    # Seed exactly 1 GiB
    one_gib = b"x" * (1024 ** 3)
    seed(bucket="my-bucket", key="big.bin", content=one_gib)
    tool = GCSEstimateStorageCost(make_ctx())
    out = tool.execute({"region": "us-multi", "storage_class": "STANDARD"})
    assert "$0.026" in out or "$0.0260" in out  # rate per GB-month
    assert "STANDARD" in out
    assert "us-multi" in out


def test_estimate_unknown_pair_errors(make_ctx, seed):
    seed(bucket="my-bucket", key="x.bin", content=b"x")
    tool = GCSEstimateStorageCost(make_ctx())
    with pytest.raises(ToolError, match="no rate for"):
        tool.execute({"region": "mars", "storage_class": "STANDARD"})


def test_estimate_payload_has_monthly_estimate(make_ctx, seed, bus):
    seed(bucket="my-bucket", key="x.bin", content=b"x" * (1024 ** 3))
    GCSEstimateStorageCost(make_ctx()).execute({})
    e = [e for e in bus.emitted if e.type == "gcs.estimate_storage_cost.completed"][0]
    assert "monthly_estimate_usd" in e.payload
    assert e.payload["monthly_estimate_usd"] == pytest.approx(0.026, abs=1e-4)
