# Phase 6 preparation — authority=false

Baseline requested by operator: `ac09d30`.
GitHub connector baseline available during preparation: `0c668ba14227ab29624c165269e878346aa79267`.

The requested local base was not resolvable through GitHub, so these commits are
created as an unattached chain and MUST be reconciled/cherry-picked only after
the local base is compared.

Scope:

1. `entry_action_policy.py` mirrors the current launcher decision mapping.
   It records explicit economics/expiry counterfactuals but cannot alter GO/WAIT.
2. `execution_contradiction_shadow.py` compares current Execution revalidation
   with a contradiction-only ownership contract. Futures-only opposition is
   context, not Market Truth contradiction.
3. `phase6_execution_twins.py` evaluates TAKER_NOW, WAIT100/300/600 and
   MAKER_IF_EXECUTABLE offline using availability-time, contemporaneous BBO,
   explicit maker queue depletion and frozen cost exactly once.
4. `phase6_execution_report.py` is descriptive only. It does not select an
   execution style, forecast, or use MFE as alpha.

Hard invariants:

- `authority=false` for every Phase-6 component.
- No hot launcher wiring.
- No Entry direction, Bias, Guardian authority, Hard Risk or Mainnet change.
- No changes to 0.55, 600ms or consumed 0.35.
- Execution shadow never changes LONG/SHORT.
- Gap/stale/epoch/hash/identity faults remain fail-closed.
- Same opportunity twins retain one immutable identity tuple:
  Market Truth hash, causal episode, WAL, candidate population, causal wave,
  Guardian version, fill model version and frozen cost hash.
- No automatic TAKER/MAKER choice.

Evidence blockers before authority:

- canonical strategy replay;
- Phase-4 shared-thesis deterministic positions;
- same-WAL Guardian active/shadow comparison;
- Phase-5 empirical removals;
- sufficient executable matched outcomes under the canonical replay contract.

Until those exist, report status remains `EXECUTION_URGENCY_UNVERIFIED` unless
the caller explicitly marks an evidence set complete; even then the result is
`OBSERVED_NOT_AUTHORIZED`, never a runtime policy.
