"""SMC2026 market black-box recorder."""

# V5 adds direction-aware acceleration and separates current cash acceptance
# from surviving control. It is a hard cohort boundary: V4 rows cannot train
# the new execution-proof economics contract.
SCHEMA_VERSION = 5
