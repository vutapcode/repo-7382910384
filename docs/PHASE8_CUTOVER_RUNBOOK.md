# Phase 8 SHADOW cutover runbook

This runbook is a **dry-run/manual cutover contract**, not an executable deployment script.

## PRECHECK

Require the exact reviewed candidate commit, clean worktree, `SHADOW`, Mainnet disarmed, flat position, current recorder with zero loss/gap violation, complete CPU 15m/1h coverage below 30%, verified artifact hashes, `READY_FOR_MANUAL_SHADOW_CUTOVER`, and a content-addressed manual approval artifact.

## SIMULATED DEPLOY

Read only the candidate authority graph, one-concern cutover manifest and rollback manifest. Candidate graph must expose one Market Truth owner, no Action/Execution/Safety ownership collision, the intended old active edges must be marked for retirement, and rollback must require exchange reconciliation before operator rollback.

No repository, profile, systemd, service or exchange mutation is permitted in the dry-run.

## POSTCHECK EXPECTATIONS

The operator must predeclare the expected strategy/profile version, services, telemetry, sealed-thesis/Guardian trace, zero live exchange mutations, soak duration and rollback triggers. Cutover is SHADOW-only and never creates a `MAINNET_READY` state.

Current Phase-4/5/6/7 evidence is incomplete, therefore a production-like Phase-8 package must remain `NOT_READY` until real artifacts satisfy the gates.
