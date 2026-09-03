# tai_eur_usd

Status: `STAGED_NOT_WIRED`

Owner: independent EUR/USD FX reference.

Semantic role: `EUR_USD_FX_DATA_ONLY`

Purpose: separate BTC-EUR movement from EUR/USD movement before any future inference about European BTC demand.

Requirements:
- external FX reference; never derive it from BTC cross-prices
- explicit event/receive timestamps, epoch and source health

Until a provider is verified: `UNAVAILABLE_PROVIDER_UNSET`.
Always `authority=false`.
