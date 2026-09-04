# tai_onchain_context

Status: `STAGED_NOT_WIRED`
Authority: `false`
Semantic role: `ONCHAIN_BACKGROUND_DATA_ONLY`

## Owns
Slow blockchain settlement/transfer context. It does not own executed-trade direction.

## Future question
`IS_THERE_SLOW_SETTLEMENT_PRESSURE_RELEVANT_TO_THE_BACKGROUND_THESIS?`

## May collect later
- exchange-labelled inflow/outflow
- large transfers with explicit entity-confidence metadata
- miner/treasury flows where provenance is strong
- block time, confirmation depth, provider availability time

## Must NOT
- treat wallet transfer as buy/sell
- treat exchange inflow as immediate sell
- participate in 100ms-6s Ignition/Entry timing
- write Bias/Entry/Guardian state directly

## Future consumers
Only a slow structural-background reasoning owner. Any consumer must preserve entity-label uncertainty and confirmation latency.

## Promotion
Requires availability-time-safe replay proving a named background-thesis benefit beyond live market data.
