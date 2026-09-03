# tai_usdt_usd

Status: `IMPLEMENTED_NOT_WIRED`
Authority: `false`

Owner: independent USDT/USD basis reference.

Semantic role: `USDT_USD_BASIS_DATA_ONLY`

Provider: **Coinbase Exchange public `USDT-USD` ticker**.
Implementation: `1_tai_du_lieu/tai_usdt_usd/tai_usdt_usd.py`.

Purpose: separate BTCUSDT movement caused by BTC from movement partly caused by USDT changing versus USD.

Why this provider:
- observes USDT directly against USD; it is not inferred from BTCUSDT/BTCUSD;
- provider is explicitly named and independently observable from Binance BTCUSDT;
- the collector carries event time, receive time, monotonic receive time, epoch and source health;
- reconnect starts a new epoch and never bridges continuity.

Namespaced outputs only:
- `usdt_usd_snapshot`
- `usdt_usd_basis_bps`
- `usdt_usd_price`
- `usdt_usd_source_health`
- `usdt_usd_epoch`

Hard invariants:
- never write Binance/Bitcoin authoritative price or flow fields;
- never create BTC direction;
- never derive basis from BTC cross-prices;
- no canonical launcher wiring in this phase;
- future consumer question is only `IS_USDT_DISTORTING_THE_OBSERVED_BTCUSDT_MOVE?`.
