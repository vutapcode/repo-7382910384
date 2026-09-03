# WStrade Ignition Core V1 strategy authority

> Canonical entrypoint: `mainnet_tier_s_lean_launcher.py`.
> File existence, class names and old journal fields do not prove that a module
> is active. Trace imports and `install(...)` calls from this entrypoint.

## Active production path

### Four-authority contract boundary

Every decision journal row may carry one content-addressed
`FOUR_AUTHORITY_CONTRACTS_V1` bundle. A qualified Action additionally seals
`ENTRY_THESIS_HANDOFF_V1`: the exact Market Truth and Action snapshots that
approved the Entry. Execution may refresh only its own contract; it cannot
rebuild or reinterpret the approved Truth.

- Market Truth owner: `loi_he_thong/market_thesis.py`. It alone records the
  mechanism, support, competing explanations, falsifiers and source health.
- Action owner: final decision integration in `mainnet_tier_s_shadow_launcher.py`.
- Execution owner: `loi_he_thong/execution_causal_revalidation.py`.
- Safety owner: `loi_he_thong/mainnet_safety.py`.
- Hard Risk remains final capital-safety authority.

Old journal fields remain readable through compatibility views but cannot gain
new authority merely because they exist.

1. Market data authority
   - Binance Spot BBO and `aggTrade` executed flow.
   - Coinbase Spot ticker and executed matches.
   - Binance Futures BBO, `aggTrade`, Open Interest and funding.
   - Coinbase L2 and quote-normalization collectors remain data/research only.

2. Data-quality and causal guards
   - Freshness, websocket idle recovery, receive-clock guards, epoch/gap reset,
     OI freshness and fail-closed runtime health.
   - Binance Spot cumulative executed flow has one owner:
     `2_suy_luan_mapping/map_dong_tien/delta_cvd.py`.
     Collectors transport trades; they do not maintain a second cumulative CVD.
   - Reconnect/sequence epoch boundaries must not be bridged by Bias memory.

3. Bias V11 — adaptive independent-cash regime
   - Owner: `2_suy_luan_mapping/bias_council.py`.
   - Question: **which direction currently owns meaningful independent cash
     control, and is that control persisting, pulling back, or transferring?**
   - Direction roots are Binance Spot BTCUSDT cash and Coinbase BTC-USD cash.
     Both are required for independent cross-cash price authority; executed
     dual-cash flow is supporting evidence for persistence/control transfer.
   - Binance Futures price/flow, OI, funding and liquidation are derivative or
     positioning context only. They cannot replace a missing cash venue and
     cannot cast an independent Bias direction vote.
   - `price + OI` is no longer a second directional seat. OI may report
     expansion/contraction and candidate build/unwind mechanisms only.
   - Observation lenses `15s / 60s / 180s / 10m / 30m / 60m` are **not votes,
     hard holding times, forecast promises or entry windows**. They are a
     bounded causal memory used to distinguish micro-noise, meaningful
     persistence, pullback and regime control transfer. The live horizon is
     therefore evidence-driven rather than fixed to one hour.
   - A short opposing move inside established longer cash control is a
     `PULLBACK`, not an automatic reversal. A control transfer requires recent
     opposite dual-cash price acceptance plus same-side independent executed
     cash support; it is not confirmed by waiting an arbitrary number of
     seconds.
   - Missing/stale independent cash returns `UNKNOWN_SOURCE` and cannot acquire
     a new direction.
   - Runtime hardening is forbidden from replacing `bias_council.s3` or any
     other Bias reasoning function. `runtime_hardening_v3._install_bias()` is a
     guard proving the canonical owner remains unchanged.
   - Output remains direction/context only. Bias never owns Entry timing,
     execution, position management or Hard Risk.

4. Ignition Core V1
   - Bias is frozen before the impulse. Receive-time 100 ms executed flow,
     venue-normalized surprise, price conversion and clock uncertainty drive
     `IGNITION -> PROBE -> PROVE`.
   - Cash may propose. Futures may alert, but cannot open without independent
     Binance Spot or Coinbase price plus executed-flow response within the
     Ignition causal episode.
   - Fast control-transfer/reversal remains an Ignition question. Bias V11 does
     not make Ignition wait for a 30m/60m observation lens before recognizing a
     strict fast reversal.
   - PROVE remains failed reversion or persistent/accelerating cash execution
     under the existing Ignition contract. Static walls/cancels and BBO size
     without executed flow never authorize Entry.
   - A Futures proposer must obtain fresh OI before Entry. OI decline may
     classify unwind; it does not create a new directional Bias.

5. Residual Edge
   - The observed cash lead over Futures is handoff timing metadata, not
     remaining alpha.
   - Completed net Guardian outcomes plus verified executable costs remain the
     empirical promotion evidence.
   - Shadow bootstrap may collect structurally valid trades. Real money remains
     evidence-gated and is not promoted by this Bias refactor.
   - Fast Ignition and Persistent Metaorder share read-only causal-wave
     representation without merging proof policies or horizons.

6. Market Truth / Action boundary
   - `MARKET_THESIS_V3_AUTHORITY_SEPARATED` freezes the entry mechanism,
     supporting evidence, competing explanations and falsifiers.
   - Market Truth may consume the frozen Bias context but cannot rebuild Bias or
     turn derivative context into independent cash evidence.
   - Action consumes sealed Truth and economics; it does not reinterpret the
     market mechanism.

7. Execution and active position
   - After Action approves, execution verifies the immutable Entry handoff. It
     cannot re-read current Bias and re-judge the same causal proof.
   - Submit-time checks are execution facts only.
   - Guardian owns adverse-move interpretation after entry; PnL cannot rewrite
     Market Truth.
   - Hard SL remains final risk authority. Profit ratchet and fee-aware floor
     remain subordinate capital protection.
   - Loss of support or neutral flow alone does not become a second Risk exit
     council.

8. Journal/state/calibration
   - Only completed canonical shadow outcomes may update empirical calibration.
   - Calibration samples remain version-bound and persistence-safe.
   - Recorder/replay are evidence systems only and never gain trading authority.
   - Any future Bias promotion/tuning must use availability-time-safe matched
     replay and compare the same causal population. Higher apparent accuracy
     caused only by excessive `ABSTAIN` is not improvement.
   - Research may evaluate 30m/60m forward behavior as diagnostics, but those
     labels never become a hard live forecast horizon automatically.

## Explicit non-authorities

- `2_suy_luan_mapping/whale_intent.py`: retired research experiment.
- `1_tai_du_lieu/tai_whale_depth/`: experimental data-only collector.
- `recorder/`: independent, public-data, read-only evidence recorder.
- `recorder/wavefront.py`: parallel shadow research evaluator only.
- `loi_he_thong/entry_council_shadow.py`: retired pre-Ignition evaluator.
- `recorder/liquidity_response.py` and
  `2_suy_luan_mapping/cash_liquidity_response.py`: executed-liquidity-response
  research. A cancellation or disappearing wall is never execution and cannot
  vote Bias merely because L2 exists.
- `recorder/causal_world_model.py`: explanatory shadow world model only.
- `recorder/coinbase_l2.py` and live Coinbase L2 transport: data-only.
- `1_tai_du_lieu/tai_usdt_usd/tai_usdt_usd.py`: quote-normalization data-only;
  it cannot create BTC direction.
- `recorder/replay.py`: deterministic evidence reconstruction only.
- Physical legacy SMC/orderbook/volume-profile modules: not loaded authority.

## Upgrade priority

Prefer better causal state, independent cash evidence, deterministic replay and
flow-quality measurement. Do not add a new score, indicator, veto or horizon
unless a named owner/question and matched evidence prove that it corrects a
specific misunderstanding.

## Historical parameter provenance (non-authoritative)

Historical threshold values in the repository are bootstrap/research metadata,
not claims of predictive probability. Current Bias V11 deliberately treats its
multi-scale lenses as observations rather than independent votes, and any
future replacement must record source windows and reset promotion evidence
through the normal code/config version gate.
