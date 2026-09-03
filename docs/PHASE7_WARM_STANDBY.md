# Phase 7 warm standby — design only

Status: `EXTERNAL_FENCING_COORDINATOR_UNAPPROVED`.

No host, AWS resource, DNS change, process restart, or runtime wiring is part of
this preparation. Standby starts with `execution_authority=false` and
`entry_authority=false`. This design does **not** claim split-brain is solved.
Cross-host authority requires an external strongly consistent coordinator;
local clocks and local file locks are explicitly insufficient.

Takeover contract:

`STANDBY -> ACQUIRE_FENCE -> ENTRY_SEALED -> EXCHANGE_RECONCILIATION -> POSITION_AND_ORDER_DISCOVERY -> HARD_STOP_VERIFICATION -> EPOCH_REBUILD -> WARM_STATE_READY -> MANUAL_APPROVAL_REQUIRED -> EXECUTION_AUTHORITY`

Any coordinator loss, stale token, reconcile failure, missing protection,
epoch/data-health failure, or unfenced prior owner goes to `NO_ENTRY` or
`SAFETY_ONLY`. A restored WAL is evidence only; it never substitutes exchange
position/order reconciliation.

A production coordinator must issue monotonically increasing fencing tokens and
lease metadata from coordinator state. A stale owner may perform only explicitly
approved safety recovery; it may not submit a new entry.
