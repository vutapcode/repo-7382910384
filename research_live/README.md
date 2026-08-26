# Live SHADOW research data

The recorder publishes a sanitized rolling snapshot to the dedicated
[`telemetry` branch](https://github.com/vutapcode/repo-7382910384/tree/telemetry/research_live)
about every three minutes.

Read `latest.json` first. It links recent demo trades, causal candidates,
reject/miss counts, Guardian evidence and runtime health. The telemetry branch
is rewritten as one rolling commit, so `main` is not flooded with hundreds of
automated commits.

Raw WAL, API credentials and private account payloads are never published.
