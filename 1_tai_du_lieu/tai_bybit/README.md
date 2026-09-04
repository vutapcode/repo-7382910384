# tai_bybit

Status: `ACTIVE_RECORDER_RESEARCH_ONLY`

Owner: Bybit `BTCUSDT` linear-perpetual public market-data collection only.

Semantic role: `DERIVATIVE_STRESS_DATA_ONLY`

The optional recorder collector exposes linear-perpetual ticker/OI/mark/index/
funding fields and the all-liquidation stream. It is enabled by
`WSTRADE_BYBIT_RESEARCH=1` and writes WAL only; it is not passed to features or
strategy research consumers.

Hard rules:
- DERIVATIVE, never CASH.
- Cannot create direction by itself.
- Liquidation is forced/closing flow, not fresh directional intent.
- Primary future question: is a Binance derivative event venue-local or cross-derivative-market stress?

Always `authority=false` until explicit replay-backed promotion.
