# Opportunity Scout V1.5

This package is intentionally staged and fail-closed.

- `SMC_ML_META_MODE=SHADOW` collects both LONG and SHORT causal snapshots and
  an A0-A5 action ladder. It has no Commander or Executor path.
- `merge_dataset.py` merges current plus numbered `SMC2026(n)-` backups,
  deduplicates cumulative copies, and separates `UNRESOLVED_FORENSIC` records.
- `label_actions.py` creates conservative Mainnet counterfactual labels. Touch
  is not a fill unless aggressor quantity consumes the recorded queue.
- `train.py` trains three one-thread XGBoost CPU seeds only after the stage's
  sample gate. It writes a candidate report; it never edits `active.json`.
- `artifact.py` verifies schema/hash and every sample, OOS, churn, tail-risk,
  and CPU gate. Missing or invalid evidence always resolves to `SHADOW`.

Typical offline sequence:

```bash
python3 4_nghien_cuu_ai/ml_meta/merge_dataset.py
python3 4_nghien_cuu_ai/ml_meta/label_actions.py \
  derived/ml_meta/live/YYYY-MM-DD.jsonl \
  --output derived/ml_meta/labeled_actions.jsonl
python3 4_nghien_cuu_ai/ml_meta/train.py \
  derived/ml_meta/labeled_actions.jsonl \
  --output derived/ml_meta/models/candidate
```

No model may promote itself. `active.json` must be separately versioned,
hashed, and approved after all gates pass.
