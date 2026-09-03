# tai_bitvavo

Status: `STAGED_NOT_WIRED`

Owner: Bitvavo `BTC-EUR` public market-data collection only.

Semantic role: `EUR_CASH_PRIMARY_DATA_ONLY`

Future collector may expose executed trades, BBO/ticker and optional book data.
It must never write canonical Binance/Coinbase strategy state and must always emit
`authority=false` until an explicit replay-backed authority change.

Bitvavo and Kraken belong to one `EUR_CASH_FAMILY` by default; do not double-count.
