---
name: "financial-ratios"
description: "Compute grounded ratios, shares, growth and percentage changes without denominator or unit confusion."
triggers: ["ratio", "percentage", "growth", "margin", "average", "FinQA"]
version: "1.0.0"
---

# Financial ratios and change

Use for a question requiring arithmetic over financial evidence.

- Identify the exact numerator and denominator from the question before selecting table cells. Read the whole relevant row and the year headers. A growth rate uses the earlier value as denominator; a share uses the requested total; a ratio may be a multiple rather than a percentage.
- Retrieve and cite each operand, with its source, period, unit and sign. Prefer a direct authoritative source value over inferring the same value from unrelated totals.
- Write the formula symbolically, then execute it with the calculation tool or terminal. For growth, use (later minus earlier) divided by earlier; for a relative premium, use (comparison minus base) divided by base. Percentage point change is subtraction of percentages and is different from relative percentage change.
- Keep raw precision throughout the calculation. Convert a ratio to percentage only when the requested output is a percentage. Do not apply the same scaling twice.
- Check denominators, signs and units. A zero denominator is undefined; a negative base may make a growth percentage misleading. Explain such conditions rather than hiding them.
- For averages, determine whether the request means arithmetic mean, weighted mean, or beginning/end average. Do not treat a sum as an average.
- Return the requested value, unit, formula, evidence citations and a short interpretation. In structured benchmark output, follow the requested numeric convention and use a flat arithmetic program only when the output contract requires one.
