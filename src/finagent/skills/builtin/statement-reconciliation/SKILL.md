---
name: "statement-reconciliation"
description: "Reconcile balance sheet, cash flow and income statement amounts with explicit definitions and tolerances."
triggers: ["balance sheet", "cash flow", "reconcile", "income statement", "cash bridge"]
version: "1.0.0"
---

# Statement reconciliation

- Identify the reporting entity, period, scale and accounting basis. Retrieve the statement and relevant explanatory notes before selecting components.
- Separate gross and net values, current and non-current classifications, operating and non-operating items, cash and non-cash items, and recurring and exceptional items.
- For a balance sheet, compare assets with liabilities plus equity. For a cash flow bridge, reconcile opening cash, operating/investing/financing flows, exchange effects and other disclosed changes to closing cash.
- For an earnings bridge, preserve the actual accounting signs. Expense reductions and negative expenses cannot be normalized by blindly taking absolute values.
- Run each subtotal and reconciliation with tools. Record the residual explicitly. Base any rounding tolerance on the displayed units and precision rather than silently forcing equality.
- If a component is missing, report the unreconciled difference with its source limitations. Do not insert a balancing plug and present it as a reported figure.
- Keep every derived amount linked to the exact observed input evidence and disclose any alternative definition used.
