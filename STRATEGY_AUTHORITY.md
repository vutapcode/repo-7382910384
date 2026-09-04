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

3. Bias V12 — active causal cash wave
   - Direction owner: `2_suy_luan_mapping/bias_council.py`.
   - Observation owner: `2_suy_luan_mapping/cash_wave_observation.py` remains
     authority-free and answers only: **what is the current independent cash
     wave doing now?** Bias consumes that observation and owns direction.
   - Question: **which direction currently owns meaningful independent cash
     control, and is that control converting, pulling back, exhausting or
     transferring?** There is no fixed forecast/holding horizon.
   - Direction roots are Binance Spot BTCUSDT cash and Coinbase BTC-USD cash.
     Both are required for independent cross-cash price authority. Directional
     executed flow must also convert into dual-cash price acceptance before an
     active wave can become `CONTROLLED`.
   - Live causal observation uses non-overlapping newest-to-oldest segments:
     `0-15s`, `15-60s`, `60-180s`, `180-600s`. They represent chronological
     cash-wave evidence, not four votes and not four independent confirmations.
   - Historical overlapping lenses `15s / 60s / 180s / 10m / 30m / 60m` are
     compatibility/replay diagnostics only. They have **zero live directional
     authority** and may not keep an old Bias alive after recent cash conversion
     has failed or control has transferred.
   - `EMERGING_CONTROL` is early information only. It deliberately remains below
     the existing Ignition handoff confidence contract and cannot by itself
     start an Entry episode. Persistent converting cash may become `CONTROLLED`
     without waiting for an arbitrary 30m/60m warm-up.
   - If old-side executed flow persists but no longer converts price, Bias
     releases stale direction as `EXHAUSTION`. Execution-linked liquidity
     research may later distinguish `ABSORPTION/REFILLING`, but live L2 is not
     promoted by this version.
   - Opposite price movement without opposite executed-flow conversion is a
     `PULLBACK`, not a reversal. A control transfer requires recent opposite
     dual-cash flow **and** price conversion, and the immediate causal sequence
     must no longer show old-side conversion. Older 180s/600s displacement is
     context, not a veto.
   - Flow/price contradiction returns `DIVERGING` and releases direction rather
     than silently holding the previous Bias.
   - Binance Futures price/flow, OI, funding and liquidation are derivative or
     positioning context only. They cannot replace a missing cash venue, cannot
     increment cash independence and cannot cast a Bias direction vote.
   - `price + OI` is not a directional seat. OI may report build/unwind candidate
     mechanisms only.
   - Static L2, queue size, walls and cancellations have zero directional
     authority. A disappearing wall is not execution. Liquidity response can be
     promoted only after execution-linked matched replay proves incremental
     value beyond price + executed cash flow.
   - Fees, commission, expected net edge and execution style are **not Bias
     questions**. They remain owned by Entry Edge / verified cost model. This
     preserves Market Truth != Action/Economics while preventing micro waves
     from handoff through the `EMERGING_CONTROL` state.
   - Missing/stale independent cash returns `UNKNOWN_SOURCE` and cannot acquire
     a new direction.
   - Runtime hardening is forbidden from replacing `bias_council.s3` or any
     Bias reasoning function. `runtime_hardening_v3._install_bias()` guards the
     canonical owner identity.
   - Output remains direction/context only. Bias never owns Entry timing,
     execution, position management or Hard Risk.

4. Ignition Core V1
   - Bias is frozen before the impulse. Receive-time 100 ms executed flow,
     venue-normalized surprise, price conversion and clock uncertainty drive
     `IGNITION -> PROBE -> PROVE`.
   - Cash may propose. Futures may alert, but cannot open without independent
     Binance Spot or Coinbase price plus executed-flow response within the
     Ignition causal episode.
   - Fast control-transfer/reversal remains an Ignition question. Bias V12 does
     not make Ignition wait for a stale historical observation lens before
     recognizing a strict fast reversal.
   - PROVE remains failed reversion or persistent/accelerating cash execution
     under the existing Ignition contract. Static walls/cancels and BBO size
     without executed flow never authorize Entry.
   - The persistent lane is a shadow/demo bootstrap consumer of the shared
     `CausalWaveSnapshot`: `shadow_bootstrap_authority=true` and
     `live_authority=false`. It collects executable evidence but cannot grant
     real-money authority before canonical replay approval.
   - A Futures proposer must obtain fresh OI before Entry. OI decline may
     classify unwind; it does not create a new directional Bias.

5. Residual Edge / economics
   - The observed cash lead over Futures is handoff timing metadata, not
     remaining alpha.
   - Verified commission, executable cost, minimum net edge and empirical
     forward-edge remain Entry/Action economics. Bias does not predict profit
     merely because a cash wave exists.
   - Completed net Guardian outcomes plus verified executable costs remain the
     empirical promotion evidence.
   - The shadow/demo bootstrap may collect structurally valid trades. Real money remains
     evidence-gated and is not promoted by this Bias refactor.
   - Fast Ignition and persistent cash-wave representation share observations
     without merging proof policies or horizons.

6. Market Truth / Action boundary
   - `MARKET_THESIS_V3_AUTHORITY_SEPARATED` freezes the entry mechanism,
     supporting evidence, competing explanations and falsifiers.
   - Market Truth may consume the frozen Bias context but cannot rebuild Bias or
     turn derivative context into independent cash evidence.
   - Action consumes sealed Truth and economics; it does not reinterpret the
     market mechanism.

7. Execution and active position
   - After Action approves, launcher no longer re-reads current Bias to judge
     the same causal proof. It verifies the immutable Entry handoff instead.
     A missing, mismatched or mutated handoff blocks before submit and is
     attributed to the dependency owner.
   - Every reserved GO is measured again immediately before submit using the
     shared Ignition classifier. Journal telemetry records GO-to-submit delay,
     flow state at GO/submit and cash age. This measurement cannot authorize a
     rejected Entry; the post-proof raw scan remains a contradiction-only
     safety guard, not a second strategy council.
   - Shadow sizing uses balance; exchange filters are enforced only when live
     filters are verified. Unknown filters remain `UNVERIFIED_FILTERS`.
   - Current Guardian action remains the rollback baseline during the Phase-4
     migration. A parallel `GUARDIAN_SHARED_THESIS_SHADOW_V1` consumes the exact
     sealed Entry truth plus subsequent canonical measurements and has
     `authority=false`; it is not blended with the current decision.
   - `MARKET_THESIS_V3_AUTHORITY_SEPARATED` freezes the Entry mechanism,
     expected sequence and
     falsifiers. `market_thesis.observe()` alone maps later evidence to
     `SUPPORT / DIVERGENCE / CONTROL_TRANSFER / FALSIFY / UNKNOWN`; PnL, entry
     price, best-R, holding time and account history cannot change that status.
     Capital protection remains a separate Risk authority. Guardian authority
     cannot move to this lane until same-WAL executable counterfactual outcomes
     prove capture ratio and hard-stop acceptance.
   - A runner with an active one-way profit floor gets a longer confirmation
     window during an ordinary pullback. Extreme cross-venue price plus strong
     adverse flow bypasses that shield through the fast-kill path.
   - A frozen established 180/60/15 second trend gets the same soft confirmation
     window while current context still agrees. Reversal candidates and fast
     causal danger disable that shield; Hard Risk remains final authority.
   - Hard SL is final risk authority; profit ratchet and fee-aware floor remain
     subordinate risk protection. Support widens the trailing gap, while the
     floor only ratchets forward and can never loosen afterward.
   - Shadow daily PnL is audit-only and never limits test trades. The configured
     daily-loss breaker is enforced only by the authenticated live execution path.
   - Loss of support or neutral flow cannot independently trigger an exhaustion
     exit inside Risk; historical `whale_*` checkpoint fields are migration-only.
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
flow-quality measurement. Do not add a score, indicator, veto or horizon unless
a named owner/question and matched evidence prove that it corrects a specific
misunderstanding.

## Historical parameter provenance (non-authoritative)

Historical threshold values in the repository are bootstrap/research metadata,
not claims of predictive probability. Bias V12 treats long observation lenses
as diagnostics only; live direction comes from causal cash-wave conversion and
falsification. Any future replacement must record source windows and reset
promotion evidence through the normal code/config version gate.
