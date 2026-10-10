---
name: "comparative-analysis"
description: "Compare companies, segments or periods using aligned metrics and reproducible tables."
triggers: ["compare", "peer", "segment", "trend", "ranking"]
version: "1.0.0"
---

# Comparative financial analysis

- Define the comparison set, requested metrics and reporting cut-off before retrieving values. Record unavailable observations rather than silently dropping inconvenient cases.
- Align fiscal periods, currency, scale and accounting definitions. Separate quarterly, annual and trailing measures. Do not annualize a seasonal quarter without explaining the assumption.
- Build a tidy table with entity, period, metric, value, unit and evidence identifier. Validate uniqueness and missingness before computing rankings or aggregates.
- Calculate comparable ratios in a reproducible tool call. Use appropriately weighted aggregation when a simple mean would change the business meaning.
- Examine outliers against the underlying statements and notes; they may reflect acquisitions, disposals, restatements or a small denominator.
- Produce a compact comparison table and, if useful, a labeled chart saved as an artifact. Include source identifiers and the transformation used.
- Separate observed differences from causal explanations. Avoid assigning a cause unless supporting evidence is available, and state when coverage is incomplete.
