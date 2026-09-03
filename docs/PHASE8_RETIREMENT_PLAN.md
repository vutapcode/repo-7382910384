# Phase 8 ordered retirement plan

Preparation only. No authority is changed by these manifests.

Order:

1. **C8.1 Shared-thesis Guardian cutover** — Guardian consumes the sealed Market Thesis; legacy Guardian causal council is retired; Hard Risk unchanged.
2. **C8.2 Canonical Action Policy cutover** — Action owns ACT/WAIT/MAKER/TAKER/ABANDON; launcher only delegates/serializes.
3. **C8.3 Contradiction-only Execution cutover** — Execution owns identity/freshness/BBO/fill/new contradiction and never re-derives LONG/SHORT/metaorder.
4. **C8.4 Approved pseudo-rule removals** — one empirical Phase-5 rule per commit; PRIOR_ONLY/UNKNOWN rules remain untouched.
5. **C8.5 Final active graph cleanup** — retire legacy truth-owner imports/calls/config, preserve read-only journal compatibility, no weighted ensemble and no runtime fallback to old brain.

Every cutover is one authority concern with its own content-addressed evidence, manual approval and rollback package. A cutover manifest must name baseline/candidate commits, exact files/imports/config flags, WAL and candidate-population identity, schema/inference/economic/frozen-cost/Guardian/Hard-Risk/fill versions, empirical acceptance, flat-state precondition, post-deploy checks, and rollback commit/config/schema.

Phase 8 preparation does not delete legacy modules or temporary roadmaps.
