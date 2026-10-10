# Quarantined candidate: learned-period-pair-earnings-change

Validation rejected promotion: no independent improvement.

## Purpose
Compute a period-over-period change in a named segment's earnings from a filing, while preventing the most common failure: pairing the wrong base period with the wrong comparative period.

## Trigger
Use when a question asks for the change, growth, or percent change in earnings for a named segment between two stated periods.

## Evidence checks
- Locate the narrative or table that states the segment's earnings for each period.
- Confirm the segment label matches the question exactly; do not substitute a broader or narrower segment.
- Confirm the period labels attached to each figure. A sentence may describe a change between two periods while also mentioning a third period in a separate sentence.
- Identify which figure is the base period and which is the comparative period as stated in the question.
- If the question asks for a change from an earlier period to a later period, the earlier figure is the base.

## Computation steps
- Let base equal the earnings figure for the earlier stated period.
- Let later equal the earnings figure for the later stated period.
- Compute the ratio as (later minus base) divided by base.
- Do not multiply by one hundred unless the question explicitly requests a formatted percentage.
- Keep the source scale of the amounts; do not convert units unless asked.

## Verification
- Re-read the source sentence and confirm the two figures you used are the ones the sentence pairs with the two periods in the question.
- Check that the base figure is not actually from a different period mentioned elsewhere in the same passage.
- Confirm the sign of the result matches the direction of change described in the source.
- If the source describes an increase, the ratio should be positive; if a decrease, negative.

## Stopping conditions
- Stop when the base and comparative figures are both confirmed against the source and the ratio is computed.
- If the source does not clearly state earnings for both periods, request the missing evidence rather than inferring or manufacturing a figure.
- If the segment label or period labels are ambiguous, request clarification before computing.

## Avoiding the observed failure
The failed run used a figure from a sentence describing a different period pair than the one asked about. Always match the period labels in the question to the period labels in the source sentence before selecting figures. Do not assume that the most recent sentence or the most prominent figure is the correct base.
