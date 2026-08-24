"""Optimize Tier-S Futures flow window scan without changing signal semantics."""

VERSION = "ENTRY_FUTURES_FLOW_SCAN_HOOK_V1_REVERSE_WINDOW"


def install(entry_council_module):
    if getattr(entry_council_module, "_futures_flow_scan_hooked", False):
        return VERSION

    def _futures_flow(state, now):
        cutoff = (float(now) - 3.0) * 1000.0
        rows = getattr(state, "danh_sach_khop_lenh_futures", None) or ()
        buy = 0.0
        sell = 0.0
        newest = 0.0

        # Binance aggTrade arrives in chronological order. Scan newest->oldest
        # and stop as soon as the 3s window boundary is crossed. This avoids
        # copying/scanning the full bounded deque on every Entry evaluation.
        for row in reversed(rows):
            try:
                ts = float(row.get("thoi_gian_ms", 0.0) or 0.0)
                if ts <= 0.0:
                    continue
                if newest <= 0.0:
                    newest = ts / 1000.0
                if ts < cutoff:
                    break
                qty = float(row.get("khoi_luong", 0.0) or 0.0)
                if bool(row.get("ban_chu_dong", False)):
                    sell += qty
                else:
                    buy += qty
            except (AttributeError, TypeError, ValueError):
                continue

        if newest <= 0.0 or float(now) - newest > entry_council_module.FUT_MAX_AGE:
            return 0.0, 0.0
        return entry_council_module._flow_imb(buy, sell)

    entry_council_module._futures_flow = _futures_flow
    entry_council_module._futures_flow_scan_hooked = True
    entry_council_module.FUTURES_FLOW_SCAN_VERSION = VERSION
    return VERSION
