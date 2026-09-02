# Phase 0 Evidence Freeze

> Scope: read-only authority/evidence baseline for the architecture recovery
> series. This artifact does not grant trading authority and does not change
> strategy behavior.

## Identity and rollback

- Series pre-Phase-1 baseline commit: `90a04c29ae29766df45a14b528119cbf1325cfc8`.
- Phase-1 implementation head before this evidence-only change:
  `1ef67d953b5fa755442768c27cd5d59a2e39572c`.
- Phase-1 rollback commit: `90a04c29ae29766df45a14b528119cbf1325cfc8`.
- Canonical entrypoint: `mainnet_tier_s_lean_launcher.py`.
- Runtime mode required by this freeze: `MAINNET_SHADOW` with
  `WSTRADE_MODE=SHADOW`, `SMC_ENABLE_TRADING=false`,
  `SMC_MAINNET_ARMED=false`, and `SMC_MAINNET_EXCLUSIVE_ACCOUNT=false`.
- Strategy config identity is taken from the live heartbeat; recorder config
  identity is separate and must not be treated as the strategy config hash.

## Active authority path

```text
Binance Spot / Coinbase Spot / Binance Futures / OI / funding
  -> collectors and normalized shared state
  -> Bias Council (background direction/context)
  -> Ignition Core (causal episode, transition and Entry proof)
  -> Entry Edge Tier (frozen executable economics)
  -> action in canonical shadow launcher
  -> shadow execution model OR WStrade live execution transaction
  -> Guardian S Tier (entry-thesis deterioration/exit)
  -> Shadow Risk + exchange hard stop (capital safety)
  -> durable journal/runtime state + recorder decision tap
```

The chain above is traced from the lean launcher and its imported hardened/risk
wrappers. A similarly named file elsewhere in the tree is not evidence of
authority.

## Question -> owner contract

| Question | Sole conclusion owner | Evidence inputs | Allowed consumers | Wrong-answer impact |
| --- | --- | --- | --- | --- |
| Is each source usable now? | Collector/data-health guards | receive age, gap, epoch, clock/idle state | Bias, Ignition, Guardian, SRE | stale or cross-gap evidence can become fake causality |
| What is the background direction/context? | `bias_council.py` | 180/60/15s cash price, executed flow, OI context | Ignition and Guardian thesis context | slow/noisy context can block or mislabel a transition |
| Is there one valid causal Entry episode? | `ignition_core.py` | frozen Bias, cash conversion, cross-cash acceptance, Futures response, OI mechanism | Edge and launcher | false entry, missed transition, or duplicated wave |
| Does the proved setup remain economically executable? | `entry_edge_tier.py` + `verified_cost_model.py` | frozen proof, BBO, execution style, fee/slippage contract, empirical cohort | action/launcher | fee-blind entry or false cost reject |
| Should this decision act now? | canonical shadow launcher | immutable Entry result, Edge authorization, reservation, current causal revalidation | execution | duplicated/replayed/stale intent |
| How is the intent submitted and protected? | `wstrade_live_execution.py` | order state, account state, protection transaction, control-plane health | reconciliation and Hard Risk | unprotected or duplicated physical exposure |
| Is the entry thesis healthy after fill? | `guardian_s_tier.py` | frozen thesis, cash/flow conversion, OI/context, recovery path | exit action | premature exit or holding a dead thesis |
| Must capital be stopped regardless of thesis? | exchange hard stop + `shadow_risk_guard.py` | position geometry, hard SL, daily live loss, reconciliation | execution exit only | catastrophic capital loss |
| What happened and can it be replayed? | durable journal + recorder | decision inputs/outputs, market WAL, fill/cost/Guardian events | offline audit only | unverifiable win/loss/miss attribution |
| May host capacity admit new real-money work? | host CPU/SRE guards | 15m/1h whole-host CPU, process/feed/storage health | live admission only | host overload or safety work starvation |

## Behavior-changing hooks on the canonical path

- Runtime/task pruning and WebSocket idle recovery.
- Durable journal, runtime-state persistence and storage-pressure entry guard.
- Dynamic shadow sizing plus fee/risk-price alignment.
- Bias/Regime OI freshness, flow alignment/weighting and shared regime snapshot.
- Completed-outcome calibration consumed only by the declared Edge contract.
- Startup consistency/state guards, clock/gap guards and critical-loop liveness.

Each hook is installed from `mainnet_tier_s_lean_launcher.py` or its canonical
hardened wrapper. Research subscribers remain `authority=false`.

## Explicit non-authorities checked

- recorder, wavefront, causal-world model and liquidity-response research;
- retired Whale/CATCH replay and `entry_council_shadow.py`;
- legacy SMC, POC/VAH/VAL, footprint/flash-flow/oracle and legacy executors;
- public Binance plugin responses (market evidence only, never authenticated
  account/order/stop proof).

## Runtime and WAL freeze

Populate from one post-restart observation before declaring the evidence range
usable:

| Field | Frozen value |
| --- | --- |
| Git source baseline | `1ef67d953b5fa755442768c27cd5d59a2e39572c` |
| Python code version | `67d13f5f13510a02` |
| Strategy config version | `2e587b1f637e347d` |
| Recorder code/config version | `67d13f5f13510a02` / `b32fe2a64a8c13c4` |
| WAL receive-time range | `[1788345006069, 1788345031069]` ms; 447 records |
| WAL transport hash run 1/run 2 | `b7867754d4c0eb354853a1b98fd50f6c7e5d598f87727449e35628ff45950778` / same |
| Runtime mode / real trading | `MAINNET_SHADOW` / `false`; live armed `false` |
| Opportunities / qualified / captured | `11887 / 44 / 35` at freeze snapshot; raw capture ratio `79.55%` |
| Near misses | `267` cumulative persisted telemetry; not a version-clean performance cohort |
| Trades / wins / losses | `35 / 2 / 33` at freeze snapshot |
| Guardian latency p95 | `UNOBSERVED_AFTER_RESTART`; flat position, zero new samples |
| Host CPU 15m / 1h | `29.54% / 29.54%` after audit cooldown; post-restart coverage incomplete |

The bounded WAL range contains Binance Spot BBO/flow, Coinbase ticker/flow,
Futures BBO/flow, mark, OI, premium and bot decision events. Every selected row
has the frozen Python code version. Recorder health for this epoch was `OK`,
with zero sequence gaps, queue drops and writer errors. The hash proves stable
transport ordering only; it does not prove deterministic strategy decisions.
The CPU snapshot is below the 30% long-window ceiling, but the newly restarted
governor did not yet have a complete 15-minute/1-hour observation window. Its
high p95 retained the deliberate integrity/hash burst, so this snapshot is not
promotion or soak-test evidence.

## Binance public availability probe

The selected Binance plugin exposed public market-data methods only. A bounded
probe returned BTCUSDT USD-M BBO `77660.90 / 77661.00` with exchange timestamp
`1788329140182`, and OI `108842.808` with timestamp `1788329042994`. This proves
that the public connector and timestamp-bearing payloads were available at the
freeze time. It does not verify account commission, private-stream lag, order
ACK, fill, stop placement or stop verification.

## Replay status and stop condition

- Deterministic receive-order transport exists in `recorder/replay.py`.
- `ops/wstrade_replay_validation.py` explicitly replays a retired Whale
  experiment and is not valid evidence for the active Ignition strategy.
- No complete canonical adapter currently replays
  `Bias -> Ignition -> Edge -> executable fill -> Guardian -> Hard Risk`.

Therefore Phase 0 must remain **PARTIAL / BLOCKED_CANONICAL_ADAPTER_MISSING**
even if the same bounded WAL produces the same transport hash twice. Later
phases must not call the retired replay a strategy baseline or promotion proof.
