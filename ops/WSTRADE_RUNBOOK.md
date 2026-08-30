# WStrade production runbook

## Safety boundary

The checked-in service starts `AUTO_PROMOTE`. Installing a unit does not start
it. Shadow collection begins immediately, but live execution stays blocked by
replay, soak, statistical, CPU and account gates. The dedicated account
must already be flat; startup refuses existing orders, algo orders or positions
and never auto-flattens startup residue. Live arm additionally requires a
connected Binance private user-data stream. A disconnect seals new entries
while Guardian exits and REST reconciliation remain active.

## Prepare

1. Create the Binance and AWS credential files described in
   `ops/MAINNET_CREDENTIALS.md`.
2. Copy `.env.example` to `.env`; fill only the Lightsail instance and region.
3. Install dependencies: `.venv/bin/pip install -r requirements.txt`.
4. Install units: `sudo ops/install_wstrade_services.sh`.
5. Optionally configure the 26% warning and 30% critical Lightsail alarms:
   `.venv/bin/python ops/configure_lightsail_alarms.py`.
6. Activate credentials and start all services with
   `sudo ops/activate_direct_live.sh`. Do not run VS Code, Codex, compilers, package
   managers, replay, or interactive workloads on the production instance.

## Canonical replay certification

Mainnet promotion is intentionally blocked until a deterministic adapter replays
the complete active chain documented in `STRATEGY_AUTHORITY.md`. The old command
below is retired research and **cannot** produce a promotion-valid report:

`ops/wstrade_replay_validation.py`

A future canonical validator must take the bot singleton lock, use receive-time,
bind code/config versions, reproduce frozen Bias -> Ignition -> Residual Edge ->
Guardian/Risk, and emit `strategy_authority=IGNITION_CORE_V1`. Any
code/config change invalidates that report and restarts shadow validation.
Ignition Core additionally requires an explicit reviewed deployment setting
`WSTRADE_IGNITION_MANUAL_APPROVAL=true`; the default remains fail-closed.

## Observe

- `/home/ubuntu/smc2026_data/health/cpu_status.json`: local rolling CPU,
  budgets, governor mode, top processes and external metric freshness.
- `/home/ubuntu/smc2026_data/health/lightsail_cpu.json`: AWS 15-minute/1-hour
  confirmation.
- `/home/ubuntu/.local/state/wstrade/promotion.json`: promotion blockers and
  validation counters.
- `/home/ubuntu/smc2026_data/health/bot_runtime.json`: runtime readiness,
  canonical Entry decision/flow quality, private-stream status and whether live
  is armed.

`AUTO_PROMOTE` waits for replay and the complete 72-hour promotion ledger. It
also keeps fixed quantity, isolated leverage, account-flat, margin, daily loss,
exchange-stop, partial-fill and unknown-execution recovery checks.

Shadow execution is deliberately conservative: canonical ACCEPTANCE entries use
the same 750-ms maker lifetime as live and require post-placement aggressive
trade-through volume. Canonical RELEASE entries and all exits cross the spread
and include adverse slippage. No retired CATCH lane has authority.
