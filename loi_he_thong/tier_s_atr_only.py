"""Tier-S ATR-only M1 maintenance loop."""
import asyncio
import time

VERSION = "TIER_S_ATR_ONLY_V1"


def _merge_klines(existing, incoming, limit=64):
    rows = {}
    for row in list(existing or ()) + list(incoming or ()):
        try:
            rows[int(row[0])] = row
        except (IndexError, TypeError, ValueError):
            continue
    return [rows[key] for key in sorted(rows)][-limit:]


async def run(app):
    state = app.state
    klines = []
    first = True
    while True:
        rows = await app.tai_nen_offline.cap_nhat_nen(
            "1m",
            64 if first else 3,
            3,
            is_update=(not first),
        )
        if rows:
            klines = _merge_klines(klines, rows)
            atr = float(app.ATR.tinh_atr_1m(klines) or 0.0)
            if atr > 0.0:
                state.atr_1m = atr
                state.atr_1m_updated_at = time.time()
            first = False
        await asyncio.sleep(app.seconds_to_next_boundary(60, 1.0))
