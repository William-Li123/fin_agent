"""Offline evidence-first finance tools with bounded Decimal arithmetic."""
from __future__ import annotations
import ast
import json
import re
from decimal import Decimal, InvalidOperation, localcontext
from html import escape
from typing import Any, Mapping
from .base import BaseTool
from .registry import ToolRegistry
from .schemas import ToolContentItem, ToolDefinition, ToolResult, ToolSource
from .store import FinanceStore


def decimal_value(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("Expected a finite numeric value.")
    text = str(value).strip().replace("−", "-")
    if len(text) > 200:
        raise ValueError("Numeric text exceeds the supported length.")
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    if "," in text:
        if not re.fullmatch(r"[+-]?\d{1,3}(?:,\d{3})+(?:\.\d*)?(?:[eE][+-]?\d+)?", text):
            raise ValueError("Commas must delimit three-digit thousands groups.")
        text = text.replace(",", "")
    try:
        result = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError("Invalid numeric value; missing values cannot be treated as zero.") from exc
    if not result.is_finite() or result.copy_abs() > Decimal("1e100") or abs(result.as_tuple().exponent) > 200:
        raise ValueError("Numeric value must be finite and within range.")
    return result


def decimal_text(value: Decimal) -> str:
    if not value.is_finite() or value.copy_abs() > Decimal("1e100") or abs(value.as_tuple().exponent) > 240:
        raise ValueError("Arithmetic result is outside the supported range.")
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def safe_calculate(expression: str, variables: Mapping[str, Any] | None = None) -> Decimal:
    """Interpret arithmetic AST without eval/compile/execution."""
    if not isinstance(expression, str) or not expression.strip() or len(expression) > 2000:
        raise ValueError("Expression must be a nonempty arithmetic string, at most 2000 characters.")
    values = dict(variables or {})
    if len(values) > 64 or any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", k) for k in values):
        raise ValueError("Invalid variable names/count.")
    values = {k: decimal_value(v) for k, v in values.items()}
    try:
        tree = ast.parse(expression, mode="eval")
    except (SyntaxError, RecursionError) as exc:
        raise ValueError("Invalid arithmetic expression.") from exc
    if len(list(ast.walk(tree))) > 256:
        raise ValueError("Arithmetic expression is too complex.")

    def visit(node: ast.AST, depth: int = 0) -> Decimal:
        if depth > 32:
            raise ValueError("Arithmetic expression nesting is too deep.")
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return decimal_value(ast.get_source_segment(expression, node))
        if isinstance(node, ast.Name) and node.id in values:
            return values[node.id]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = visit(node.operand, depth + 1)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)):
            left, right = visit(node.left, depth + 1), visit(node.right, depth + 1)
            if isinstance(node.op, ast.Add):
                result = left + right
            elif isinstance(node.op, ast.Sub):
                result = left - right
            elif isinstance(node.op, ast.Mult):
                result = left * right
            elif isinstance(node.op, ast.Div):
                if right == 0:
                    raise ValueError("Division by zero is undefined.")
                result = left / right
            else:
                if right != right.to_integral_value() or abs(right) > 12 or (left == 0 and right <= 0):
                    raise ValueError("Exponent must be an integer between -12 and 12; zero cannot have a nonpositive exponent.")
                result = left ** int(right)
            decimal_text(result)
            return result
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
            if node.func.id not in {"abs", "min", "max"} or not 1 <= len(node.args) <= 32:
                raise ValueError("Only bounded abs/min/max functions are supported.")
            args = [visit(x, depth + 1) for x in node.args]
            if node.func.id == "abs":
                if len(args) != 1:
                    raise ValueError("abs takes exactly one value.")
                return abs(args[0])
            return min(args) if node.func.id == "min" else max(args)
        raise ValueError("Unsupported arithmetic syntax; executable code is unavailable.")

    with localcontext() as context:
        context.prec = 40
        try:
            return visit(tree.body)
        except (InvalidOperation, OverflowError) as exc:
            raise ValueError("Arithmetic operation is undefined or outside range.") from exc


UNIT_SCALE = {"number": Decimal(1), "元": Decimal(1), "yuan": Decimal(1),
              "万元": Decimal(10000), "亿元": Decimal(100000000), "thousand": Decimal(1000),
              "million": Decimal(1000000), "billion": Decimal(1000000000), "ratio": Decimal(1),
              "percent": Decimal("0.01"), "%": Decimal("0.01"),
              "bps": Decimal("0.0001"), "basis_points": Decimal("0.0001")}
RATIO_UNITS = {"ratio", "percent", "%", "bps", "basis_points"}
UNIT_ALIASES = {"%": "percent", "basis_points": "bps"}


def normalize_value(value: str, unit: str = "number", target_unit: str = "number",
                    currency: str | None = None) -> dict[str, Any]:
    if not isinstance(value, str) or len(value) > 200:
        raise ValueError("value must be a numeric string.")
    unit = UNIT_ALIASES.get(unit, unit)
    target_unit = UNIT_ALIASES.get(target_unit, target_unit)
    text, detected_currency = value.strip(), None
    detected_codes = set()
    for mark, code in {"$": "USD", "€": "EUR", "£": "GBP", "￥": "CNY", "¥": "CNY"}.items():
        if mark in text:
            detected_codes.add(code)
            detected_currency, text = code, text.replace(mark, "")
    code_match = re.search(r"\b(USD|EUR|GBP|CNY|RMB)\b", text, re.I)
    if code_match:
        code = code_match.group(1).upper()
        detected_currency = "CNY" if code == "RMB" else code
        detected_codes.add(detected_currency)
        text = text[:code_match.start()] + text[code_match.end():]
    if len(detected_codes) > 1:
        raise ValueError("Conflicting currency marks/codes.")
    if currency is not None:
        if not isinstance(currency, str) or currency.upper() not in {"USD", "EUR", "GBP", "CNY"}:
            raise ValueError("Unsupported currency; exchange conversion is unavailable.")
        currency = currency.upper()
        if detected_currency and detected_currency != currency:
            raise ValueError("Currency conversion requires an external exchange rate.")
    suffix = re.search(r"\s*(billion|million|thousand|basis_points|bps|percent|万元|亿元|元|%)\s*$", text, re.I)
    if suffix:
        detected_unit = UNIT_ALIASES.get(suffix.group(1).lower(), suffix.group(1).lower())
        text = text[:suffix.start()]
        if unit != "number" and unit != detected_unit:
            raise ValueError("Explicit unit conflicts with the numeric suffix.")
        unit = detected_unit
    if unit not in UNIT_SCALE or target_unit not in UNIT_SCALE:
        raise ValueError("Unsupported unit.")
    yuan_units = {"元", "yuan", "万元", "亿元"}
    effective_currency = currency or detected_currency
    if yuan_units.intersection({unit, target_unit}):
        if effective_currency not in {None, "CNY"}:
            raise ValueError("Yuan units cannot convert a different currency without an exchange rate.")
        effective_currency = "CNY"
    if effective_currency and RATIO_UNITS.intersection({unit, target_unit}):
        raise ValueError("Currency amounts cannot be converted into ratios.")
    if (unit in RATIO_UNITS) != (target_unit in RATIO_UNITS) and "number" not in {unit, target_unit}:
        raise ValueError("Cannot convert monetary amounts into ratios.")
    with localcontext() as context:
        context.prec = 40
        result = decimal_value(text) * UNIT_SCALE[unit] / UNIT_SCALE[target_unit]
    return {"value": decimal_text(result), "input_unit": unit, "unit": target_unit,
            "currency": effective_currency, "exchange_rate_applied": False}


def _validate(arguments: Mapping[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, Mapping):
        raise ValueError("Tool arguments must be an object.")
    args, properties = dict(arguments), schema["properties"]
    if set(args).difference(properties) or set(schema.get("required", [])).difference(args):
        raise ValueError("Unknown or missing required tool argument.")
    for key, value in args.items():
        spec, kind = properties[key], properties[key].get("type")
        if kind == "string" and not isinstance(value, str):
            raise ValueError(f"{key} must be a string.")
        if kind == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
            raise ValueError(f"{key} must be an integer.")
        if kind == "array":
            if not isinstance(value, list):
                raise ValueError(f"{key} must be an array.")
            if len(value) > spec.get("maxItems", 256):
                raise ValueError(f"{key} has too many items.")
            item_type = spec.get("items", {}).get("type")
            if item_type == "string" and not all(isinstance(x, str) for x in value):
                raise ValueError(f"{key} must contain strings.")
            if item_type == "integer" and not all(isinstance(x, int) and not isinstance(x, bool) for x in value):
                raise ValueError(f"{key} must contain integers.")
        if kind == "object" and not isinstance(value, dict):
            raise ValueError(f"{key} must be an object.")
        if kind == "integer" and (value < spec.get("minimum", value) or value > spec.get("maximum", value)):
            raise ValueError(f"{key} is outside the permitted range.")
    return args


class FinanceTool(BaseTool):
    def __init__(self, definition: ToolDefinition, store: FinanceStore, handler: Any) -> None:
        super().__init__(definition)
        self.store, self.handler = store, handler

    async def ainvoke(self, arguments: Mapping[str, Any] | None = None) -> ToolResult:
        try:
            args = _validate(arguments if arguments is not None else {}, self.input_schema)
            result = self.handler(args)
            return ToolResult(tool_name=self.name, success=True, structured_content=result,
                              content=[ToolContentItem(text=json.dumps(result, ensure_ascii=False))])
        except (ValueError, TypeError, KeyError, OSError, InvalidOperation) as exc:
            return ToolResult(tool_name=self.name, success=False, error=str(exc),
                              content=[ToolContentItem(text=f"{self.name} failed: {exc}")])


def build_finance_tools(*, store: FinanceStore) -> list[BaseTool]:
    evidence_schema, string_schema = {"type": "array", "items": {"type": "string"}}, {"type": "string"}
    document_schema = {"type": "string", "description": "A document id in the permitted task scope."}
    tools: list[BaseTool] = []
    def add(name: str, description: str, properties: dict[str, Any], required: list[str], handler: Any) -> None:
        tools.append(FinanceTool(ToolDefinition(name="finance_" + name, description=description,
                     source=ToolSource.LOCAL, input_schema={"type": "object", "properties": properties,
                     "required": required, "additionalProperties": False},
                     metadata={"profile": "finance", "offline": True}), store, handler))
    def ids(args: dict[str, Any]) -> list[str]:
        return store.validate_evidence_ids(args.get("evidence_ids", []))

    def lexical_terms(text: str) -> set[str]:
        words = set(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]", text.lower()))
        variants = set(words)
        for word in words:
            if len(word) > 4 and word.endswith("ies"):
                variants.add(word[:-3] + "y")
            elif len(word) > 4 and word.endswith("s") and not word.endswith("ss"):
                variants.add(word[:-1])
            for candidate in (word, word[:-1] if word.endswith("s") else word):
                if len(candidate) > 5 and candidate.endswith("ing"):
                    variants.add(candidate[:-3])
                elif len(candidate) > 5 and candidate.endswith("ion"):
                    variants.add(candidate[:-3])
        return variants

    def search(args: dict[str, Any]) -> dict[str, Any]:
        query = args["query"].strip()
        if not query or len(query) > 1000:
            raise ValueError("Query must be nonempty and at most 1000 characters.")
        selected = args.get("document_id")
        docs = [store.get_document(selected)["id"]] if selected else [x["document_id"] for x in store.list_documents()]
        terms = lexical_terms(query)
        terms -= {"the", "a", "an", "of", "in", "and", "to", "what", "was", "is", "for", "by", "with"}
        hits = []
        for doc_id in docs:
            evidence = store.evidence_for_document(doc_id)
            paragraphs = {(item["section"], item["paragraph"]): item for item in evidence
                          if item["section"] in {"pre_text", "post_text"}}
            for item in evidence:
                body = item["body"].lower()
                body_terms = lexical_terms(body)
                core_terms = {term for term in terms if not term.isdigit()}
                core_matches = len(core_terms.intersection(body_terms))
                year_terms = {term for term in terms if re.fullmatch(r"(?:19|20)\d{2}", term)}
                matches = terms.intersection(body_terms)
                score = (2 * core_matches + len(matches - core_terms - year_terms)
                         + 3 * len(matches.intersection(year_terms))) if not core_terms or core_matches else 0
                if query.lower() in body:
                    score += 3
                if score:
                    hit = dict(item)
                    if item["section"] in {"pre_text", "post_text"}:
                        section, index = item["section"], item["paragraph"]
                        def brief(paragraph):
                            return {key: paragraph[key] for key in ("evidence_id", "locator", "text")}
                        hit["context_before"] = [brief(paragraphs[(section, i)])
                                                 for i in range(max(0, index - 3), index)]
                        hit["context_after"] = [brief(paragraphs[(section, index + 1)])] if (section, index + 1) in paragraphs else []
                        # Preserve an actual preceding year heading, not a guessed
                        # fiscal period. Extracted disclosures can split headings across lines.
                        for previous in range(index - 1, -1, -1):
                            candidate = paragraphs[(section, previous)]
                            if re.match(r"^\s*(?:19|20)\d{2}\s*(?:vs\b|outlook\b)", candidate["text"], re.I):
                                hit["context_heading"] = brief(candidate)
                                heading_matches = terms.intersection(re.findall(r"[a-z0-9]+", candidate["text"].lower()))
                                score += len(heading_matches) + 2 * len(heading_matches.intersection(year_terms))
                                break
                    hits.append({**hit, "score": score})
        hits.sort(key=lambda x: (-x["score"], x["document_id"], x["locator"]))
        return {"query": query, "hits": hits[:args.get("top_k", 5)], "scoped_document_ids": docs}

    def read_table(args: dict[str, Any]) -> dict[str, Any]:
        doc = store.get_document(args.get("document_id"))
        table = doc["table"]
        start, end = args.get("row_start", 0), args.get("row_end", len(table))
        if end < start or start > len(table) or end > len(table):
            raise ValueError(f"Invalid table row range: require 0 <= row_start <= row_end <= {len(table)}; "
                             "row_end is exclusive. Omit row_end to read all rows.")
        columns = args.get("columns")
        if columns is not None and (len(columns) != len(set(columns)) or any(x < 0 for x in columns)):
            raise ValueError("Invalid or duplicated column index.")
        evidence = {(x["row"], x["column"]): x for x in store.evidence_for_document(doc["id"]) if x["section"] == "table"}
        rows = []
        for row in range(start, end):
            cols = columns if columns is not None else list(range(len(table[row])))
            if any(col >= len(table[row]) for col in cols):
                raise ValueError("Column index is outside the table row.")
            rows.append({"row": row, "cells": [evidence[(row, col)] for col in cols]})
        return {"document_id": doc["id"], "index_base": 0, "row_end_exclusive": True, "rows": rows}

    def calculate(args: dict[str, Any]) -> dict[str, Any]:
        references = ids(args)
        value = safe_calculate(args["expression"], args.get("variables"))
        variables = {key: decimal_text(decimal_value(value)) for key, value in args.get("variables", {}).items()}
        result = {"expression": args["expression"], "variables": variables,
                  "value": decimal_text(value), "evidence_ids": references}
        result["calculation_id"] = store.save_calculation(result)
        return result

    def normalize(args: dict[str, Any]) -> dict[str, Any]:
        references = ids(args)
        result = normalize_value(args["value"], args.get("unit", "number"),
                                 args.get("target_unit", "number"), args.get("currency"))
        result["evidence_ids"] = references
        result["calculation_id"] = store.save_calculation({"operation": "normalize_value", **result})
        return result

    def compare(args: dict[str, Any]) -> dict[str, Any]:
        references = ids(args)
        current, previous = decimal_value(args["current"]), decimal_value(args["previous"])
        with localcontext() as context:
            context.prec = 40
            delta = current - previous
            percentage = None if previous == 0 else decimal_text(delta / previous * Decimal(100))
        result = {"current": decimal_text(current), "previous": decimal_text(previous),
                  "delta": decimal_text(delta), "change_percent": percentage,
                  "formula": "(current - previous) / previous * 100", "evidence_ids": references}
        if previous == 0:
            result["change_percent_status"] = "undefined: previous period equals zero"
        result["calculation_id"] = store.save_calculation({"operation": "compare_periods", **result})
        return result

    def render_chart(args: dict[str, Any]) -> dict[str, Any]:
        references = ids(args)
        labels, values = args["labels"], [decimal_value(x) for x in args["values"]]
        if len(labels) != len(values) or not 1 <= len(values) <= 24:
            raise ValueError("Chart requires 1-24 matching labels and values.")
        if len(args["title"]) > 160 or any(len(x) > 120 for x in labels):
            raise ValueError("Chart title/labels are too long.")
        with localcontext() as context:
            context.prec = 40
            low, high = min(min(values), Decimal(0)), max(max(values), Decimal(0))
            if high == low:
                high = low + 1
            def y(value: Decimal) -> Decimal:
                return Decimal(340) - (value - low) / (high - low) * Decimal(260)
            zero, step = y(Decimal(0)), Decimal(620) / len(values)
            svg = ['<svg xmlns="http://www.w3.org/2000/svg" width="760" height="440" viewBox="0 0 760 440">',
                   '<rect width="760" height="440" fill="white"/>',
                   f'<text x="40" y="35" font-family="sans-serif" font-size="18">{escape(args["title"])}</text>',
                   f'<line x1="70" y1="{zero:.2f}" x2="710" y2="{zero:.2f}" stroke="#475569"/>',
                   f'<text x="10" y="85" font-size="11">{escape(decimal_text(high))}</text>',
                   f'<text x="10" y="343" font-size="11">{escape(decimal_text(low))}</text>']
            for index, (label, value) in enumerate(zip(labels, values)):
                x, target_y = Decimal(80) + step * index, y(value)
                top, height, width = min(zero, target_y), abs(zero - target_y), step * Decimal("0.65")
                svg.extend([f'<rect x="{x:.2f}" y="{top:.2f}" width="{width:.2f}" height="{height:.2f}" fill="#2563eb"/>',
                            f'<text x="{x:.2f}" y="{max(Decimal(65), top - 6):.2f}" font-size="10">{escape(decimal_text(value))}</text>',
                            f'<text transform="translate({x:.2f},365) rotate(25)" font-size="10">{escape(label[:40])}</text>'])
            svg.append("</svg>")
        return store.write_artifact(kind="chart", content="\n".join(svg), extension=".svg",
                                    evidence_ids=references, filename=args.get("filename"))

    def write_report(args: dict[str, Any]) -> dict[str, Any]:
        references = ids(args)
        if not references:
            raise ValueError("Report requires at least one verified source reference.")
        if not args["title"].strip() or len(args["title"]) > 200 or not args["body"].strip() or len(args["body"]) > 100000:
            raise ValueError("Report requires a bounded nonempty title and body.")
        sources = store.get_evidence(references)
        lines = [f'- [{x["evidence_id"]}] document={x["document_id"]}; locator={x["locator"]}; '
                 f'source_sha256={x["source_sha256"]}; text={json.dumps(x["text"], ensure_ascii=False)}' for x in sources]
        content = "# " + args["title"].replace("\n", " ") + "\n\n" + args["body"] + "\n\n## References\n\n" + "\n".join(lines) + "\n"
        return store.write_artifact(kind="report", content=content, extension=".md",
                                    evidence_ids=references, filename=args.get("filename"))

    def read_document(args: dict[str, Any]) -> dict[str, Any]:
        doc = store.get_document(args.get("document_id"))
        records = store.evidence_for_document(doc["id"])
        offset, max_chars = args.get("offset", 0), args.get("max_chars", 40000)
        if offset > len(records):
            raise ValueError("offset exceeds the document evidence count")
        # Compact source records share the document/hash once. Each original
        # paragraph/cell retains its stable identity, text and exact locator.
        selected, used = [], 0
        for record in records[offset:]:
            item = {key: value for key, value in record.items()
                    if key not in {"body", "source_sha256", "document_id", "input_path"}}
            size = len(json.dumps(item, ensure_ascii=False))
            if used + size > max_chars:
                if not selected:
                    raise ValueError("One source record exceeds max_chars; use search_evidence or a narrower source document")
                break
            selected.append(item)
            used += size
        position = offset + len(selected)
        digest = next(item["source_sha256"] for item in store.list_documents() if item["document_id"] == doc["id"])
        return {"document_id": doc["id"], "source_sha256": digest,
                "evidence": selected, "offset": offset, "next_offset": position,
                "total_records": len(records), "has_more": position < len(records)}

    add("list_documents", "List sanitized documents available in this research workspace.", {}, [], lambda args: {"documents": store.list_documents()})
    add("read_document", "Read paragraphs and table cells with stable evidence IDs; bounded pages preserve complete records. Continue with next_offset if has_more.",
        {"document_id": document_schema, "offset": {"type": "integer", "minimum": 0},
         "max_chars": {"type": "integer", "minimum": 1000, "maximum": 40000}}, [], read_document)
    add("search_evidence", "Lexically search table and document body; returns evidence ids, locators and SHA256.",
        {"query": string_schema, "document_id": document_schema, "top_k": {"type": "integer", "minimum": 1, "maximum": 30}}, ["query"], search)
    add("read_table", "Read zero-based table cells with source evidence; row_end is exclusive.",
        {"document_id": document_schema, "row_start": {"type": "integer", "minimum": 0},
         "row_end": {"type": "integer", "minimum": 0}, "columns": {"type": "array", "items": {"type": "integer"}}}, [], read_table)
    add("calculate", "Finite Decimal arithmetic (+ - * / **, abs/min/max) with variables and evidence; no code execution.",
        {"expression": string_schema, "variables": {"type": "object"}, "evidence_ids": evidence_schema}, ["expression"], calculate)
    add("normalize_value", "Normalize numeric text and thousand/million/billion/yuan or ratio/percent/bps; no currency exchange.",
        {"value": string_schema, "unit": string_schema, "target_unit": string_schema, "currency": string_schema,
         "evidence_ids": evidence_schema}, ["value"], normalize)
    add("compare_periods", "Delta and percentage change (current-previous)/previous*100; zero previous yields undefined growth.",
        {"current": string_schema, "previous": string_schema, "evidence_ids": evidence_schema}, ["current", "previous"], compare)
    add("render_chart", "Write an escaped local bar chart SVG with manifest and source references.",
        {"title": string_schema, "labels": {"type": "array", "items": {"type": "string"}},
         "values": {"type": "array", "items": {"type": "string"}, "maxItems": 24},
         "evidence_ids": evidence_schema, "filename": string_schema},
        ["title", "labels", "values"], render_chart)
    add("write_report", "Write Markdown and resolved source references; valid evidence from read/search is required.",
        {"title": string_schema, "body": string_schema, "evidence_ids": evidence_schema, "filename": string_schema},
        ["title", "body", "evidence_ids"], write_report)
    return tools


def register_finance_tools(registry: ToolRegistry, *, store: FinanceStore, overwrite: bool = False) -> list[BaseTool]:
    tools = build_finance_tools(store=store)
    registry.register_many(tools, overwrite=overwrite)
    return tools
