"""Per-op cost calculation correctness + format_cost_usd rendering."""
from __future__ import annotations

import pytest

from arc_plugin_gcs import rates


def test_class_a_constant():
    assert rates.CLASS_A_USD_PER_CALL == pytest.approx(5e-7)


def test_class_b_constant():
    assert rates.CLASS_B_USD_PER_CALL == pytest.approx(4e-8)


def test_list_cost_is_class_a():
    c = rates.list_cost()
    assert c.cost_usd == pytest.approx(5e-7)
    assert c.bytes_transferred == 0


def test_stat_cost_is_class_b():
    c = rates.stat_cost()
    assert c.cost_usd == pytest.approx(4e-8)
    assert c.bytes_transferred == 0


def test_upload_cost_has_zero_egress():
    c = rates.upload_cost(size_bytes=10 * (1024 ** 3))  # 10 GiB
    # Upload is free; only the Class A op cost counts.
    assert c.cost_usd == pytest.approx(5e-7)
    assert c.bytes_transferred == 10 * (1024 ** 3)


def test_download_cost_1gb():
    c = rates.download_cost(size_bytes=1024 ** 3)
    # Class B + 1 GB * $0.12 ≈ $0.12 (Class B is negligible)
    assert c.cost_usd == pytest.approx(rates.CLASS_B_USD_PER_CALL + 0.12)
    assert c.bytes_transferred == 1024 ** 3


def test_download_cost_1_4gb_matches_design_example():
    # 1.4 GB download from 0021 design — expected ~ $0.168
    c = rates.download_cost(size_bytes=int(1.4 * 1024 ** 3))
    assert c.cost_usd == pytest.approx(1.4 * 0.12, abs=1e-4)


def test_signed_url_cost_is_class_a():
    c = rates.signed_url_cost()
    assert c.cost_usd == pytest.approx(5e-7)


def test_read_text_cost_includes_egress():
    c = rates.read_text_cost(bytes_read=512 * 1024 ** 2)  # 512 MiB
    expected = rates.CLASS_B_USD_PER_CALL + 0.5 * 0.12
    assert c.cost_usd == pytest.approx(expected, abs=1e-6)


def test_delete_cost_is_class_a():
    c = rates.delete_cost()
    assert c.cost_usd == pytest.approx(5e-7)


def test_storage_lookup_us_multi_standard():
    monthly = rates.monthly_storage_cost_usd(
        total_bytes=1024 ** 3, region="us-multi", storage_class="STANDARD",
    )
    assert monthly == pytest.approx(0.026)


def test_storage_lookup_us_region_standard():
    monthly = rates.monthly_storage_cost_usd(
        total_bytes=1024 ** 3, region="us-region", storage_class="STANDARD",
    )
    assert monthly == pytest.approx(0.020)


def test_storage_lookup_us_multi_archive():
    monthly = rates.monthly_storage_cost_usd(
        total_bytes=1024 ** 3, region="us-multi", storage_class="ARCHIVE",
    )
    assert monthly == pytest.approx(0.0012)


def test_storage_lookup_unknown_pair_raises():
    with pytest.raises(rates.UnknownRateError, match="no rate for"):
        rates.monthly_storage_cost_usd(
            total_bytes=100, region="mars-multi", storage_class="STANDARD",
        )


# ── format_cost_usd ──


def test_format_sub_cent_renders_as_threshold():
    assert rates.format_cost_usd(5e-7) == "<$0.0001"
    assert rates.format_cost_usd(0) == "<$0.0001"
    assert rates.format_cost_usd(0.00005) == "<$0.0001"


def test_format_meaningful_uses_4_decimals():
    assert rates.format_cost_usd(0.0001) == "$0.0001"
    assert rates.format_cost_usd(0.1234) == "$0.1234"
    assert rates.format_cost_usd(12.3456) == "$12.3456"
