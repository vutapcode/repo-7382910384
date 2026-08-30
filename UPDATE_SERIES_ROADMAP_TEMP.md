# WStrade update-series roadmap (temporary working file)

> Status: `PHASE_4_BATCH6_SHADOW_ACTIVE / MAINNET_UNPROMOTED`  
> Baseline: `main@a92a7ed`  
> Trading authority: Batch 6 semantics active in SHADOW only; Mainnet remains locked  
> Delete this file only after every approved update phase and its validation are complete.  
> This is a work queue, not strategy authority. `STRATEGY_AUTHORITY.md` and the active launcher remain authoritative.

Source snapshot (read-only):
`/home/ubuntu/.codex/attachments/086373e3-611c-45cf-a085-6c69fb8f1fda/pasted-text.txt`

Execution-spec review (read-only):
`/home/ubuntu/.codex/attachments/16e4946f-6f30-4f52-94c1-05df4f6bda02/pasted-text.txt`

Post-Phase-4 canonical audit (read-only):
`/home/ubuntu/.codex/attachments/8a421947-e8e1-4b2b-a927-85b0eff290a7/pasted-text.txt`

## Execution rules

- Fix P0 correctness, wiring, and data integrity before changing P1 authority.
- Do not tune thresholds while fixing wiring.
- Each issue must be inspected against the active launcher before patching; file existence does not imply authority.
- Each behavior change must be atomic, tested, and replayed on the same WAL and frozen economics contract.
- Keep Mainnet locked throughout this series.
- Do not add external WS, lower thresholds in bulk, extend all TTLs, tune Guardian from charts, or promote P1 without evidence.

## Mandatory execution-spec boundaries

These requirements apply to every phase and must be in place before Phase 1
changes can be considered complete.

### Schema and cohort boundary

- Any renamed inference state or newly added evidence/liquidity field must bump
  the applicable `telemetry_schema_version`, `inference_version`, and
  `economic_contract_version`.
- Old and new WAL rows must never enter the same empirical cohort unless an
  explicit, tested migration proves semantic equivalence.
- Every replay report must state its schema, inference, code, config, and
  economic-contract versions.

### Shadow/live end-to-end parity

- P0.1 through P0.3 must trace and test all three paths:
  - `mainnet_tier_s_shadow_launcher.py`;
  - `3_thuc_thi/wstrade_live_execution.py`;
  - `loi_he_thong/execution_causal_revalidation.py`.
- `authority_basis`, frozen proof, proof hash, execution policy, cost plan, and
  failed dependencies must retain the same meaning from Ignition through
  shadow simulation and the fail-closed live submit boundary.
- Passing shadow tests does not unlock Mainnet or establish authenticated-live
  readiness.

### Evidence provenance invariant

- P1.9 must emit `evidence_id`, `parent_evidence_ids`, and
  `root_evidence_id`.
- Consumers must enforce: observations sharing one `root_evidence_id` cannot
  count as independent corroboration more than once.
- Provenance fields without this consumer invariant do not satisfy P1.9.

### Canonical rejected-candidate adjudication

- P1.27/P1.30 datasets must retain rejected candidates, not only trades.
- A veto or miss is economically adjudicated only with all of:
  `feed_valid`, `causal_valid`, `executable_fill`, `frozen_cost`, current
  Guardian replay, and net outcome.
- Chart movement or MFE alone cannot prove `.35`, `PERP_LED_VETO`,
  `ABSORPTION_VETO`, or an economic miss.

### Maker fill feasibility

- P1.32 `MAKER_IF_EXECUTABLE` requires an order-existing-before-trade test,
  queue/touch/trade-through evidence, TTL, fill ordering, and missed-fill
  outcome.
- Never assume a maker fill merely because a later candle traded through the
  price.

### Full causal latency path

- P1.36 must timestamp:
  `event_time -> WS_receive -> normalize -> bucket_close -> ignition_eval -> proof_freeze -> submit`.
- Report p50/p95/p99 for each segment and end to end before changing scheduler
  cadence.

### Promotion and rollback manifest

- Every proposed P1 authority change requires a manifest containing:
  `baseline_revision`, `candidate_revision`, WAL/data ID, schema versions,
  frozen costs, changed variable, metrics, and `PROMOTE/REJECT` decision.
- A rejected candidate revision must leave canonical behavior unchanged; no
  half-enabled flags, dormant alternate semantics, or mixed cohorts may remain.

### No silent strategic rejection after GO

Every rejection between GO and submit must journal:

```text
reject_stage
blocking_reason
authority_basis
proof_hash
failed_dependency
```

A bare downstream `continue` after GO fails the acceptance gate.

### Pre-Phase 1 readiness checklist

- [ ] P1.1-P1.36 each has an active `file::function` route or an explicit
  `INACTIVE/RETIRED` finding.
- [ ] Schema, inference, and economic-contract bump rules are covered by tests.
- [ ] Shadow/live proof and execution-policy parity tests exist end to end.
- [ ] Rejected-candidate replay and maker fill feasibility contracts are
  deterministic and no-lookahead.

## Phase 1 — P0.1 to P0.5: authority and decision correctness

- [x] **P0.1 — Transition authorization handoff**
  - Inspect `mainnet_tier_s_shadow_launcher.py::_entry_loop()`.
  - A decision with `authority_basis=TRANSITION_CONFIRMED` must not be rejected by a second slow-Bias-side check.
  - `BIAS_ALIGNED` decisions must still revalidate their Bias dependency.
  - Acceptance: opposite slow-Bias transition reaches execution validation without bypassing its remaining gates.

- [x] **P0.2 — Canonical proof contract**
  - Inspect launcher `_entry_quorum_ok()` and `entry_edge_tier.py::_ignition_contract()`.
  - Use one frozen structural validator.
  - Treat `PERSISTENT_METAORDER` consistently as shadow-bootstrap authority only, never live authority.
  - Acceptance: no Ignition GO is rejected only because downstream uses a different proof-name whitelist.

- [x] **P0.3 — Preserve frozen execution policy**
  - Launcher must not infer MAKER/TAKER again after Ignition freezes `execution_policy`.
  - Acceptance: proof result, execution submission, and ledger retain the same policy.

- [x] **P0.4 — Venue-local flow freshness**
  - Inspect `ignition_core.py::_flow_efficiency_snapshot()`.
  - Record `observed_end_ms`, `age_ms`, and `fresh` for every venue.
  - Aggregate only fresh venue state.
  - Acceptance: stale Coinbase state cannot turn current Binance `FADING` into `CONTINUING`.

- [x] **P0.5 — True timestamp for old-side failure**
  - Make `old_side_failure` a stateful proof node that may arise after reversal onset.
  - Timestamp the actual failure observation; do not backdate it to episode start.
  - Acceptance: old side `CONTINUING@t0 -> FADING@t1` becomes usable at `t1` only.

### Phase 1 stop gate

- [x] Active launcher traced end to end.
- [x] No Bias, proof-name, execution-policy, stale-venue, or failure-timestamp semantic drift.
- [x] Targeted tests pass without changing `0.55`, `600 ms`, `0.35`, gap/clock guards, Guardian, or Hard Risk.

Phase 1 local commits (not pushed, services not restarted):

- `6df2c3f` — honor frozen transition authority in launcher.
- `da4e380` — unify frozen entry proof contract.
- `564c413` — preserve frozen execution policy through submit.
- `2412dfa` — exclude stale venue flow from entry state.
- `1d1a9dc` — timestamp old-side failure at observation.
- `54dba8e` — journal post-GO strategic rejections.

## Phase 2 — P0.6 to P0.12: economics, recorder, and test truth

- [x] **P0.6 — One FrozenCostPlan**
  - Unify launcher, hardening, fee-alignment, verified-cost model, Guardian, risk, and close ledger.
  - Acceptance: identical round-trip cost at Entry, Guardian, and ledger; no simultaneous 5/9/verified bps contracts.
  - Local commit: `69da6e9` — freeze one canonical execution-cost plan from Entry through Guardian and close ledger.
  - Binance public exchange metadata was used only to verify BTCUSDT filters; authenticated account commission remains fail-closed/unverified by that source.

- [x] **P0.7 — Coinbase recorder information parity**
  - Subscribe recorder to both `matches` and `ticker` where the live information set requires both.
  - Acceptance: Coinbase BBO/ticker and executed trades coexist in WAL with valid timestamps.
  - Local commit: `1f55744` — recorder subscribes to and emits both Coinbase channels.
  - Acceptance test: `6d1cc42` — a simulated WS session writes both streams with causal timestamps.

- [x] **P0.8 — Correct liquidity refill math**
  - `depletion = pre_qty - post_min_qty`.
  - `refill = current_qty - post_min_qty`.
  - `refill_fraction = refill / depletion` when depletion is positive.
  - Acceptance: queue `100 -> 90 -> 90` yields zero refill.
  - Local commit: `66f60b5` — refill is measured from the post-depletion minimum.
  - Acceptance test: `6d1cc42` — `100 -> 90 -> 90` is zero; recovery to `95` is `0.5`.

- [x] **P0.9 — Canonical replay mirror**
  - Align minimum quantity, 100 ms buckets, 600 ms follower contract, conversion, cost, fills, and current Guardian.
  - Acceptance: every ablation changes one rule only.
  - Local commit: `0cf69da` — add a non-authority canonical mirror with active contracts and one-variable ablation enforcement.

- [x] **P0.10 — Cohort and causal matching integrity**
  - Parent cohorts exclude child samples.
  - Match by `causal_wave_id` or onset signature, not same side/time proximity alone.
  - Acceptance: no cohort double-count and no cross-wave matching.
  - Local commit: `3a172db` — subtract exact samples from parent cohorts and require causal identity for matching.

- [x] **P0.11 — Document persistent authority accurately**
  - Contract: `shadow_bootstrap_authority=true`, `live_authority=false`.
  - Acceptance: docs, tests, producer, and consumer agree.
  - Local commit: `5d4f729` — make the persistent scope explicit and reject it at the live boundary.

- [x] **P0.12 — Mark inactive hooks retired**
  - Add `RETIRED_NON_AUTHORITY` metadata/header to inactive legacy hooks.
  - Test that canonical launcher does not install them.
  - Local commit: `c4c0f0a` — mark inactive hooks retired and test that the canonical launcher does not install them.

### Phase 2 stop gate

- [x] P0.6 and P0.9-P0.12 complete; P0.7/P0.8 remained unchanged and complete.
- [x] `561` tests pass (`2` intentional skips); repository integrity passes for `218` checked files.
- [x] No strategy threshold, Guardian, Hard Risk, service state, or Mainnet lock changed.
- [x] No service restart and no remote push performed.

## Canonical P1 file/function routing

Before editing any target below, trace its import/call path from the active
launcher. If the symbol is inactive, record that fact and do not patch it as
though it had authority.

| Item | Canonical target(s) |
| --- | --- |
| P1.1 | `2_suy_luan_mapping/bias_council.py::s2(), story(), combine()` |
| P1.2 | `loi_he_thong/entry_microstructure.py::price_impact()` |
| P1.3 | `loi_he_thong/ignition_core.py::_flow_efficiency_snapshot()` |
| P1.4 | `loi_he_thong/entry_thesis_gate.py::_liquidity_question()` |
| P1.5 | `loi_he_thong/entry_thesis_gate.py::_independence_question()` |
| P1.6 | `loi_he_thong/flow_lead_engine.py::analyze()` |
| P1.7 | `loi_he_thong/microstructure_regime.py::classify()`, `loi_he_thong/liquidation_context.py` |
| P1.8 | `loi_he_thong/flow_weighting_hook.py` |
| P1.9 | `loi_he_thong/ignition_core.py`, `loi_he_thong/entry_thesis_gate.py`, recorder decision tap and all corroboration consumers |
| P1.10 | `recorder/collector.py` Binance Spot depth handler |
| P1.11 | `recorder/liquidity_response.py::observe()` and tracker creation |
| P1.12 | `loi_he_thong/ignition_signals.py::_Venue.push(), _Venue.finalize()` |
| P1.13 | `loi_he_thong/ignition_core.py::_current_cash_conversion()` and Fast Transition qualified acceptance |
| P1.14 | `loi_he_thong/ignition_signals.py` plus research CausalWave snapshot |
| P1.15 | `recorder/collector.py` Binance Futures aggTrade path |
| P1.16 | `loi_he_thong/ignition_core.py` cash proposer/follower gate |
| P1.17 | `loi_he_thong/ignition_core.py` leader handling after `_leader_from_rows()` |
| P1.18 | `loi_he_thong/execution_causal_revalidation.py::_opposing_ok(), validate_submit()` |
| P1.19 | `loi_he_thong/ignition_core.py::_failed_reversion()` |
| P1.20 | `loi_he_thong/ignition_core.py::_proof()` |
| P1.21 | `loi_he_thong/ignition_signals.py` acceleration calculation and `ignition_core.py` metaorder proof |
| P1.22 | `2_suy_luan_mapping/bias_council.py`, `loi_he_thong/runtime_hardening_v3.py` Bias flow computation |
| P1.23 | Bias quorum mapping in `2_suy_luan_mapping/bias_council.py` and active runtime wiring |
| P1.24 | `loi_he_thong/ignition_core.py` Fast Transition contradiction block |
| P1.25 | `loi_he_thong/ignition_core.py::_current_cash_conversion()` and transition labels |
| P1.26 | Research metadata around `ignition_signals.py`, `ignition_core.py`, `verified_cost_model.py`, and Guardian |
| P1.27 | `recorder/decision_outcomes.py`, `recorder/opportunity_research_matrix.py` |
| P1.28 | `loi_he_thong/ignition_core.py::MAX_CONSUMED_FRACTION`, `recorder/residual_edge.py` |
| P1.29 | `loi_he_thong/entry_edge_tier.py::classify()` |
| P1.30 | `loi_he_thong/entry_microstructure.py::price_impact()`, `loi_he_thong/entry_edge_tier.py::classify()` |
| P1.31 | `loi_he_thong/microstructure_regime.py::classify()` and replay cohort factors |
| P1.32 | `loi_he_thong/verified_cost_model.py`, `loi_he_thong/shadow_execution_model.py` |
| P1.33 | `2_suy_luan_mapping/bias_council.py` confidence generation |
| P1.34 | `loi_he_thong/entry_economics_v2.py` cohort/LCB calculation |
| P1.35 | `3_thuc_thi/ve_si_lenh/guardian_s_tier.py` state transitions |
| P1.36 | `loi_he_thong/tier_s_runtime_prune.py`, `loi_he_thong/host_cpu_governor.py` plus event-path telemetry producers |

## Phase 3 — P1-A and P1-B: semantic cleanup and WS research

This phase must not change GO/WAIT authority unless separately approved after canonical replay.

### P1-A — Observation-neutral semantics

- [ ] **P1.1 REOPENED:** Bias observations must use neutral labels and
  unverified mechanisms must not alter authority. Current `story()/combine()`
  still emits `SELL_FLOW_ABSORBED_BY_LONG_BUILD` /
  `BUY_FLOW_ABSORBED_BY_SHORT_BUILD` and exempts those stories from strong
  opposing-flow handling without a liquidity-response proof.
- [x] P1.2: rename price/flow proxy to `FLOW_PRICE_NONCONVERSION`; require liquidity response before calling absorption.
- [x] P1.3: use primitive flow states such as persistent nonconversion and progress decay.
- [x] P1.4: when live depth is unavailable, record `LIQUIDITY_RESPONSE=UNOBSERVED`.
- [x] P1.5: use `DUAL_CASH_CROSS_VENUE_CORROBORATION`, not statistical independence language.
- [x] P1.6: distinguish displacement dominance from event-ordering lead.
- [x] P1.7: OI/price observations do not imply liquidation without `forceOrder` corroboration.
- [x] P1.8: rename static reliability values to venue-weight priors; keep feed quality dynamic and separate.
- [x] P1.9: add `evidence_id` and `parent_evidence_ids` to prevent one observation becoming several independent votes.
- [ ] **P1.9b:** audit every active corroboration consumer so derived labels
  sharing one `root_evidence_id` cannot count as multiple causal families.
  Provenance fields alone are insufficient if FlowEfficiency, Thesis, regime
  and Edge independently re-count the same executed-flow observation.

Local commits:

- `a83f510` — neutral OI/price observations and separated mechanism hypotheses.
- `2c47972` — nonconversion/progress-decay semantics and unobserved liquidity.
- `e14e5ba` — cross-venue corroboration separated from event ordering.
- `029b8a6` — venue priors separated from dynamic feed quality.
- `5b9d527` — root evidence provenance enforced by corroboration consumers.

### P1-B — Use existing WS according to their information content

- [x] P1.10: recorder stores/derives event-conditioned Binance Spot top-5 depth response.
- [x] P1.11: add Spot aggTrade + depth5 liquidity-response research, `authority=false`.
- [x] P1.12: record impulse response at `+50/+100/+250/+500 ms` rather than only same-bucket first/last price.
- [x] P1.13: use receive time for freshness and corrected event time plus uncertainty for causal synchrony.
- [x] P1.14: record flow-to-microprice/queue/spread response; static imbalance remains non-authoritative.
- [x] P1.15: record Futures `q`, `nq`, and `q-nq` for research ablation only.
- [ ] **P1.15b:** add a bounded research consumer/ablation for available
  Futures `q/nq/q-nq`; it remains `authority=false` and must answer a specific
  positioning/liquidity question before any promotion.

### P0.13 — Bucket availability clock correctness (new blocking correctness item)

- [ ] Split `ignition_signals._Venue.finalize()` timestamps into:
  `bucket_end_ms`, `last_trade_receive_ms`, `available_time_ms`, and
  `corrected_event_time_ms +/- uncertainty`.
- [ ] Freshness, proof age, acceptance and decay must use
  `available_time_ms`; bucket identity must use `bucket_end_ms`; causal ordering
  must use corrected event time plus uncertainty.
- [ ] `available_time_ms` must be the actual finalize/observation time, never
  synthesized as `bucket_start_ms + 100`.
- [ ] Bump the signal/WAL/decision schema boundary and prevent pre-fix and
  post-fix rows from entering one empirical cohort.
- [ ] Tests must cover delayed event-loop finalization, no-lookahead, monotonic
  availability, reconnect epochs and the 300/600 ms causal boundaries.
- [ ] This correctness patch precedes every P1.16-P1.36 authority ablation.

Local commit:

- `255ba4a` — event-conditioned Spot top-5 response, causal clock metadata,
  and optional USD-M `q/nq/q-nq`, all research-only. Recorder schema bumped
  to V3; missing `nq` remains unknown and is never fabricated.

Phase 3 code validation:

- `569` tests pass (`2` intentional skips).
- Repository integrity passes for `219` checked files.
- No Entry/Guardian/Hard Risk authority, service state, Mainnet lock, or
  strategy threshold changed.
- No service restart and no remote push performed.

### Mandatory stop after Phase 3

- [x] Stop code changes for the Phase-3 collection boundary.
- [x] Collect a new, clean WAL batch with the new schemas.
- [ ] Confirm feed validity, timestamp integrity, deterministic replay, and CPU limits.
- [x] Do not promote Phase-4 authority before this evidence exists. Only the
  non-authoritative P1.26/P1.27 measurement framework has been installed.

Active stop-gate collection:

- Cohort: `phase3-v3-255ba4a-20260829T064923Z`.
- Window: `2026-08-29 06:49:23Z` through `10:49:23Z` (exactly 4h).
- Runtime: SHADOW, Mainnet disarmed, starting position flat.
- New rows after the marker are schema V3/runtime code `52c1b555d1b5b7d7`.
- A 30-second low-weight sampler preserves whole-host CPU/service/disk history
  in `/home/ubuntu/smc2026_data/health/phase3_v3_cpu_samples.jsonl`.
- Final deterministic replay remains pending until the collection closes and
  the bot lock can be released safely.

Recorder recursion incident:

- The first cohort was invalidated at `2026-08-29 07:42:42Z` because derived
  research rows re-entered analyzers and recursively duplicated output.
- Local fix `1dc5349` separates WAL publication from research-consumer routing
  and makes liquidity tracker completion reentrancy-safe.
- Replacement cohort runs from `07:43:10Z` through `09:43:10Z` (2h), SHADOW,
  flat at start, runtime code `6800a7d6cd5a222c`.

## Phase 4 — P1-C and P1-D: authority ablation and empirical thresholds

### P1-C — Missing causal market cases, one-variable ablation only

- [ ] P1.16: one-variable ablation of the active cash proposer/follower rule:
  `ONE_CASH_ONLY -> Futures mandatory`, `DUAL_FRESH_CASH -> Futures verifier`,
  `FUTURES_PROPOSER -> cash mandatory`. Do not simply remove the 600 ms guard.
- [x] P1.17: test whether dual fresh cash can validate direction while leader
  identity remains `UNKNOWN`; receive-time synchrony cannot masquerade as
  objective venue leadership.
- [ ] P1.18: ablate execution revalidation so Futures-only opposing buckets
  become warning/urgency deterioration; only fresh opposing cash control or
  Futures opposition plus current-cash collapse may hard reject.
- [x] P1.19: model failed reversion as
  `RECLAIM -> RETEST -> HOLD/FAIL`; counterflow contradicts only when it
  converts and the reclaimed area fails.
- [x] P1.20: separate immutable `causal_origin_proof` from present-tense
  `current_execution_proof`; journal both through GO -> reserve -> submit.
- [x] P1.21: calculate same-side intensity, opposite-side intensity, and net
  directional acceleration separately. Current absolute-intensity delta can
  be raised by a burst from the opposite side.
- [x] P1.22: separate `background_pressure_60s` and
  `marginal_control_1-5s`; neither may silently answer the other's question.
- [x] P1.23: replace equal 2-of-3 venue interpretation with causal families:
  Binance Spot + Coinbase are cash observations and Binance Futures is the
  derivative family. Binance Spot/Futures echoes cannot count as two
  independent confirmations.
- [x] P1.24: grade contradiction from none through Futures-only warning,
  single-cash reclaim and dual-cash control; each grade must have one owner and
  explicit downstream authority.
- [x] P1.25: call one material 100 ms bucket `CURRENT_CASH_ACCEPTANCE`; reserve
  `CONTROL` for survival evidence. Research must distinguish
  `FLOW_LEADS_PRICE`, `COINCIDENT`, `FLOW_CHASES_PRICE` and `NONCONVERSION`
  using event-conditioned +50/+100/+250/+500 ms responses.

### P1-D — Replace unsupported constants only with empirical evidence

- [x] P1.26: classify every authority threshold as safety invariant, data-quality rule, engineering prior, empirical alpha, or risk policy.
- [x] P1.27: record distance-to-boundary and outcome curves around every gate.
- [ ] P1.28: keep consumed `0.35` as prior until out-of-sample residual Guardian-net proves a better rule.
- [ ] P1.29: ablate perp-cash gap bins before changing `PERP_LED_VETO`.
  Rejected candidates require executable Guardian-net counterfactuals because
  traded cohorts cannot validate a vetoed population.
- [ ] P1.30: retain/remove `FLOW_PRICE_NONCONVERSION_VETO` only from executable
  Guardian-net counterfactuals; same-bucket price/flow co-movement is not by
  itself causal conversion or absorption.
- [ ] P1.31: regime starts feature-only; current heuristic
  `price_factor/cost_factor/expectancy_factor` must not become authoritative
  until matched replay cohorts demonstrate net benefit.
- [ ] P1.32: learn execution-cost distribution and matched twins
  `TAKER_NOW/250ms/500ms/MAKER_IF_EXECUTABLE`. Maker feasibility must include
  order-before-trade, conservative queue-ahead, partial depletion,
  cancel/repost priority, TTL and missed-fill outcomes.
- [ ] P1.33: call Bias output a support score until calibrated against realized probability.
- [ ] P1.34: replace near-IID LCB with cluster/block bootstrap by causal wave,
  session, active day and regime. Promotion coverage must require distinct
  active days/sessions/regime blocks; elapsed first-to-last span alone cannot
  satisfy `MIN_DAYS=14`.
- [ ] P1.35: do not add Guardian rules by eye; learn recovery hazard by causal
  mechanism after canonical replay while preserving the V11 recovery state
  machine and Hard Risk authority.
- [ ] P1.36: after P0.13, timestamp and report p50/p95/p99 for
  `event_time -> WS_receive -> normalize -> bucket_available -> ignition_eval
  -> proof_freeze -> reserve -> submit_revalidation -> API_submit`, segmented
  by governor mode. Degrade recorder/diagnostics first only when measured lag
  proves they are responsible.

### Audit findings explicitly not reopened

- Threshold provenance and distance-to-boundary are already implemented by
  `loi_he_thong/causal_threshold_registry.py`,
  `loi_he_thong/decision_boundary_evidence.py` and telemetry V4 at `a92a7ed`;
  the audit's lookup for `recorder/threshold_provenance.py` used a different
  path and does not establish a missing feature.
- Do not reopen frozen execution-cost, Coinbase recorder parity, Spot
  liquidity-response, refill math, causal-wave matching, canonical mirror,
  GO-to-submit timing, Guardian V11 path memory or live fail-closed
  reconciliation without new canonical evidence.

## Canonical execution order

1. P0.1 through P0.5.
2. P0.6.
3. P0.7 through P0.10.
4. P0.11 through P0.12.
5. P1.1 through P1.9 without authority changes.
6. P1.10 through P1.15 with `authority=false`.
7. Collect new WAL.
8. Build P1.26 and P1.27 boundary/ablation evidence.
9. Fix P0.13 availability-time semantics, bump versions and collect a new
   post-fix WAL cohort.
10. Ablate P1.16 through P1.25 one causal rule per run.
11. Only then evaluate P1.28 through P1.36.

Phase 4 evidence checkpoint:

- Local commit `b2c2b27` adds an immutable threshold registry, signed
  decision-boundary observations carried into counterfactual outcomes, and a
  fail-closed one-variable promotion manifest.
- This checkpoint changes no GO/WAIT, Guardian, Hard Risk, cost, or Mainnet
  authority behavior.
- The active replay remains non-canonical for live Ignition authority; P1.16
  through P1.25 therefore remain unpromoted until a replay report contains
  causal-wave matching, executable fill, frozen cost and the current Guardian.

Batch 6 shadow checkpoint:

- Commit `8280829` implements P1.19-P1.25 and confirms the pre-existing P1.17
  route without changing `0.55`, `600 ms`, `0.35`, Guardian or Hard Risk.
- Failed reversion now owns `RECLAIM -> RETEST -> HOLD/FAIL`; origin proof is
  immutable while the execution proof is present-tense and both are journaled
  through pre-submit revalidation.
- Directional flow separates same-side, opposite-side and net acceleration.
  One cash bucket is acceptance, surviving buckets are control, and Spot depth
  response labels remain research-only.
- Bias S3 now counts causal families: Binance Spot/Futures echo alone is not an
  independent quorum; Coinbase+Futures or two cash venues can corroborate.
- Schema boundary: recorder V5, Ignition inference V4, economics V7 and shadow
  state V11. V4/V6 rows cannot train the new cohort.
- Runtime deployment exposed a recorder shutdown defect: in-flight compaction
  checked the shutdown/CPU guard only after Arrow/zstd work, so systemd reached
  its 90-second timeout. Commit `7d6f808` checks before WAL decode and before
  Arrow conversion; no strategy authority changed.
- The canonical import smoke also duplicated three journal/state guard
  subprocesses and exceeded its 15-second timeout on Lightsail. Commit
  `07f048c` skips only those duplicate side effects in the disposable import
  process; normal runtime/systemd startup still executes every guard.
- Verification: 606 tests passed, 2 retired legacy tests skipped. Mainnet
  promotion decision remains `REJECT_UNPROVEN`; a clean post-restart V5 WAL is
  required before empirical evaluation.

## Global acceptance gate

Every promoted change must use:

```text
same WAL
same candidate population
same frozen costs
same Guardian
same fill model
same schema and inference versions
one-variable ablation
no lookahead
causal_wave matched
net EV improvement
false-positive rate controlled
deterministic replay
shadow/live semantic parity
rejected candidates adjudicated
maker queue/fill feasibility
promotion or rollback manifest
no silent post-GO rejection
```

## Completion protocol

- [ ] Every approved item has an atomic commit and test evidence.
- [ ] Every P1 authority experiment has a promotion/rollback manifest.
- [ ] Version boundaries prevent old/new cohort contamination.
- [ ] No post-GO rejection lacks stage, reason, authority, proof, and dependency.
- [ ] New WAL stop gate was respected.
- [ ] Mainnet stayed locked.
- [ ] Final authority and recorder audit passed.
- [ ] Delete `UPDATE_SERIES_ROADMAP_TEMP.md` in the final cleanup commit.
