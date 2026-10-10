---
name: "valuation-dcf"
description: "Build an explicit, auditable discounted cash flow scenario when sufficient inputs and assumptions exist."
triggers: ["DCF", "valuation", "discounted cash flow", "terminal value", "sensitivity"]
version: "1.0.0"
---

# Discounted cash flow scenario

- Establish the valuation date, currency and method. Distinguish enterprise free cash flow discounted at a weighted average cost of capital from equity free cash flow discounted at a cost of equity.
- Gather grounded historical operating cash flow drivers, capital expenditure, working capital, tax basis, debt, cash and diluted share count as required by the chosen method. List missing inputs before attempting a valuation.
- Keep reported facts separate from user-supplied or explicitly labeled scenario assumptions. Do not invent a current market price, discount rate, growth forecast or capital structure.
- Project cash flows under a transparent set of assumptions. State timing conventions. Compute terminal value only under compatible growth and discount assumptions; reject a perpetual growth rate that meets or exceeds the discount rate.
- Execute discounted cash flow calculations and the enterprise-to-equity bridge using the terminal or calculator. Save the input and output tables so the result is reproducible.
- Run sensitivity cases over user-approved assumptions and show the resulting range. Explain which assumptions dominate the change rather than reporting a single precise target as a fact.
- Check units, share dilution, non-operating assets and debt adjustments. Present the result as a conditional research scenario with citations and explicit limitations.
