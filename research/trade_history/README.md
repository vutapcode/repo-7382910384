# Sanitized shadow trade history

`shadow_trades.jsonl` is a compact research copy exported from the local
shadow journal. It is intentionally versioned with the repository so changes
to Entry, cost and Guardian logic can be reviewed against historical outcomes.

This dataset is **virtual trading only** and is not evidence of current-strategy
profitability. Historical rows may have been produced by older code. The file
contains only whitelisted lifecycle, thesis, execution-cost and outcome fields;
it excludes credentials, account/order identifiers, balances and raw market
WAL data.

Refresh manually after a completed shadow trade:

```bash
python3 ops/export_research_trade_history.py \
  /home/ubuntu/.local/state/smc2026/mainnet_shadow/events.jsonl \
  research/trade_history/shadow_trades.jsonl
```

Review the diff before committing. Runtime services never write into the git
working tree.
