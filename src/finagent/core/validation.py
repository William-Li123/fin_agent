"""Deterministic output contract. Semantic financial correctness remains an evaluation concern."""
from __future__ import annotations
import json
import math
import re
from decimal import Decimal


def parse_action(text: str) -> dict:
    value = text.strip()
    if value.startswith("```") and value.endswith("```"):
        value = value.split("\n", 1)[1].rsplit("```", 1)[0]
    parsed = json.loads(value, parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))
    if not isinstance(parsed, dict) or parsed.get("action") not in {"tool", "skill", "finish"}:
        raise ValueError("Return one JSON object with action tool, skill, or finish")
    return parsed


def validate_final(final: dict, observed: set[str], calculations: dict[str, dict]) -> dict:
    if not isinstance(final, dict):
        raise ValueError("final must be an object")
    status = final.get("status", "answered")
    if status not in {"answered", "insufficient_evidence"}:
        raise ValueError("Invalid final status")
    sources = final.get("sources")
    if not isinstance(sources, list) or not all(isinstance(s, str) for s in sources) or len(set(sources)) != len(sources):
        raise ValueError("sources must contain unique observed evidence IDs")
    if set(sources) - observed:
        raise ValueError("Final cites unobserved evidence; retrieve the records first")
    if status == "answered" and not sources:
        raise ValueError("Answered financial claims require observed source evidence")
    answer = final.get("answer")
    if status == "insufficient_evidence" and answer is not None:
        raise ValueError("insufficient_evidence requires a null answer")
    if isinstance(answer, bool) or isinstance(answer, (list, dict)) or isinstance(answer, float) and not math.isfinite(answer):
        raise ValueError("answer must be a finite number, text, or null")
    if status == "answered" and (answer is None or isinstance(answer, str) and not answer.strip()):
        raise ValueError("An answered result needs a nonempty answer")
    if isinstance(answer, str):
        if re.fullmatch(r"[+-]?(?:nan|s?nan\d*|inf(?:inity)?)", answer.strip(), re.I):
            raise ValueError("Nonfinite answer is invalid")
        if re.fullmatch(r"[\s$€£¥+\-()\d,.%]+(?:\s*(?:percent|bps|million|billion|元|万元|亿元))?", answer, re.I) and re.search(r"\d", answer):
            try:
                Decimal(answer)
            except Exception as exc:
                raise ValueError("Use a canonical numeric answer and put formatting/scale in unit") from exc
    if not isinstance(final.get("explanation"), str) or not final["explanation"].strip():
        raise ValueError("Explain the conclusion or missing evidence")
    calc_id = final.get("calculation_id")
    if calc_id:
        if calc_id not in calculations:
            raise ValueError("calculation_id must come from a successful observed finance calculation")
        calc = calculations[calc_id]
        if "value" not in calc:
            raise ValueError("Use finance_calculate to produce a single value receipt for the final answer")
        if not calc.get("evidence_ids"):
            raise ValueError("Final calculation needs observed evidence_ids for its inputs")
        if not set(calc.get("evidence_ids", [])) <= set(sources):
            raise ValueError("Final must cite every source used by its calculation")
        if "value" in calc:
            try:
                actual, expected = Decimal(str(answer)), Decimal(str(calc["value"]))
            except Exception as exc:
                raise ValueError("A computed answer must be numeric; put its units in unit") from exc
            tolerance = max(Decimal("0.00001"), abs(expected) * Decimal("0.000001"))
            if not actual.is_finite() or abs(actual - expected) > tolerance:
                raise ValueError("Answer disagrees with the cited tool calculation; preserve its scale")
    numeric = isinstance(answer, (float, int))
    if isinstance(answer, str):
        try:
            numeric = Decimal(answer).is_finite()
        except Exception:
            pass
    if status == "answered" and numeric and not calc_id:
        raise ValueError("Numeric answers require a finance_calculate receipt; even copied values can use value/1")
    return {**final, "status": status}
