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
BTCUSDT. A future consumer must separate FX/local-basis movement from BTC demand.

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

## Non-price Prime owners

These modules exist to answer questions that another exchange price cannot answer.
They remain DATA-ONLY and MUST NOT be turned into generic LONG/SHORT votes.

### 10. Stablecoin stress / quote health

Owner path: `1_tai_du_lieu/tai_stablecoin_stress/`

Semantic role: `STABLECOIN_STRESS_DATA_ONLY`

Purpose: answer whether a BTCUSDT dislocation is partly caused by stress in the quote
asset rather than new BTC demand/supply.

Desired observations:
- USDT and USDC market-price deviation versus USD using independently named venues/providers
- cross-venue stablecoin basis disagreement
- liquidity deterioration / spread widening where available
- redemption/reserve status only as slow context when sourced from an official provider

Hard invariants:
- stablecoin stress does not create BTC direction.
- do not derive this module from BTCUSDT versus BTCUSD; that would be circular.
- depeg/basis is a falsifier or normalization input, not an alpha vote.

Future consumer question: `IS_THE_QUOTE_ASSET_DISTORTING_THE_OBSERVED_BTC_MOVE?`

### 11. Options forward-risk regime

Owner path: `1_tai_du_lieu/tai_deribit_options/`

Semantic role: `OPTIONS_RISK_REGIME_DATA_ONLY`

Purpose: observe how the market prices future uncertainty/tail risk, not current cash
direction.

Desired observations:
- BTC option implied volatility by tenor
- put/call skew or equivalent risk-reversal observations
- option OI / volume when semantics are clear
- underlying/index timestamps and source health

Hard invariants:
- IV/skew cannot directly issue LONG/SHORT.
- options data belongs to forward-risk/regime context, not cash-control evidence.
- do not mix option OI with Futures OI identity inference.

Future consumer question: `IS_FORWARD_TAIL_RISK_BEING_REPRICED_WHILE_THE_CURRENT_CASH_THESIS_PLAYS_OUT?`

### 12. ETF / regulated structural flow

Owner path: `1_tai_du_lieu/tai_etf_flow/`

Semantic role: `REGULATED_STRUCTURAL_FLOW_DATA_ONLY`

Purpose: observe slow institutional allocation/redemption pressure in regulated BTC
vehicles.

Desired observations:
- official or provider-attributed ETF creations/redemptions / net flows
- publication timestamp and economic reference date must be distinct
- source revision/version when available

Hard invariants:
- ETF flow is slow structural context; never a hot Entry trigger.
- publication-time semantics must prevent lookahead in replay.
- do not infer intraday execution direction from daily net flow alone.

Future consumer question: `IS_THERE_PERSISTENT_REGULATED_CAPITAL_INFLOW_OR_OUTFLOW_IN_THE_BACKGROUND?`

### 13. Macro risk context

Owner path: `1_tai_du_lieu/tai_macro_risk/`

Semantic role: `MACRO_RISK_CONTEXT_DATA_ONLY`

Purpose: observe broad USD/rates/equity risk repricing that may explain simultaneous
cross-asset moves without pretending to identify BTC microstructure control.

Candidate observations after provider selection:
- DXY or a clearly defined USD index proxy
- US Treasury yield reference(s)
- S&P/Nasdaq futures or equivalent risk proxy
- event/receive time, provider, market-open status and source health

Hard invariants:
- macro context cannot override live executed BTC cash evidence.
- no indicator score or weighted risk-on/risk-off composite.
- stale/closed-market values must be marked as such, never forward-filled as fresh truth.

Future consumer question: `IS_A_BROAD_MACRO_REPRICING_A_PLAUSIBLE_COMMON_CAUSE_OF_THE_MOVE?`

### 14. On-chain context

Owner path: `1_tai_du_lieu/tai_onchain_context/`

Semantic role: `ONCHAIN_BACKGROUND_DATA_ONLY`

Purpose: observe slow blockchain settlement/transfer context that may matter over
minutes-to-days, while acknowledging that a transfer is not the same thing as a trade.

Candidate observations after provider selection:
- exchange-labelled inflow/outflow
- large transfers with explicit entity-confidence metadata
- miner/treasury flows where provenance is strong

Hard invariants:
- wallet transfer != buy/sell.
- exchange inflow != immediate sell.
- entity labels and block confirmation latency must be recorded.
- on-chain data never participates in 100ms-6s Ignition/Entry timing.

Future consumer question: `IS_THERE_SLOW_SETTLEMENT_PRESSURE_RELEVANT_TO_THE_BACKGROUND_THESIS?`

## Future consumer routing

Collection modules expose facts only. Future reasoning modules must request the source
that owns the question; they must not read every source and create an undifferentiated
score.

Suggested routing contract:

- `CASH_CONTROL / IGNITION`
  - executed Binance Spot / Coinbase / regional fiat cash after quote normalization
  - optional cash L2 response for depletion/refill/absorption
  - MUST NOT consume ETF, macro, on-chain, options as direct direction votes

- `QUOTE_NORMALIZATION`
  - `tai_usdt_usd`, `tai_eur_usd`, `tai_krw_usd`, `tai_stablecoin_stress`
  - output should describe distortion/health, not direction

- `DERIVATIVE_MECHANISM`
  - Binance Futures/OI/liquidation + `tai_bybit` + `tai_cme`
  - answer follow/build/unwind/forced-flow questions
  - MUST NOT self-create cash direction

- `FORWARD_RISK_CONTEXT`
  - `tai_deribit_options`
  - answer risk-regime/tail repricing only

- `STRUCTURAL_BACKGROUND`
  - `tai_etf_flow`, `tai_macro_risk`, `tai_onchain_context`
  - background context only; no hot-path veto unless separately proven and promoted

Any future consumer must document:
1. exact question being answered;
2. exact source owner(s);
3. why the evidence is independent or explicitly grouped;
4. event-time and availability-time semantics;
5. falsification condition;
6. whether the answer changes Market Truth, Action Policy, or only context;
7. matched-replay evidence before authority promotion.

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
For slow published datasets, store both economic/reference time and first-availability
(publication/receive) time so replay cannot look ahead.

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
- USDT/USD basis + stablecoin-stress observations may share venues/providers; deduplicate
  before treating them as separate evidence.
- CME futures and options-derived institutional signals remain derivative/risk families,
  never cash confirmations.
- ETF flow, macro and on-chain are slow background families, not three independent hot
  confirmations.

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
