# tai_cme

Status: `STAGED_NOT_WIRED`

Owner: CME Bitcoin futures institutional-derivative data contract.

Semantic role: `INSTITUTIONAL_DERIVATIVE_DATA_ONLY`

Desired future data: top-of-book, trades, market statistics, event/receive timestamps and source health.

CME real-time market data requires the appropriate official entitlement/config. If unavailable, publish `UNAVAILABLE_NO_ENTITLEMENT`; never silently substitute Binance/Bybit/delayed data.

CME is DERIVATIVE, never CASH, and cannot create direction by itself.
