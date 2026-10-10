"""Persistent, conservative request admission and redacted cost accounting.

The ledger records configured-rate estimates, not the supplier's invoice. It
includes commitments from the earlier experiment, and retains the full request
reservation when a response or its usage cannot be verified.
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator, Mapping


NANO = Decimal(1_000_000_000)
RATES = {"input_miss_cny_per_million": "2", "input_hit_cny_per_million": "0.04",
         "output_cny_per_million": "8", "checked_at": "2026-10-10",
         "label": "configured conservative rates; not a supplier invoice"}
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,119}\Z")


class BudgetExceeded(RuntimeError):
    """The request was rejected before dispatch because its reservation cannot fit."""


class BudgetAccountingError(RuntimeError):
    """Uncertain billing or an exceeded reservation requires accounting review."""


def _money(value: Decimal | str | int | float, field: str) -> int:
    try:
        amount = Decimal(str(value))
        nanos = amount * NANO
        if not amount.is_finite() or amount < 0 or nanos != nanos.to_integral_value() or nanos > 10**17:
            raise ValueError
        return int(nanos)
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        raise ValueError(f"{field} must be a finite non-negative amount with at most 9 decimal places.") from None


def _cny(nanos: int) -> str:
    return format(Decimal(nanos) / NANO, "f")


def _integer(value: Any, field: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < int(positive):
        raise ValueError(f"{field} must be {'positive' if positive else 'non-negative'} integer tokens.")
    return value


def _label(value: str, field: str) -> str:
    if not isinstance(value, str) or not _LABEL.fullmatch(value):
        raise ValueError(f"{field} must be a short identifier, not arbitrary text.")
    return value


def normalize_usage(raw: Mapping[str, Any]) -> dict[str, int]:
    """Validate totals and the two common OpenAI/DeepSeek cache usage formats."""
    if not isinstance(raw, Mapping):
        raise ValueError("Missing token usage.")
    prompt = _integer(raw.get("prompt_tokens"), "prompt_tokens")
    output = _integer(raw.get("completion_tokens"), "completion_tokens")
    total = _integer(raw.get("total_tokens", prompt + output), "total_tokens")
    if total != prompt + output:
        raise ValueError("Inconsistent token total.")
    details = raw.get("prompt_tokens_details")
    if details is not None and not isinstance(details, Mapping):
        raise ValueError("Invalid prompt token details.")
    detailed_hit = details.get("cached_tokens") if details else None
    hit = raw.get("prompt_cache_hit_tokens", detailed_hit)
    miss = raw.get("prompt_cache_miss_tokens")
    if hit is None and miss is None:
        hit, miss = 0, prompt  # Missing cache information uses the higher rate.
    elif hit is None:
        miss = _integer(miss, "prompt_cache_miss_tokens")
        hit = prompt - miss
    elif miss is None:
        hit = _integer(hit, "prompt_cache_hit_tokens")
        miss = prompt - hit
    hit = _integer(hit, "prompt_cache_hit_tokens")
    miss = _integer(miss, "prompt_cache_miss_tokens")
    if hit + miss != prompt or (detailed_hit is not None and _integer(detailed_hit, "cached_tokens") != hit):
        raise ValueError("Inconsistent cached token counts.")
    completion_details = raw.get("completion_tokens_details")
    if completion_details is not None:
        if not isinstance(completion_details, Mapping):
            raise ValueError("Invalid completion token details.")
        reasoning = completion_details.get("reasoning_tokens")
        if reasoning is not None and _integer(reasoning, "reasoning_tokens") > output:
            raise ValueError("Reasoning tokens cannot exceed completion tokens.")
    return {"prompt_tokens": prompt, "completion_tokens": output, "total_tokens": total,
            "prompt_cache_hit_tokens": hit, "prompt_cache_miss_tokens": miss}


@dataclass(frozen=True)
class Reservation:
    call_id: str
    input_upper: int
    output_upper: int
    reserved_cny: str


class BudgetLedger:
    """SQLite transactions make admission atomic across processes and threads.

    Existing ledgers cannot be reopened with a different initial ceiling or
    carried commitment. Explicit budget increases are separate, append-only
    authorizations; they never rewrite historical commitments or usage.
    Audit events are append-only, including through normal SQL.
    Pending reservations survive process death; unknown outcomes freeze new
    calls. This object never stores prompts, completions, credentials or errors.
    """

    def __init__(self, path: str | Path, ceiling_cny: Any = 50, carried_cny: Any = "22.84835420") -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        ceiling, carried = _money(ceiling_cny, "ceiling_cny"), _money(carried_cny, "carried_cny")
        if carried > ceiling:
            raise ValueError("Carried commitments exceed the budget ceiling.")
        with self._connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS config (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    ceiling_nanos INTEGER NOT NULL, carried_nanos INTEGER NOT NULL,
                    rates_json TEXT NOT NULL, schema_version INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS calls (
                    call_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, stage TEXT NOT NULL,
                    model TEXT NOT NULL, input_upper INTEGER NOT NULL, output_upper INTEGER NOT NULL,
                    reserved_nanos INTEGER NOT NULL, status TEXT NOT NULL,
                    known_nanos INTEGER, usage_json TEXT, finished_at TEXT);
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
                    call_id TEXT NOT NULL, kind TEXT NOT NULL, data_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS authorizations (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT CHECK(sequence > 0),
                    authorization_id TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
                    previous_ceiling_nanos INTEGER NOT NULL CHECK(typeof(previous_ceiling_nanos)='integer'),
                    ceiling_nanos INTEGER NOT NULL CHECK(typeof(ceiling_nanos)='integer'),
                    reason TEXT NOT NULL CHECK(length(trim(reason)) BETWEEN 1 AND 1000),
                    CHECK(ceiling_nanos > previous_ceiling_nanos));
                CREATE TRIGGER IF NOT EXISTS config_no_replace BEFORE INSERT ON config
                    WHEN EXISTS(SELECT 1 FROM config WHERE singleton=NEW.singleton)
                    BEGIN SELECT RAISE(ABORT, 'Budget configuration is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS config_no_update BEFORE UPDATE ON config
                    BEGIN SELECT RAISE(ABORT, 'Budget configuration is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS config_no_delete BEFORE DELETE ON config
                    BEGIN SELECT RAISE(ABORT, 'Budget configuration is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
                    BEGIN SELECT RAISE(ABORT, 'Audit events are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
                    BEGIN SELECT RAISE(ABORT, 'Audit events are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS authorizations_no_update BEFORE UPDATE ON authorizations
                    BEGIN SELECT RAISE(ABORT, 'Budget authorizations are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS authorizations_no_delete BEFORE DELETE ON authorizations
                    BEGIN SELECT RAISE(ABORT, 'Budget authorizations are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS authorizations_no_replace BEFORE INSERT ON authorizations
                    WHEN EXISTS(SELECT 1 FROM authorizations
                        WHERE sequence=NEW.sequence OR authorization_id=NEW.authorization_id)
                    BEGIN SELECT RAISE(ABORT, 'Budget authorizations are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS authorizations_require_increase BEFORE INSERT ON authorizations
                    WHEN NOT EXISTS(SELECT 1 FROM config WHERE singleton=1)
                        OR NEW.previous_ceiling_nanos != COALESCE(
                            (SELECT ceiling_nanos FROM authorizations ORDER BY sequence DESC LIMIT 1),
                            (SELECT ceiling_nanos FROM config WHERE singleton=1))
                        OR NEW.ceiling_nanos <= NEW.previous_ceiling_nanos
                        OR (NEW.sequence != -1 AND NEW.sequence <= COALESCE((SELECT MAX(sequence) FROM authorizations),0))
                    BEGIN SELECT RAISE(ABORT, 'Authorization must strictly increase the current ceiling'); END;
            """)
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT * FROM config WHERE singleton=1").fetchone()
            if existing is None:
                conn.execute("INSERT INTO config VALUES (1, ?, ?, ?, 1)",
                             (ceiling, carried, json.dumps(RATES, sort_keys=True)))
                existing = conn.execute("SELECT * FROM config WHERE singleton=1").fetchone()
            if (existing["ceiling_nanos"], existing["carried_nanos"], existing["rates_json"], existing["schema_version"]) != (
                    ceiling, carried, json.dumps(RATES, sort_keys=True), 1):
                raise ValueError("Existing budget ceiling, carried commitments and rate schedule are immutable.")
            conn.commit()

    @classmethod
    def open(cls, path: str | Path) -> "BudgetLedger":
        """Reopen the stored authorization without accepting replacement limits."""
        resolved = Path(path).expanduser().resolve()
        if not resolved.is_file():
            raise FileNotFoundError("The budget ledger does not exist; initialize its authorization first.")
        try:
            with sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True, timeout=30) as conn:
                row = conn.execute("SELECT ceiling_nanos,carried_nanos FROM config WHERE singleton=1").fetchone()
        except sqlite3.DatabaseError:
            raise ValueError("The file is not a supported budget ledger.") from None
        if row is None:
            raise ValueError("The budget ledger has no persisted authorization.")
        return cls(resolved, ceiling_cny=_cny(row[0]), carried_cny=_cny(row[1]))

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA synchronous=FULL")
        try:
            yield connection
        finally:
            if connection.in_transaction:
                connection.rollback()
            connection.close()

    @staticmethod
    def _event(conn: sqlite3.Connection, call_id: str, kind: str, data: dict[str, Any]) -> None:
        conn.execute("INSERT INTO events(created_at,call_id,kind,data_json) VALUES(?,?,?,?)",
                     (datetime.now(UTC).isoformat(), call_id, kind, json.dumps(data, sort_keys=True)))

    @staticmethod
    def _totals(conn: sqlite3.Connection) -> tuple[int, int, int, int, bool]:
        config = conn.execute("SELECT * FROM config WHERE singleton=1").fetchone()
        authorization = conn.execute("SELECT ceiling_nanos FROM authorizations ORDER BY sequence DESC LIMIT 1").fetchone()
        ceiling = authorization["ceiling_nanos"] if authorization else config["ceiling_nanos"]
        sums = conn.execute("""SELECT COALESCE(SUM(known_nanos),0) AS known,
            COALESCE(SUM(CASE WHEN status IN ('pending','unknown') THEN reserved_nanos ELSE 0 END),0) AS reserved,
            COALESCE(SUM(CASE WHEN status IN ('unknown','exceeded') THEN 1 ELSE 0 END),0) AS halted
            FROM calls""").fetchone()
        return ceiling, config["carried_nanos"], sums["known"], sums["reserved"], bool(sums["halted"])

    def raise_ceiling(self, new_ceiling_cny: Any, reason: str) -> dict[str, Any]:
        """Record an explicitly authorized increase without changing prior costs.

        This is a host/operator action, never a model tool. The caller must have
        the user's authorization. Increasing the limit does not resolve uncertain
        billing, release pending reservations, or count as a chargeable request.
        Repeating an existing amount is rejected instead of creating duplicates.
        """
        ceiling = _money(new_ceiling_cny, "new_ceiling_cny")
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 1000 or "\x00" in reason:
            raise ValueError("A nonempty authorization reason of at most 1000 characters is required.")
        reason = reason.strip()
        if re.search(r"\bsk-[A-Za-z0-9_-]{12,}\b|\bBearer\s+\S+", reason, re.I):
            raise ValueError("An authorization reason must not contain credentials.")
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current, _, _, _, _ = self._totals(conn)
            if ceiling <= current:
                raise ValueError("The authorized ceiling must strictly exceed the current ceiling.")
            identity = "authorization_" + uuid.uuid4().hex
            created_at = datetime.now(UTC).isoformat()
            cursor = conn.execute("""INSERT INTO authorizations
                (authorization_id,created_at,previous_ceiling_nanos,ceiling_nanos,reason)
                VALUES(?,?,?,?,?)""", (identity, created_at, current, ceiling, reason))
            record = {"sequence": cursor.lastrowid, "authorization_id": identity, "created_at": created_at,
                      "previous_ceiling_cny": _cny(current), "ceiling_cny": _cny(ceiling), "reason": reason}
            self._event(conn, identity, "ceiling_raised", record)
            conn.commit()
        return record

    def authorizations(self) -> list[dict[str, Any]]:
        """Return immutable authorization history separately from model calls."""
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM authorizations ORDER BY sequence").fetchall()
        result = []
        for row in rows:
            record = dict(row)
            record["previous_ceiling_cny"] = _cny(record.pop("previous_ceiling_nanos"))
            record["ceiling_cny"] = _cny(record.pop("ceiling_nanos"))
            result.append(record)
        return result

    def reserve(self, input_upper: int, output_upper: int, *, stage: str, model: str) -> Reservation:
        _integer(input_upper, "input_upper", positive=True)
        _integer(output_upper, "output_upper", positive=True)
        _label(stage, "stage")
        _label(model, "model")
        amount = input_upper * 2000 + output_upper * 8000
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            ceiling, carried, known, reserved, halted = self._totals(conn)
            if halted:
                raise BudgetAccountingError("Billing is uncertain or exceeded its reservation; new requests are frozen.")
            if carried + known + reserved + amount > ceiling:
                raise BudgetExceeded("The next request's conservative reservation exceeds the remaining budget.")
            call_id = uuid.uuid4().hex
            conn.execute("""INSERT INTO calls(call_id,created_at,stage,model,input_upper,output_upper,reserved_nanos,status)
                VALUES(?,?,?,?,?,?,?,'pending')""", (call_id, datetime.now(UTC).isoformat(), stage, model,
                                                      input_upper, output_upper, amount))
            self._event(conn, call_id, "reserved", {"stage": stage, "model": model, "input_upper": input_upper,
                                                   "output_upper": output_upper, "reserved_cny": _cny(amount)})
            conn.commit()
        return Reservation(call_id, input_upper, output_upper, _cny(amount))

    def mark_unknown(self, call_id: str, reason: str = "transport_error") -> None:
        if reason not in {"transport_error", "invalid_or_missing_usage", "settlement_error", "interrupted"}:
            raise ValueError("Unknown accounting reason must be one of the predefined redacted categories.")
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status FROM calls WHERE call_id=?", (call_id,)).fetchone()
            if row is None or row["status"] != "pending":
                raise BudgetAccountingError("Only a pending request can become uncertain.")
            conn.execute("UPDATE calls SET status='unknown',finished_at=? WHERE call_id=?",
                         (datetime.now(UTC).isoformat(), call_id))
            self._event(conn, call_id, "unknown", {"reason": reason, "reservation_retained": True})
            conn.commit()

    def settle(self, call_id: str, usage: Mapping[str, Any]) -> dict[str, Any]:
        try:
            normalized = normalize_usage(usage)
        except (ValueError, TypeError):
            self.mark_unknown(call_id, "invalid_or_missing_usage")
            raise BudgetAccountingError("Response usage is missing or inconsistent; reservation retained and new calls frozen.") from None
        cost = (normalized["prompt_cache_miss_tokens"] * 2000 + normalized["prompt_cache_hit_tokens"] * 40
                + normalized["completion_tokens"] * 8000)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM calls WHERE call_id=?", (call_id,)).fetchone()
            if row is None or row["status"] != "pending":
                raise BudgetAccountingError("A request can be settled only once, from pending state.")
            exceeded = normalized["prompt_tokens"] > row["input_upper"] or normalized["completion_tokens"] > row["output_upper"]
            status = "exceeded" if exceeded else "settled"
            conn.execute("UPDATE calls SET status=?,known_nanos=?,usage_json=?,finished_at=? WHERE call_id=?",
                         (status, cost, json.dumps(normalized, sort_keys=True), datetime.now(UTC).isoformat(), call_id))
            result = {"call_id": call_id, "status": status, "known_cny": _cny(cost), "usage": normalized}
            self._event(conn, call_id, status, result)
            conn.commit()
        if exceeded:
            raise BudgetAccountingError("Provider token usage exceeded its reservation; usage recorded and new calls frozen.")
        return result

    def status(self) -> dict[str, Any]:
        with self._connection() as conn:
            conn.execute("BEGIN")
            ceiling, carried, known, reserved, halted = self._totals(conn)
            counts = conn.execute("SELECT status,COUNT(*) AS count FROM calls GROUP BY status").fetchall()
            initial_ceiling = conn.execute("SELECT ceiling_nanos FROM config WHERE singleton=1").fetchone()[0]
            authorizations = conn.execute("SELECT COUNT(*) FROM authorizations").fetchone()[0]
        return {"ceiling_cny": _cny(ceiling), "carried_cny": _cny(carried), "new_known_cny": _cny(known),
                "initial_ceiling_cny": _cny(initial_ceiling), "authorization_count": authorizations,
                "new_reserved_cny": _cny(reserved), "all_committed_cny": _cny(carried + known + reserved),
                "remaining_cny": _cny(ceiling - carried - known - reserved), "halted": halted,
                "calls_sent": sum(row["count"] for row in counts),
                "call_states": {row["status"]: row["count"] for row in counts},
                "rates": dict(RATES), "account_bill_exact": False}

    def calls(self) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM calls ORDER BY created_at,call_id").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["reserved_cny"] = _cny(item.pop("reserved_nanos"))
            known = item.pop("known_nanos")
            item["known_cny"] = _cny(known) if known is not None else None
            raw_usage = item.pop("usage_json")
            item["usage"] = json.loads(raw_usage) if raw_usage else None
            result.append(item)
        return result

    def events(self) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM events ORDER BY sequence").fetchall()
        result = []
        for row in rows:
            event = dict(row)
            event["data"] = json.loads(event.pop("data_json"))
            result.append(event)
        return result
