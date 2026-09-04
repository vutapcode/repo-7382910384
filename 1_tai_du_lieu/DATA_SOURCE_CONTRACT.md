# WStrade Prime Data Source Contract

Status: `MIXED_COLLECTION_STAGING`

This file is the canonical collection-layer ownership contract. It does not grant
Bias, Ignition, Entry, Action, Execution, Guardian, Hard Risk, promotion, or
Mainnet authority.

Canonical rule: **collect first; authority only after matched same-WAL replay proves
a named causal benefit.**

## Current implementation status

### Active canonical market transport

These sources are started by the current Tier-S runtime:

- `1_tai_du_lieu/tai_dong_tien/tai_dong_tien.py`
  - Binance Spot `aggTrade` executed cash flow.
  - Binance Futures `aggTrade` derivative flow.
  - Binance Futures `forceOrder` is transported on the SAME Futures socket.
  - Canonical runtime injects `loi_he_thong/liquidation_context.py` as the only
    forceOrder consumer; there is no second dedicated forceOrder socket/task.
- `1_tai_du_lieu/tai_gia_tick/tai_gia_tick.py`
  - Binance Spot BBO.
  - Binance Futures executable BBO.
- `1_tai_du_lieu/tai_vi_mo/tai_vi_mo.py`
  - Binance Futures Open Interest + funding context.
- `1_tai_du_lieu/tai_coinbase/tai_coinbase.py`
  - Coinbase BTC-USD ticker + executed matches.
  - Coinbase BTC-USD public `level2_batch` transport/reconstruction.
  - L2 is explicitly data-only and never calls Bias/Ignition/Entry.
- `1_tai_du_lieu/tai_nen_offline/tai_nen_offline.py`
  - Binance 1m candles used by current ATR normalization.

### Implemented but NOT started by canonical runtime

- `1_tai_du_lieu/tai_usdt_usd/tai_usdt_usd.py`
  - Provider: Coinbase Exchange public `USDT-USD` ticker.
  - Role: `USDT_USD_BASIS_DATA_ONLY`.
  - Status: `IMPLEMENTED_NOT_WIRED`.
  - It observes USDT directly against USD; it is NOT derived from BTCUSDT vs
    BTCUSD and writes only namespaced `usdt_usd_*` fields.

### Research observation owner, NOT strategy-wired

- `2_suy_luan_mapping/cash_liquidity_response.py`
  - Role: `CASH_LIQUIDITY_RESPONSE_OBSERVATION_ONLY`.
  - `authority=false`.
  - It refuses to classify raw L2 removal/cancellation as execution.
  - A caller must explicitly provide execution-linked depletion before a
    `FLOW_CONVERTING`/`ABSORBED` observation is possible.

### Reserved / staged, no collector wiring yet

- `tai_bitvavo` — EUR cash primary.
- `tai_kraken` — EUR cash secondary; same EUR evidence family by default.
- `tai_upbit` — KRW local-fiat cash.
- `tai_bybit` — derivative stress/liquidation cross-check.
- `tai_cme` — institutional derivatives; requires official entitlement.
- `tai_eur_usd` — EUR/USD normalization; provider unset.
- `tai_krw_usd` — KRW/USD normalization; provider unset.
- `tai_stablecoin_stress` — stablecoin/quote-health context.
- `tai_deribit_options` — forward-risk/options context.
- `tai_etf_flow` — slow regulated structural flow.
- `tai_macro_risk` — slow macro common-cause context.
- `tai_onchain_context` — slow settlement context.

Do not create parallel replacements for an existing owner path.

## Source ownership and future questions

### Binance Spot

Role: current crypto-native cash source.

Question: `IS_EXECUTED_BINANCE_CASH_AGGRESSING_AND_CONVERTING_PRICE?`

Executed Spot flow may participate in cash truth. Binance Spot + Binance Futures
is one Binance complex and must not be counted as two independent confirmations.

### Coinbase BTC-USD executed cash

Owner: `1_tai_du_lieu/tai_coinbase/`.

Question: `DOES_AN_INDEPENDENT_USD_CASH_VENUE_ACCEPT_THE_MOVE?`

Only executed `match` rows establish Coinbase executed cash flow.

### Coinbase BTC-USD L2

Same owner as Coinbase cash; no parallel `tai_coinbase_v2`.

Semantic role: `USD_CASH_LIQUIDITY_DATA_ONLY`.

Question: `WHAT_DID_USD_CASH_LIQUIDITY_DO_AFTER_EXECUTED_FLOW?`

Hard invariants:
- L2 removal/cancellation != execution.
- L2 quantity is not directional intent.
- reconnect starts a new L2 epoch.
- L2 fields are namespaced `coinbase_l2_*` and `authority=false`.
- future depletion/refill/absorption reasoning belongs to
  `2_suy_luan_mapping/cash_liquidity_response.py`, not the collector.

### Binance Futures + OI + liquidation

Roles: derivative response, positioning context, forced-flow classification.

Questions:
- `IS_DERIVATIVE_FLOW_FOLLOWING_OR_DRIVING_WITHOUT_CASH?`
- `ARE_POSITIONS_EXPANDING_OR_CONTRACTING?`
- `IS_THE_MOVE_PARTLY_FORCED_CLOSING_FLOW?`

Hard invariants:
- Futures/OI/liquidation cannot independently create cash direction.
- OI does not identify trader identity.
- liquidation is forced/closing flow.
- one `forceOrder` event is ingested once through the combined Futures socket.

### USDT/USD quote normalization

Owner: `1_tai_du_lieu/tai_usdt_usd/`.

Provider: Coinbase Exchange `USDT-USD`.

Semantic role: `USDT_USD_BASIS_DATA_ONLY`.

Question: `IS_USDT_DISTORTING_THE_OBSERVED_BTCUSDT_MOVE?`

Hard invariants:
- direct USDT/USD observation only; never derive from BTC cross-prices.
- output describes quote distortion/health, not BTC direction.
- no canonical launcher wiring until matched replay defines a consumer and proves
  a named benefit.

### EUR cash / normalization

Owners:
- `tai_bitvavo` = `EUR_CASH_PRIMARY_DATA_ONLY`.
- `tai_kraken` = `EUR_CASH_SECONDARY_DATA_ONLY`.
- `tai_eur_usd` = `EUR_USD_FX_DATA_ONLY`.

Question: `IS_EUROPEAN_FIAT_CASH_MOVING_BTC_AFTER_EUR_FX_IS_REMOVED?`

Bitvavo + Kraken are one `EUR_CASH_FAMILY` unless replay proves additional
independence. Raw BTC-EUR must not be compared directly with BTC-USD/BTCUSDT.

### KRW cash / normalization

Owners:
- `tai_upbit` = `KRW_LOCAL_CASH_DATA_ONLY`.
- `tai_krw_usd` = `KRW_USD_FX_DATA_ONLY`.

Question: `IS_THERE_KOREAN_LOCAL_FIAT_DEMAND_AFTER_FX_AND_LOCAL_BASIS_ARE_SEPARATED?`

Raw KRW-BTC price never becomes a generic cross-exchange price vote.

### Bybit

Role: `DERIVATIVE_STRESS_DATA_ONLY`.

Question: `IS_A_BINANCE_DERIVATIVE_EVENT_VENUE_LOCAL_OR_CROSS_DERIVATIVE_STRESS?`

Bybit is derivative, never cash. Liquidation cannot create direction.

### CME

Role: `INSTITUTIONAL_DERIVATIVE_DATA_ONLY`.

Question: `IS_REGULATED_INSTITUTIONAL_DERIVATIVE_RISK_REPRICING_TOO?`

Without official real-time entitlement/config, publish
`UNAVAILABLE_NO_ENTITLEMENT`; never silently substitute Binance/Bybit/delayed data.

### Stablecoin stress

Role: `STABLECOIN_STRESS_DATA_ONLY`.

Question: `IS_THE_QUOTE_ASSET_DISTORTING_THE_OBSERVED_BTC_MOVE?`

May later combine explicitly named USDT/USDC direct markets, cross-venue basis
and liquidity deterioration. It must not reuse BTC cross-price differences as a
circular input and cannot create BTC direction.

### Options

Owner: `tai_deribit_options`.
Role: `OPTIONS_RISK_REGIME_DATA_ONLY`.

Question: `IS_FORWARD_TAIL_RISK_BEING_REPRICED_WHILE_CURRENT_CASH_TRUTH_PLAYS_OUT?`

IV/skew/options OI are forward-risk context, never direct LONG/SHORT authority.

### ETF / macro / on-chain

Roles: slow structural/background context only.

Questions:
- ETF: `IS_REGULATED_CAPITAL_PERSISTENTLY_ENTERING_OR_LEAVING?`
- Macro: `IS_A_BROAD_MACRO_REPRICING_A_PLAUSIBLE_COMMON_CAUSE?`
- On-chain: `IS_THERE_SLOW_SETTLEMENT_PRESSURE_RELEVANT_TO_BACKGROUND?`

They never participate directly in 100ms-6s Ignition/Entry timing. Publication
and first-availability timestamps must prevent replay lookahead.

## Consumer routing

Collection modules expose facts only. Future consumers request only the owner
that answers their question; no undifferentiated multi-source score.

`CASH_CONTROL / IGNITION`
- executed Binance Spot + Coinbase cash;
- regional fiat cash only after quote normalization;
- cash L2 response only after execution linkage;
- ETF/macro/on-chain/options cannot become direct direction votes.

`CASH_LIQUIDITY_RESPONSE`
- raw executed cash event + same-epoch L2 response;
- output: observation only (`FLOW_CONVERTING`, `ABSORBED`, `REFILLING`,
  `LIQUIDITY_RETREAT`, `UNKNOWN`);
- raw cancel/removal alone => `UNKNOWN`.

`QUOTE_NORMALIZATION`
- `tai_usdt_usd`, `tai_eur_usd`, `tai_krw_usd`, `tai_stablecoin_stress`;
- output distortion/health only, not direction.

`DERIVATIVE_MECHANISM`
- Binance Futures/OI/liquidation + future Bybit/CME;
- answer follow/build/unwind/forced-flow questions;
- no independent cash direction.

`FORWARD_RISK_CONTEXT`
- future Deribit/options only.

`STRUCTURAL_BACKGROUND`
- ETF + macro + on-chain only.

## Normalized data envelope

Every new/staged source must expose, when applicable:

- `source_id`
- `venue`
- `instrument`
- `market_family`
- `quote_currency`
- `semantic_role`
- `event_type`
- `event_time_ms`
- `receive_time_ms`
- `receive_time_monotonic_ns`
- `epoch`
- sequence/trade id where available
- `source_health`
- `authority=false`

Reconnect/gap begins a new causal epoch. Never bridge continuity across it.
For slow published datasets, store economic/reference time separately from first
availability/publication time.

## Evidence-family rules

Do not count correlated evidence as independent:

- Binance Spot + Binance Futures = one Binance complex, not two cash votes.
- Bitvavo + Kraken = one EUR family by default.
- Binance Futures + Bybit = derivative family evidence, not cash control.
- Coinbase USD and Upbit KRW can represent different fiat pools, but raw prices
  still require quote/basis normalization.
- USDT/USD basis + future stablecoin-stress observations may share provider data;
  deduplicate before treating them as separate evidence.
- CME/options remain derivative/risk families, never cash confirmations.
- ETF/macro/on-chain are slow background families, not three hot confirmations.

## Session rule

Collectors remain available whenever their venue/source is operational. Do not
hard-disable Coinbase outside U.S. hours, EUR venues outside Europe hours, or
Upbit outside Asia hours at the data layer. Session relevance belongs to a future
reasoning/context owner and must be justified empirically.

## Runtime boundary

Current exceptions to `STAGED_NOT_WIRED` are explicit:
- Coinbase L2 is transported by the already-active Coinbase collector but remains
  `authority=false` and has no strategy consumer.
- USDT/USD collector exists but is `IMPLEMENTED_NOT_WIRED` and is not started by
  canonical launchers.

Do NOT:
- start staged sources from canonical launcher merely because code exists;
- write new observations into existing authoritative Binance/Bias/OI/Guardian/
  Action/Execution fields;
- let a collector interpret Market Truth;
- add scores/weights/votes/session hard-disables in collection code;
- let a slow context source veto hot Entry without a separate proven authority
  change.

## Promotion rule

No source earns strategy authority because it exists or appears intuitively useful.
Promotion requires same-WAL matched replay showing that it corrects a named
misunderstanding the previous source set could not resolve, without lookahead or
correlated-evidence double counting.

Before any authority promotion, document:
1. exact question;
2. module owner;
3. consumer;
4. independence/evidence family;
5. event-time + availability-time semantics;
6. falsification condition;
7. which authority may change (Market Truth, Action, or Safety);
8. matched replay / shadow evidence and version boundary.
