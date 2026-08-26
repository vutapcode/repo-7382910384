# WStrade Ignition Core V1 strategy authority

> Canonical entrypoint: `mainnet_tier_s_lean_launcher.py`.
> File existence, class names and old journal fields do not prove that a module
> is active. Trace imports and `install(...)` calls from this entrypoint.

## Active production path

1. Market data authority
   - Binance Spot BBO and `aggTrade` executed flow.
   - Coinbase Spot ticker and executed matches.
   - Binance Futures BBO, `aggTrade`, Open Interest and funding.
2. Data-quality and causal guards
   - freshness, websocket idle recovery, receive-clock guards, flow alignment,
     epoch/gap reset, OI freshness and fail-closed runtime health.
3. Bias Council
   - S1 cross-venue price, S2 price x OI, S3 executed flow.
   - Output is direction plus confidence/hysteresis only.
4. Ignition Core V1
   - Bias is frozen before the impulse. Receive-time 100 ms executed flow,
     venue-normalized surprise, price conversion and clock uncertainty drive
     `IGNITION -> PROBE -> PROVE`.
   - Cash may propose. Futures may alert, but cannot open without independent
     Binance Spot or Coinbase price plus executed-flow response within 600 ms.
   - The frozen Bias handoff includes cash direction phase, hysteresis, story,
     price/flow votes and OI regime. A pending two-step reversal cannot reuse
     the old direction to authorize Entry.
   - PROVE is failed reversion or two accelerating 100 ms metaorder buckets.
     Failed reversion requires material opposition, adverse excursion, material
     reclaim and material same-side acceptance persisting for at least 400 ms.
   - Consumed fraction uses the greater of venue-local episode displacement and
     bounded 3/6/15-second pre-ignition cash displacement, divided by live 1m
     ATR. This prevents a late proposer from resetting a mature wave to zero;
     missing ATR fails closed. Only IGNITION/EARLY at most 0.35 may enter.
   - A Futures proposer must obtain fresh OI before Entry. A material fresh OI
     decrease classifies the move as unwind and cannot authorize a Futures-led
     entry; cash-led unwind remains a distinct cash-authority path.
   - Coinbase older than five seconds fails closed. Static walls/cancels and
     BBO quantity without executed flow never authorize Entry.
5. Residual Edge
   - The observed cash lead over Futures is handoff timing metadata, not
     remaining alpha. Better Futures confirmation must not reduce Edge.
     Completed net Guardian outcomes plus verified executable costs replace
     the 13/20/35 bps prior. Those values remain historical metadata only.
   - Shadow bootstrap may collect structurally valid trades. Real money needs
     at least 30 persisted outcomes, positive expectancy, non-negative LCB,
     non-negative 25 bps stress and verified commission.
   - A bounded 1-6 second persistent-metaorder lane is recorder telemetry only.
     It cannot open, block or promote a trade until empirical review.
7. Execution and active position
   - Shadow sizing uses balance; exchange filters are enforced only when live
     filters are verified. Unknown filters remain `UNVERIFIED_FILTERS`.
   - Guardian exits only on adverse price with causal deterioration that breaks
     the cash-led thesis recorded at entry. Its old sensitive thresholds are an
     early scout only; they cannot independently authorize an exit.
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
   - Calibration samples must persist across restarts and remain version-bound.
   - Bias OI freshness hooks may adjust only the collector-aligned age bound;
     they must not monkey-patch Ignition, Guardian or execution authority.
   - A qualified causal opportunity follows `reserve -> fill -> commit`.
     Terminal non-fills release the reservation so the same still-live episode
     may retry; temporary BBO health is an execution concern, not identity.

## Explicit non-authorities

- `2_suy_luan_mapping/whale_intent.py`: retired research experiment. Its
  `CATCH` and `SHADOW_PROBE` outputs must never enter production decisions.
- `1_tai_du_lieu/tai_whale_depth/`: experimental data-only collector; not loaded
  by the canonical launcher and cannot vote.
- `recorder/`: independent, public-data, read-only evidence recorder. It cannot
  submit orders or authorize Bias/Entry/Exit.
- `recorder/wavefront.py`: parallel `WAVEFRONT_SHADOW` research evaluator. Its
  cash-proposer/Futures-follower candidates, maker/taker twins and residual-edge
  reports always carry `authority=false`; promotion is manual and requires a
  separate canonical wiring change after the evidence gates pass.
- `loi_he_thong/entry_council_shadow.py`: retired pre-Ignition evaluator kept
  only for historical journal/test decoding. It is not a fallback lane.
- `recorder/liquidity_response.py`: offline executed-depletion/refill research.
  A cancel or disappearing wall is never execution and its output cannot vote.
- `recorder/replay.py`: deterministic evidence transport/reconstruction only.
  A replay becomes promotion evidence only when a canonical strategy adapter
  explicitly evaluates the complete active chain.
- `ops/wstrade_replay_validation.py`: retired Whale/CATCH research replay and
  explicitly invalid for Mainnet promotion.
- Physical legacy SMC/orderbook/volume-profile modules: not loaded. The bootstrap
  exposes inert compatibility shells only.

## Upgrade priority

Prefer empirical persistence, canonical deterministic replay and flow-quality
measurement. Do not add another indicator, veto or confidence field unless a
named active downstream consumer and behavioral test prove its effect.

## Historical parameter provenance (non-authoritative, 2026-08-23)

This bootstrap is evidence-bound, not a promise of alpha:

- Binance plugin, BTCUSDT USD-M: 239 closed 1m bars and 287 closed 5m bars.
  The 5m range distribution was p50 `12.95`, p75 `19.70`, p95 `35.06` bps;
  therefore old reports used `13/20/35` bps. Ignition Core does not authorize
  entries from these candle-range numbers.
- Recorder, latest three hours: 3,600 non-overlapping 3s buckets. Volume p25 was
  Binance Spot `0.01735`, Futures `0.152`, Coinbase `0.001614` BTC. Rounded
  materiality floors are `0.015/0.15/0.002` BTC respectively.
- Absolute 3s imbalance p25 was Futures `0.4859`, Binance Spot `0.6589`, and
  Coinbase `0.7656`; initial minimum/strong flow thresholds are `0.20/0.55`.
- Local OI polling p95 absolute change was `0.008392%`; initial OI build
  threshold is rounded to `0.0085%`.
- Binance exchange information verified BTCUSDT `TRADING`, price tick `0.10`,
  quantity min/step `0.001`, and min notional `50 USDT`. Public plugin data does
  not verify account commission, balance, leverage, margin mode, or order path.

Any future replacement must record its source window and reset promotion
evidence through the normal code/config version gate.
