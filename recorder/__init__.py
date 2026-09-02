"""SMC2026 market black-box recorder."""

# V7 adds the canonical temporal envelope.  It is a hard cohort boundary:
# historical V6 rows may be replayed through compatibility fallbacks, but may
# not claim monotonic availability, source health, or epoch continuity.
SCHEMA_VERSION = 7
