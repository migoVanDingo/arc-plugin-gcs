"""SessionBudget enforcement."""
from __future__ import annotations

import pytest

from arc_plugin_gcs.budget import BudgetCaps, BudgetDenial, SessionBudget


def test_no_caps_allows_everything():
    b = SessionBudget(BudgetCaps())
    for _ in range(1000):
        denial = b.try_reserve(api_calls=1, bytes_transferred=10_000_000, cost_usd=1.0)
        assert denial is None
        b.commit(api_calls=1, bytes_transferred=10_000_000, cost_usd=1.0)
    assert b.api_calls_used == 1000


def test_api_call_cap_blocks_4th_when_3_allowed():
    b = SessionBudget(BudgetCaps(max_api_calls=3))
    for _ in range(3):
        assert b.try_reserve() is None
        b.commit()
    denial = b.try_reserve()
    assert isinstance(denial, BudgetDenial)
    assert denial.cap == "api_calls"
    assert "session GCS budget exceeded" in denial.message()


def test_bytes_cap_pre_flight_rejection():
    b = SessionBudget(BudgetCaps(max_bytes_transferred=1024))
    # 2048 bytes pre-flight rejected — no API call attempted.
    denial = b.try_reserve(bytes_transferred=2048)
    assert denial is not None and denial.cap == "bytes_transferred"
    # The denial was pre-flight: counters NOT incremented.
    assert b.bytes_transferred == 0
    assert b.api_calls_used == 0


def test_cost_cap_pre_flight_rejection():
    b = SessionBudget(BudgetCaps(max_cost_usd=0.10))
    denial = b.try_reserve(cost_usd=0.15)
    assert denial is not None
    assert denial.cap == "cost_usd"
    assert "cost cap reached" in denial.message()


def test_first_denial_only_emits_event_once():
    b = SessionBudget(BudgetCaps(max_api_calls=0))
    # First denial — caller should emit
    denial = b.try_reserve()
    assert denial is not None
    assert b.mark_denial_emitted(denial.cap) is True
    # Subsequent denials — already emitted, don't re-emit
    denial2 = b.try_reserve()
    assert denial2 is not None
    assert b.mark_denial_emitted(denial.cap) is False


def test_failed_call_does_not_commit():
    """Caller is expected to call commit() only on success. We don't
    auto-commit anything from try_reserve — that's the contract."""
    b = SessionBudget(BudgetCaps(max_api_calls=10))
    b.try_reserve()
    # Simulate the API call failing — we never call commit().
    assert b.api_calls_used == 0


def test_per_cap_isolation():
    """Hitting api_calls cap doesn't affect bytes/cost counters."""
    b = SessionBudget(BudgetCaps(max_api_calls=1))
    b.try_reserve()
    b.commit()
    # api_calls now at cap; next call denied for api_calls but
    # bytes/cost weren't yet touched and remain zero.
    denial = b.try_reserve(bytes_transferred=100, cost_usd=0.5)
    assert denial is not None and denial.cap == "api_calls"
    assert b.bytes_transferred == 0
    assert b.cost_usd_used == 0
