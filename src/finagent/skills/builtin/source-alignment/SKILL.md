---
name: "source-alignment"
description: "Resolve conflicting dates, units, entities and definitions before using financial evidence."
triggers: ["source", "period", "unit", "conflicting evidence", "annual report"]
version: "1.0.0"
---

# Source alignment

Use when retrieving financial facts or when two plausible values disagree.

- Restate the requested entity, fiscal period, measure, currency and desired output unit. A calendar year and fiscal year are not automatically equivalent.
- Inspect document metadata and the relevant table headers, row labels, footnotes and nearby text. A search excerpt is a lead; open the original evidence before calculating.
- Build a small evidence ledger: value, source identifier, exact row/column or passage, period, unit, definition and sign. Use only observations actually returned by tools in this run or validated scoped memory.
- Distinguish reported amounts from calculated amounts, restated figures from originally reported figures, consolidated from segment data, and year-end stock measures from annual flow measures.
- Convert units explicitly and preserve precision. Parentheses can denote a negative amount; an em dash often denotes missing or not applicable data, not a numeric zero.
- When competing sources cannot be reconciled, state the discrepancy and ask for the missing basis. Do not pick the more convenient value.
- Before finalizing, check every factual claim against an observed evidence identifier. If the source is unavailable or ambiguous, qualify the result and explain the missing evidence.
