# WStrade Prime Data Source Contract

Status: `STAGED_NOT_WIRED`

This document freezes the collection-layer ownership for future Prime market-data work.
It is intentionally DATA-ONLY. Nothing in this document grants Bias, Ignition, Entry,
Action, Execution, Guardian, Hard Risk, promotion, or Mainnet authority.

Canonical rule: **collect first; authority only after matched same-WAL replay proves a
named causal benefit.**

## Existing active sources

These already exist in the canonical runtime and must not be duplicated:

- `1_tai_du_lieu/tai_dong_tien/tai_dong_tien.py`
  - Binance Spot executed flow (`aggTrade`)
  - Binance Futures executed flow / force-order context
- `1_tai_du_lieu/tai_gia_tick/tai_gia_tick.py`
  - Binance Spot BBO
  - Binance Futures executable BBO
- `1_tai_du_lieu/tai_vi_mo/tai_vi_mo.py`
  - Binance Futures OI + funding context
- `1_tai_du_lieu/tai_coinbase/tai_coinbase.py`
  - Coinbase BTC-USD ticker + executed matches
- `1_tai_du_lieu/tai_nen_offline/tai_nen_offline.py`
  - Binance 1m candle source used for ATR normalization

Do not create parallel replacements for these paths.

## Prime source module ownership

Each source owns one collection concern. Future consumers may import/read the source
through an explicit adapter, but no source may mutate another source's semantic state.

### 1. Coinbase USD cash liquidity

Owner path: `1_tai_du_lieu/tai_coinbase/`

Upgrade the existing Coinbase module; do NOT create `tai_coinbase_v2` or another
parallel Coinbase collector.

Desired additional raw data:
- BTC-USD Level-2 / `level2_batch`
- exchange event time where supplied
- local receive wall-clock + monotonic time
- source epoch / continuity state

Semantic role: `USD_CASH_LIQUIDITY_DATA_ONLY`

Important:
- L2 cancel/removal is NOT executed flow.
- Only executed matches establish executed cash flow.
- Depth may later answer depletion/refill/absorption questions, but this stage has
  `authority=false`.

### 2. Bitvavo EUR cash

Owner path: `1_tai_du_lieu/tai_bitvavo/`

Instrument: `BTC-EUR`

Desired public data:
- executed trades
- best bid/ask / ticker
- optional book data, collection-only

Semantic role: `EUR_CASH_PRIMARY_DATA_ONLY`

Purpose: observe the primary EUR cash pool, not add a generic exchange vote.

### 3. Kraken EUR cash

Owner path: `1_tai_du_lieu/tai_kraken/`

Instrument: BTC/EUR Spot

Desired public data:
- WebSocket v2 `trade`
- WebSocket v2 `book`

Semantic role: `EUR_CASH_SECONDARY_DATA_ONLY`

Hard invariant: Bitvavo + Kraken belong to the same `EUR_CASH_FAMILY` until replay
proves otherwise. They MUST NOT be counted as two independent confirmations by
default.

### 4. Upbit Korean local-fiat cash

Owner path: `1_tai_du_lieu/tai_upbit/`

Instrument: `KRW-BTC`

Desired public data:
- executed trades
- orderbook

Semantic role: `KRW_LOCAL_CASH_DATA_ONLY`

Hard invariant: raw KRW BTC price is never directly comparable with BTC-USD or
BTC-USDT. A future consumer must separate FX/local-basis movement from BTC demand.

### 5. Bybit derivative stress

Owner path: `1_tai_du_lieu/tai_bybit/`

Instrument: `BTCUSDT` linear perpetual

Desired public data:
- public trades
- BBO/ticker
- OI / mark / index / funding fields exposed by public ticker
- all-liquidation stream

Semantic role: `DERIVATIVE_STRESS_DATA_ONLY`

Hard invariants:
- Bybit is DERIVATIVE, never CASH.
- It cannot create direction by itself.
- Liquidation is forced/closing flow, not fresh directional intent.
- Its main future question is whether a Binance derivative event is venue-local or
  cross-derivative-market stress.

### 6. CME institutional derivatives

Owner path: `1_tai_du_lieu/tai_cme/`

Desired data contract:
- Bitcoin futures top-of-book
- trades
- market statistics
- event and receive timestamps
- source health

Semantic role: `INSTITUTIONAL_DERIVATIVE_DATA_ONLY`

CME real-time data requires the appropriate official entitlement/config. Without it,
this source must publish `UNAVAILABLE_NO_ENTITLEMENT`. Never silently substitute
Binance/Bybit/delayed data.

## Quote / basis normalization owners

Normalization sources are separate collection concerns. They do not belong inside
Binance, Coinbase, Bitvavo, Kraken, or Upbit modules.

### 7. USDT/USD basis

Owner path: `1_tai_du_lieu/tai_usdt_usd/`

Semantic role: `USDT_USD_BASIS_DATA_ONLY`

Purpose: distinguish a BTCUSDT move caused by BTC from one partly caused by USDT
moving versus USD.

Requirements:
- provider must be explicitly named and independently observable
- timestamp / receive-time / health / epoch
- no circular derivation from BTCUSDT vs BTCUSD itself

Until a provider is selected and verified, status is `UNAVAILABLE_PROVIDER_UNSET`.

### 8. EUR/USD FX reference

Owner path: `1_tai_du_lieu/tai_eur_usd/`

Semantic role: `EUR_USD_FX_DATA_ONLY`

Purpose: separate BTC-EUR movement from EUR/USD movement before any future inference
about European BTC demand.

Requirements:
- external FX reference, not derived from BTC cross-prices
- explicit timestamp / receive-time / source health

Until a provider is selected and verified, status is `UNAVAILABLE_PROVIDER_UNSET`.

### 9. KRW/USD FX + local basis reference

Owner path: `1_tai_du_lieu/tai_krw_usd/`

Semantic role: `KRW_USD_FX_DATA_ONLY`

Purpose: separate KRW FX movement from Korean BTC local-premium/demand movement.

Requirements:
- external KRW/USD reference, not derived from BTC cross-prices
- local BTC premium must remain a separate derived observation; it is not the FX
  reference itself

Until a provider is selected and verified, status is `UNAVAILABLE_PROVIDER_UNSET`.

## Future Phase-2 source (not current implementation scope)

Owner path reserved conceptually: `1_tai_du_lieu/tai_deribit_options/`

Semantic role: `OPTIONS_RISK_REGIME_DATA_ONLY`

Deribit/options may later answer forward-risk / implied-volatility questions. It must
never be treated as a direct LONG/SHORT cash vote. Do not implement or wire it as part
of the current Prime collector phase unless separately approved.

## Normalized event envelope

Every staged source must eventually expose normalized data carrying at least:

- `source_id`
- `venue`
- `instrument`
- `market_family`
- `quote_currency`
- `semantic_role`
- `event_type`
- `event_time_ms` when exchange/source provides it
- `receive_time_ms`
- `receive_time_monotonic_ns` when practical
- `epoch`
- sequence/trade id when available
- `source_health`
- `authority=false`

A reconnect/gap begins a new causal epoch. Never bridge continuity claims across it.

## Runtime boundary

Current state: all new Prime modules are `STAGED_NOT_WIRED`.

Do NOT:
- import/start them from `mainnet_tier_s_lean_launcher.py`
- import/start them from `loi_he_thong/tier_s_runtime_prune.py`
- write into existing authoritative fields such as canonical Binance Spot BBO/CVD,
  Bias fields, canonical OI, Guardian state, Action state, or Execution state
- add thresholds, scores, weights, votes, or session hard-disable logic
- let one source call another strategy owner directly

Future wiring must name the exact question, owner, consumer, falsification rule and
matched-replay evidence before authority changes.

## Evidence-family rules

Do not count correlated evidence as independent:

- Binance Spot + Binance Futures = same Binance complex, not two independent cash votes.
- Bitvavo + Kraken = one EUR cash family until independence is demonstrated.
- Bybit + Binance Futures = derivative family evidence; useful for cross-venue stress,
  not independent cash control.
- Coinbase USD and Upbit KRW may represent different fiat pools, but raw price comparison
  still requires quote/basis normalization.

## Session rule

Collectors remain available 24/7 when operational. Do not hard-disable Coinbase outside
US hours, Bitvavo/Kraken outside Europe hours, or Upbit outside Asia hours at the data
layer. Session relevance belongs to a future reasoning/context layer and must be learned
or explicitly justified, not baked into collection.

## Promotion rule

No source earns strategy authority because it exists or appears intuitively useful.
Promotion requires same-WAL matched replay showing that the source corrects a named
misunderstanding that the previous source set could not resolve, without double-counting
correlated evidence or introducing lookahead.
