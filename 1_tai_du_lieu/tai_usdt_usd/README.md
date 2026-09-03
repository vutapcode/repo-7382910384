# tai_usdt_usd

Status: `STAGED_NOT_WIRED`

Owner: independent USDT/USD basis reference.

Semantic role: `USDT_USD_BASIS_DATA_ONLY`

Purpose: separate BTCUSDT movement caused by BTC from movement partly caused by USDT changing versus USD.

Requirements:
- provider must be explicitly named and independently observable
- no circular derivation from BTCUSDT vs BTCUSD
- timestamp, receive-time, epoch and source health required

Until a provider is verified: `UNAVAILABLE_PROVIDER_UNSET`.
Always `authority=false`.
