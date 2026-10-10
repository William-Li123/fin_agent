from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import sqlite3

import pytest

from finagent.models import BudgetAccountingError, BudgetExceeded, BudgetLedger, normalize_usage


def ledger(tmp_path, ceiling=1, carried=0):
    return BudgetLedger(tmp_path / "budget.sqlite", ceiling_cny=ceiling, carried_cny=carried)


def usage(prompt=100, output=10, hit=20):
    return {"prompt_tokens": prompt, "completion_tokens": output, "total_tokens": prompt + output,
            "prompt_cache_hit_tokens": hit, "prompt_cache_miss_tokens": prompt - hit}


def test_existing_global_commitment_cannot_be_reset(tmp_path):
    path = tmp_path / "budget.sqlite"
    original = BudgetLedger(path)
    assert Decimal(original.status()["remaining_cny"]) == Decimal("27.15164580")
    assert BudgetLedger(path).status() == original.status()
    with pytest.raises(ValueError, match="immutable"):
        BudgetLedger(path, carried_cny=0)
    with pytest.raises(ValueError, match="immutable"):
        BudgetLedger(path, ceiling_cny=100)


def test_open_existing_ledger_uses_its_persisted_limits(tmp_path):
    path = tmp_path / "custom.sqlite"
    account = BudgetLedger(path, ceiling_cny="12.5", carried_cny="1.234")
    reservation = account.reserve(100, 100, stage="test", model="deepseek-flash")
    account.mark_unknown(reservation.call_id)
    reopened = BudgetLedger.open(path)
    assert reopened.status() == account.status()
    assert reopened.status()["halted"]
    assert reopened.status()["ceiling_cny"] == "12.5"
    with pytest.raises(FileNotFoundError):
        BudgetLedger.open(tmp_path / "missing.sqlite")
    assert not (tmp_path / "missing.sqlite").exists()


def test_settlement_releases_bound_and_charges_verified_cached_usage(tmp_path):
    account = ledger(tmp_path)
    reservation = account.reserve(1000, 100, stage="test.loop", model="deepseek-flash")
    assert Decimal(account.status()["new_reserved_cny"]) == Decimal("0.0028")
    outcome = account.settle(reservation.call_id, usage())
    assert Decimal(outcome["known_cny"]) == Decimal("0.0002408")
    status = account.status()
    assert Decimal(status["new_reserved_cny"]) == 0
    assert Decimal(status["new_known_cny"]) == Decimal("0.0002408")
    assert account.calls()[0]["usage"]["prompt_cache_hit_tokens"] == 20
    assert [event["kind"] for event in account.events()] == ["reserved", "settled"]
    with pytest.raises(BudgetAccountingError, match="once"):
        account.settle(reservation.call_id, usage())


def test_parallel_admission_never_overspends_shared_ceiling(tmp_path):
    path = tmp_path / "shared.sqlite"
    account = BudgetLedger(path, ceiling_cny="0.02", carried_cny=0)

    def reserve_one(_):
        competing = BudgetLedger(path, ceiling_cny="0.02", carried_cny=0)
        try:
            competing.reserve(1000, 250, stage="concurrent", model="deepseek-flash")
            return True
        except BudgetExceeded:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(reserve_one, range(24))) == 5
    assert Decimal(account.status()["all_committed_cny"]) == Decimal("0.02")
    assert account.status()["calls_sent"] == 5


def test_uncertain_bill_retains_reservation_and_survives_restart(tmp_path):
    account = ledger(tmp_path)
    reservation = account.reserve(1000, 100, stage="test", model="deepseek-flash")
    account.mark_unknown(reservation.call_id)
    reopened = ledger(tmp_path)
    assert reopened.status()["halted"]
    assert Decimal(reopened.status()["new_reserved_cny"]) == Decimal("0.0028")
    with pytest.raises(BudgetAccountingError, match="frozen"):
        reopened.reserve(1, 1, stage="test", model="deepseek-flash")


@pytest.mark.parametrize("bad_usage", [None, {}, {"prompt_tokens": True, "completion_tokens": 0},
    {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 7},
    {"prompt_tokens": 100, "completion_tokens": 2, "prompt_cache_hit_tokens": 80, "prompt_cache_miss_tokens": 30},
    {"prompt_tokens": 100, "completion_tokens": 2, "prompt_cache_hit_tokens": 20,
     "prompt_tokens_details": {"cached_tokens": 80}},
    {"prompt_tokens": 100, "completion_tokens": 2, "completion_tokens_details": {"reasoning_tokens": 3}},
])
def test_missing_or_inconsistent_usage_freezes_accounting(tmp_path, bad_usage):
    account = ledger(tmp_path)
    request = account.reserve(1000, 100, stage="test", model="deepseek-flash")
    with pytest.raises(BudgetAccountingError):
        account.settle(request.call_id, bad_usage)
    assert account.status()["halted"]
    assert account.calls()[0]["status"] == "unknown"


def test_usage_exceeding_bound_is_recorded_and_freezes_future_calls(tmp_path):
    account = ledger(tmp_path)
    request = account.reserve(100, 10, stage="test", model="deepseek-flash")
    with pytest.raises(BudgetAccountingError, match="exceeded"):
        account.settle(request.call_id, usage(prompt=101))
    assert account.status()["halted"]
    assert Decimal(account.status()["new_known_cny"]) > 0
    assert Decimal(account.status()["new_reserved_cny"]) == 0
    assert account.calls()[0]["status"] == "exceeded"


def test_no_cache_details_are_charged_at_full_input_rate():
    result = normalize_usage({"prompt_tokens": 90, "completion_tokens": 5})
    assert result["prompt_cache_miss_tokens"] == 90
    assert result["prompt_cache_hit_tokens"] == 0
    compatible = normalize_usage({"prompt_tokens": 90, "completion_tokens": 5,
                                  "prompt_tokens_details": {"cached_tokens": 80}})
    assert compatible["prompt_cache_hit_tokens"] == 80
    assert compatible["prompt_cache_miss_tokens"] == 10


def test_audit_events_and_configuration_reject_sql_mutation(tmp_path):
    account = ledger(tmp_path)
    account.reserve(1, 1, stage="test", model="deepseek-flash")
    with sqlite3.connect(account.path) as conn:
        for query in ("DELETE FROM events", "UPDATE events SET kind='forged'",
                      "DELETE FROM config", "UPDATE config SET carried_nanos=0"):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(query)


@pytest.mark.parametrize("amount", ["nan", "Infinity", "-1", "0.0000000001", True])
def test_invalid_budget_amount_rejected(tmp_path, amount):
    with pytest.raises(ValueError):
        ledger(tmp_path, ceiling=amount)


def test_pending_call_survives_process_restart_and_remains_committed(tmp_path):
    account = ledger(tmp_path)
    account.reserve(1000, 100, stage="test", model="deepseek-flash")
    reopened = ledger(tmp_path)
    assert reopened.status()["call_states"] == {"pending": 1}
    assert Decimal(reopened.status()["new_reserved_cny"]) == Decimal("0.0028")
