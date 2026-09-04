# tai_deribit_options

Status: `STAGED_NOT_WIRED`
Authority: `false`
Semantic role: `OPTIONS_RISK_REGIME_DATA_ONLY`

## Owns
Forward-risk observations from BTC options. It does not own spot direction or futures identity.

## Future question
`IS_FORWARD_TAIL_RISK_BEING_REPRICED_WHILE_THE_CURRENT_CASH_THESIS_PLAYS_OUT?`

## May collect later
- implied volatility by tenor
- put/call skew or risk reversal
- option OI / volume with clear semantics
- underlying/index timestamps and source health

## Must NOT
- issue LONG/SHORT
- override executed cash control
- mix option OI with futures OI identity inference
- write Bias/Entry/Guardian state directly

## Future consumers
Only a forward-risk/regime reasoning owner. Action/Guardian may later consume a proved risk-regime statement, never raw option fields.

## Promotion
Requires matched replay proving that options risk data changes a named risk-regime misunderstanding or exit/action decision without becoming pseudo-direction authority.
