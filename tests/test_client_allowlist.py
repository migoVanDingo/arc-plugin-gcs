"""GCSClient URI parsing + bucket allowlist enforcement."""
from __future__ import annotations

import pytest

from arc_plugin_gcs.client import GCSClient, GCSClientError


def test_parse_full_uri(make_client):
    client = make_client(allowed_buckets=["my-bucket"])
    p = client.parse_uri("gs://my-bucket/path/to/file.ext")
    assert p.bucket == "my-bucket"
    assert p.key == "path/to/file.ext"
    assert p.gs_uri == "gs://my-bucket/path/to/file.ext"


def test_parse_bare_path_uses_default(make_client):
    client = make_client(default_bucket="my-bucket")
    p = client.parse_uri("research/paper.pdf")
    assert p.bucket == "my-bucket"
    assert p.key == "research/paper.pdf"


def test_parse_bare_path_strips_leading_slash(make_client):
    client = make_client(default_bucket="my-bucket")
    p = client.parse_uri("/foo/bar.txt")
    assert p.key == "foo/bar.txt"


def test_parse_bare_path_no_default_raises(make_client):
    client = make_client(default_bucket=None)
    with pytest.raises(GCSClientError, match="needs a default_bucket"):
        client.parse_uri("just/a/path.txt")


def test_parse_bucket_only(make_client):
    client = make_client(allowed_buckets=["my-bucket"])
    p = client.parse_uri("gs://my-bucket")
    assert p.bucket == "my-bucket"
    assert p.key == ""
    p = client.parse_uri("gs://my-bucket/")
    assert p.bucket == "my-bucket"
    assert p.key == ""


def test_parse_malformed_missing_slashes(make_client):
    client = make_client()
    with pytest.raises(GCSClientError, match="missing slash"):
        client.parse_uri("gs:/my-bucket/path")


def test_parse_empty_bucket_raises(make_client):
    client = make_client()
    with pytest.raises(GCSClientError, match="empty bucket"):
        client.parse_uri("gs:///key-with-no-bucket")


def test_disallowed_bucket_rejected_before_api(make_client):
    client = make_client(allowed_buckets=["my-bucket"])
    with pytest.raises(GCSClientError, match="not in allowed_buckets"):
        client.parse_uri("gs://other-bucket/foo.txt")


def test_empty_allowlist_fails_closed(fake_sdk):
    with pytest.raises(GCSClientError, match="allowed_buckets is empty"):
        GCSClient(sdk_client=fake_sdk, allowed_buckets=[], default_bucket=None)


def test_default_bucket_not_in_allowlist_rejects(fake_sdk):
    with pytest.raises(GCSClientError, match="must be in allowed_buckets"):
        GCSClient(
            sdk_client=fake_sdk,
            allowed_buckets=["my-bucket"],
            default_bucket="other-bucket",
        )


def test_require_object_rejects_bucket_root(make_client):
    client = make_client()
    with pytest.raises(GCSClientError, match="bucket root"):
        client.parse_uri("gs://my-bucket/", require_object=True)


def test_check_allowed_disallowed_raises(make_client):
    client = make_client(allowed_buckets=["foo"])
    with pytest.raises(GCSClientError, match="not in allowed_buckets"):
        client.check_allowed("bar")
