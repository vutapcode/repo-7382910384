# tai_upbit

Status: `STAGED_NOT_WIRED`

Owner: Upbit `KRW-BTC` public market-data collection only.

Semantic role: `KRW_LOCAL_CASH_DATA_ONLY`

Future collector may expose executed trades + orderbook. Raw KRW BTC price must never be compared directly with BTC-USD/BTC-USDT. Any future consumer must first separate KRW FX and local-premium/basis effects.

Always `authority=false` until explicit replay-backed promotion.
