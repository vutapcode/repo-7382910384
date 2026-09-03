# tai_bybit

Status: `STAGED_NOT_WIRED`

Owner: Bybit `BTCUSDT` linear-perpetual public market-data collection only.

Semantic role: `DERIVATIVE_STRESS_DATA_ONLY`

Future collector may expose public trades, BBO/ticker, OI/mark/index/funding fields and all-liquidation data.

Hard rules:
- DERIVATIVE, never CASH.
- Cannot create direction by itself.
- Liquidation is forced/closing flow, not fresh directional intent.
- Primary future question: is a Binance derivative event venue-local or cross-derivative-market stress?

Always `authority=false` until explicit replay-backed promotion.
