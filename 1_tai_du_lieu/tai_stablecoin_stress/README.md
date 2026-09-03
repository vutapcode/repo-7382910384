# tai_stablecoin_stress

Status: `STAGED_NOT_WIRED`
Authority: `false`
Semantic role: `STABLECOIN_STRESS_DATA_ONLY`

## Owns
Quote-asset health/stress observations for USDT/USDC and similar approved stablecoin references.

## Future question
`IS_THE_QUOTE_ASSET_DISTORTING_THE_OBSERVED_BTC_MOVE?`

## May collect later
- independently sourced USDT/USD and USDC/USD deviations
- cross-venue stablecoin basis disagreement
- stablecoin spread/liquidity deterioration
- official reserve/redemption status as slow context

## Must NOT
- infer BTC direction
- derive stress from BTCUSDT vs BTCUSD itself
- write canonical BTC BBO/CVD/OI/Bias/Guardian state
- become a hot Entry vote

## Future consumers
Only a dedicated quote-normalization / quote-health reasoning owner may consume this directly. Cash-control logic may consume the normalized result, not raw stablecoin observations.

## Promotion
Requires same-WAL replay showing that quote stress corrected a named BTC-market misunderstanding without lookahead or circular derivation.
