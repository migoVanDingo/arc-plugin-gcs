"""Tiered escalation policy correctness."""
from __future__ import annotations

import pytest

from arc_plugin_gcs.escalation import (
    InvalidEscalationLevel,
    should_escalate,
    validate_level,
)


# ── validate_level ──


def test_validate_destructive():
    assert validate_level("destructive") == "destructive"


def test_validate_mutations():
    assert validate_level("mutations") == "mutations"


def test_validate_all():
    assert validate_level("all") == "all"


def test_validate_case_insensitive():
    assert validate_level("DESTRUCTIVE") == "destructive"
    assert validate_level("All") == "all"


def test_validate_typo_raises():
    with pytest.raises(InvalidEscalationLevel, match="invalid escalation_level"):
        validate_level("destrcutive")


# ── should_escalate at each tier ──


def test_destructive_tier_gates_only_destructive_ops():
    L = "destructive"
    assert should_escalate("delete", L) is True
    assert should_escalate("upload_overwrite", L) is True
    assert should_escalate("download_overwrite", L) is True
    # Mutations + reads NOT gated at this tier.
    assert should_escalate("upload_new", L) is False
    assert should_escalate("signed_url", L) is False
    assert should_escalate("list", L) is False
    assert should_escalate("stat", L) is False
    assert should_escalate("read_text", L) is False


def test_mutations_tier_gates_destructive_and_mutations():
    L = "mutations"
    assert should_escalate("delete", L) is True
    assert should_escalate("upload_overwrite", L) is True
    assert should_escalate("upload_new", L) is True
    assert should_escalate("signed_url", L) is True
    # Reads still not gated.
    assert should_escalate("list", L) is False
    assert should_escalate("stat", L) is False


def test_all_tier_gates_everything():
    L = "all"
    for op in (
        "delete", "upload_overwrite", "download_overwrite",
        "upload_new", "signed_url",
        "list", "stat", "read_text", "dirs", "recent",
        "summarize_bucket", "estimate_storage_cost",
    ):
        assert should_escalate(op, L) is True, f"expected escalate for {op!r}"


def test_unknown_op_raises():
    with pytest.raises(ValueError, match="unknown operation"):
        should_escalate("bogus_op", "destructive")  # type: ignore[arg-type]
