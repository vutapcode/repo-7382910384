# tai_krw_usd

Status: `STAGED_NOT_WIRED`

Owner: independent KRW/USD FX reference for Korean-local market normalization.

Semantic role: `KRW_USD_FX_DATA_ONLY`

Purpose: separate KRW FX movement from Korean BTC local-premium/demand movement.

Requirements:
- external KRW/USD reference; never derive it from BTC cross-prices
- local BTC premium remains a separate derived observation, not the FX source itself
- explicit event/receive timestamps, epoch and source health

Until a provider is verified: `UNAVAILABLE_PROVIDER_UNSET`.
Always `authority=false`.
