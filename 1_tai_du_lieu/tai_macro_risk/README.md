# tai_macro_risk

Status: `STAGED_NOT_WIRED`
Authority: `false`
Semantic role: `MACRO_RISK_CONTEXT_DATA_ONLY`

## Owns
Broad USD/rates/equity-risk context that may explain simultaneous cross-asset repricing.

## Future question
`IS_A_BROAD_MACRO_REPRICING_A_PLAUSIBLE_COMMON_CAUSE_OF_THE_MOVE?`

## May collect later
- DXY or explicitly defined USD-index proxy
- US Treasury yield reference(s)
- S&P/Nasdaq futures or equivalent risk proxy
- provider, market-open state, event/receive time, source health

## Must NOT
- override live executed BTC cash evidence
- create a weighted risk-on/risk-off score
- forward-fill stale/closed-market values as fresh truth
- write Bias/Entry/Guardian state directly

## Future consumers
Only a macro-context reasoning owner. Hot-path modules may consume a proved context statement only if matched replay later justifies it.

## Promotion
Requires replay showing a named misunderstanding corrected by macro context, with strict freshness and market-hours semantics.
