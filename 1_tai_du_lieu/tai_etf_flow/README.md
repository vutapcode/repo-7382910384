# tai_etf_flow

Status: `STAGED_NOT_WIRED`
Authority: `false`
Semantic role: `REGULATED_STRUCTURAL_FLOW_DATA_ONLY`

## Owns
Slow regulated BTC allocation/redemption observations such as ETF/ETP flow data.

## Future question
`IS_THERE_PERSISTENT_REGULATED_CAPITAL_INFLOW_OR_OUTFLOW_IN_THE_BACKGROUND?`

## May collect later
- official/provider-attributed creations/redemptions or net flows
- economic reference date
- first public availability/publication timestamp
- revision/version metadata

## Must NOT
- trigger hot Entry
- infer intraday execution direction from daily flow
- use economic date as if data were known before publication
- write Bias/Entry/Guardian state directly

## Future consumers
Only a structural-background reasoning owner. It may provide background context to Market Truth after availability-time-safe replay proof.

## Promotion
Requires replay using first-availability time, showing incremental benefit beyond live cash/derivative sources without lookahead.
