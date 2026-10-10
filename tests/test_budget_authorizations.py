"""Increasing an explicit spending authorization must never reset accounting."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import json
import sqlite3

import pytest

from finagent.models import BudgetAccountingError, BudgetExceeded, BudgetLedger
from finagent.models.budget import RATES


def test_raise_to_one_hundred_preserves_every_prior_commitment(tmp_path):
    account = BudgetLedger(tmp_path / "budget.sqlite")
    billed = account.reserve(1000, 100, stage="previous.run", model="deepseek-flash")
    account.settle(billed.call_id, {"prompt_tokens": 100, "completion_tokens": 10})
    account.reserve(1000, 100, stage="pending.run", model="deepseek-flash")
    before = account.status()
    calls_before, events_before = account.calls(), account.events()
    authorization = account.raise_ceiling(100, "User explicitly increased the cumulative budget to CNY 100.")
    after = account.status()
    assert after["ceiling_cny"] == "100"
    assert after["initial_ceiling_cny"] == "50"
    assert after["authorization_count"] == 1
    for field in ("carried_cny", "new_known_cny", "new_reserved_cny", "all_committed_cny", "calls_sent", "call_states", "halted"):
        assert after[field] == before[field]
    assert Decimal(after["remaining_cny"]) - Decimal(before["remaining_cny"]) == 50
    assert account.calls() == calls_before
    assert account.events()[:-1] == events_before
    assert account.events()[-1]["kind"] == "ceiling_raised"
    assert account.events()[-1]["data"] == authorization
    assert account.authorizations() == [authorization]
    assert BudgetLedger.open(account.path).status() == after
    # The initialization contract still describes the immutable original grant.
    assert BudgetLedger(account.path).status() == after
    with pytest.raises(ValueError, match="immutable"):
        BudgetLedger(account.path, ceiling_cny=100)
    with sqlite3.connect(account.path) as conn:
        assert conn.execute("SELECT ceiling_nanos,carried_nanos FROM config").fetchone() == (50_000_000_000, 22_848_354_200)


def create_legacy_ledger(path):
    """Actual old schema: no authorization table or migration marker."""
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE config(singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                ceiling_nanos INTEGER NOT NULL,carried_nanos INTEGER NOT NULL,rates_json TEXT NOT NULL,schema_version INTEGER NOT NULL);
            CREATE TABLE calls(call_id TEXT PRIMARY KEY,created_at TEXT NOT NULL,stage TEXT NOT NULL,
                model TEXT NOT NULL,input_upper INTEGER NOT NULL,output_upper INTEGER NOT NULL,reserved_nanos INTEGER NOT NULL,
                status TEXT NOT NULL,known_nanos INTEGER,usage_json TEXT,finished_at TEXT);
            CREATE TABLE events(sequence INTEGER PRIMARY KEY AUTOINCREMENT,created_at TEXT NOT NULL,
                call_id TEXT NOT NULL,kind TEXT NOT NULL,data_json TEXT NOT NULL);
            CREATE TRIGGER config_no_update BEFORE UPDATE ON config
                BEGIN SELECT RAISE(ABORT, 'Budget configuration is immutable'); END;
            CREATE TRIGGER config_no_delete BEFORE DELETE ON config
                BEGIN SELECT RAISE(ABORT, 'Budget configuration is immutable'); END;
        """)
        conn.execute("INSERT INTO config VALUES(1,?,?,?,1)",
                     (50_000_000_000, 22_848_354_200, json.dumps(RATES, sort_keys=True)))
        conn.execute("INSERT INTO calls VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
            "legacy-settled", "2026-10-10T00:00:00+00:00", "old.run", "deepseek-flash", 1000, 100,
            2_800_000, "settled", 240800, '{"prompt_tokens":100,"completion_tokens":10}', "2026-10-10T00:00:01+00:00"))
        conn.execute("INSERT INTO calls VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
            "legacy-pending", "2026-10-10T00:00:02+00:00", "old.pending", "deepseek-flash", 1000, 100,
            2_800_000, "pending", None, None, None))
        conn.execute("INSERT INTO events(created_at,call_id,kind,data_json) VALUES(?,?,?,?)",
                     ("2026-10-10T00:00:01+00:00", "legacy-settled", "settled", '{"legacy_record":true}'))


def test_old_ledger_is_migrated_additively_without_rewriting_old_rows(tmp_path):
    path = tmp_path / "old.sqlite"
    create_legacy_ledger(path)
    with sqlite3.connect(path) as conn:
        before = {table: conn.execute(f"SELECT * FROM {table}").fetchall() for table in ("config", "calls", "events")}
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='authorizations'").fetchone() is None
    account = BudgetLedger.open(path)
    assert account.status()["ceiling_cny"] == "50"
    assert account.status()["authorization_count"] == 0
    assert account.status()["all_committed_cny"] == "22.851395"
    with sqlite3.connect(path) as conn:
        for table, rows in before.items():
            assert conn.execute(f"SELECT * FROM {table}").fetchall() == rows
    account.raise_ceiling("100", "User authorized the new cumulative limit.")
    assert account.status()["all_committed_cny"] == "22.851395"
    assert BudgetLedger.open(path).status()["ceiling_cny"] == "100"
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT * FROM config").fetchall() == before["config"]
        assert conn.execute("SELECT * FROM calls").fetchall() == before["calls"]
        assert conn.execute("SELECT * FROM events WHERE sequence=1").fetchall() == before["events"]


@pytest.mark.parametrize("rejected", [100, 99, 50, 0])
def test_repeating_or_lowering_authorization_is_rejected_without_new_events(tmp_path, rejected):
    account = BudgetLedger(tmp_path / "budget.sqlite")
    account.raise_ceiling(100, "Explicit user authorization.")
    before, events = account.status(), account.events()
    with pytest.raises(ValueError, match="strictly exceed"):
        account.raise_ceiling(rejected, "This must not replace the active authorization.")
    assert account.status() == before
    assert account.events() == events


def test_authorization_history_rejects_sql_changes_deletion_and_replacement(tmp_path):
    account = BudgetLedger(tmp_path / "budget.sqlite")
    authorization = account.raise_ceiling(100, "Explicit user authorization.")
    with sqlite3.connect(account.path) as conn:
        for query in ("UPDATE authorizations SET reason='rewritten'", "DELETE FROM authorizations"):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(query)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("""INSERT OR REPLACE INTO authorizations
                (sequence,authorization_id,created_at,previous_ceiling_nanos,ceiling_nanos,reason)
                VALUES(?,?,?,?,?,?)""", (authorization["sequence"], authorization["authorization_id"], "forged",
                                        100_000_000_000, 200_000_000_000, "Replacement is forbidden."))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT OR REPLACE INTO config VALUES(1,200000000000,0,?,1)", (json.dumps(RATES, sort_keys=True),))
    assert account.authorizations() == [authorization]
    assert account.status()["ceiling_cny"] == "100"


def test_effective_ceiling_admits_exact_boundary_including_old_commitments(tmp_path):
    account = BudgetLedger(tmp_path / "budget.sqlite", ceiling_cny=50, carried_cny="49.996")
    account.reserve(1000, 250, stage="boundary.old", model="deepseek-flash")
    assert account.status()["all_committed_cny"] == "50"
    with pytest.raises(BudgetExceeded):
        account.reserve(1, 1, stage="boundary.rejected", model="deepseek-flash")
    account.raise_ceiling(100, "User approved cumulative CNY 100.")
    # Ledger-level synthetic token counts exercise exact fixed-point arithmetic;
    # no SDK request is created, and client-specific token limits are separate.
    account.reserve(1_000_000, 6_000_000, stage="boundary.new", model="deepseek-flash")
    assert account.status()["all_committed_cny"] == "100"
    assert account.status()["remaining_cny"] == "0"
    with pytest.raises(BudgetExceeded):
        account.reserve(1, 1, stage="boundary.exceeded", model="deepseek-flash")
    assert account.status()["calls_sent"] == 2


def test_increasing_limit_does_not_release_unknown_billing_freeze(tmp_path):
    account = BudgetLedger(tmp_path / "budget.sqlite")
    request = account.reserve(1000, 100, stage="unknown", model="deepseek-flash")
    account.mark_unknown(request.call_id)
    before = account.status()
    account.raise_ceiling(100, "User increased the limit; accounting uncertainty is unchanged.")
    assert account.status()["halted"]
    assert account.status()["new_reserved_cny"] == before["new_reserved_cny"]
    assert account.status()["all_committed_cny"] == before["all_committed_cny"]
    with pytest.raises(BudgetAccountingError, match="frozen"):
        account.reserve(1, 1, stage="still.frozen", model="deepseek-flash")


def test_concurrent_identical_increases_create_exactly_one_authorization(tmp_path):
    account = BudgetLedger(tmp_path / "budget.sqlite")

    def increase(_):
        try:
            BudgetLedger.open(account.path).raise_ceiling(100, "The same explicit user authorization.")
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=6) as pool:
        assert sum(pool.map(increase, range(12))) == 1
    assert account.status()["authorization_count"] == 1
    assert account.status()["calls_sent"] == 0
    assert account.status()["all_committed_cny"] == "22.8483542"
    assert len(account.events()) == 1


def test_successive_increases_form_an_unbroken_auditable_chain(tmp_path):
    account = BudgetLedger(tmp_path / "budget.sqlite")
    first = account.raise_ceiling(75, "First explicit test authorization.")
    second = account.raise_ceiling(100, "Second explicit test authorization.")
    assert first["previous_ceiling_cny"] == "50"
    assert second["previous_ceiling_cny"] == first["ceiling_cny"] == "75"
    assert second["ceiling_cny"] == "100"
    assert account.authorizations() == [first, second]
    assert BudgetLedger.open(account.path).status()["authorization_count"] == 2
    assert account.status()["all_committed_cny"] == "22.8483542"


def test_failed_audit_write_rolls_back_authorization_atomically(tmp_path):
    account = BudgetLedger(tmp_path / "budget.sqlite")
    with sqlite3.connect(account.path) as conn:
        conn.execute("""CREATE TRIGGER fail_test_authorization_audit BEFORE INSERT ON events
            WHEN NEW.kind='ceiling_raised' BEGIN SELECT RAISE(ABORT, 'controlled audit failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="controlled audit failure"):
        account.raise_ceiling(100, "User increased the limit.")
    assert account.status()["ceiling_cny"] == "50"
    assert account.authorizations() == []
    assert account.events() == []


@pytest.mark.parametrize("reason", ["", " \t ", None, "x" * 1001, "contains\x00null", "sk-example-secret-token-long"])
def test_invalid_or_credential_bearing_reason_is_not_persisted(tmp_path, reason):
    account = BudgetLedger(tmp_path / "budget.sqlite")
    with pytest.raises(ValueError):
        account.raise_ceiling(100, reason)
    assert account.authorizations() == []
    assert account.events() == []
