"""Hợp nhất bằng chứng dòng tiền để bảo kê vị thế, không dùng một cờ đơn lẻ."""

import time


FLASH_ADVERSE_SHARE_MIN = 0.65
FLASH_NET_P95_MULT = 1.50
FLASH_NET_THRESHOLD_RATIO = 0.35
FOOTPRINT_GUARDIAN_TTL_SECONDS = 15.0


def _persistent_obi(state, side, now):
    samples = [
        value for ts, value in getattr(state, 'obi_history', ())
        if now - ts <= 2.0
    ]
    if len(samples) < 5:
        return None
    aligned = sum(
        1 for value in samples
        if (side == 'LONG' and value >= 0.3) or (side == 'SHORT' and value <= -0.3)
    )
    adverse = sum(
        1 for value in samples
        if (side == 'LONG' and value <= -0.3) or (side == 'SHORT' and value >= 0.3)
    )
    required = max(3, int(len(samples) * 0.6))
    if aligned >= required:
        return 'support'
    if adverse >= required:
        return 'adverse'
    return None


def evaluate(state, side, now=None):
    now = time.time() if now is None else now
    support = []
    adverse = []
    book_support = False
    book_adverse = False

    buy = float(getattr(state, 'current_cvd_buy_3s', 0.0) or 0.0)
    sell = float(getattr(state, 'current_cvd_sell_3s', 0.0) or 0.0)
    threshold = max(
        float(getattr(state, 'p95_value', 3.0) or 3.0) * 3.0,
        float(getattr(state, 'vol_pct90', 0.0) or 0.0),
    )
    aligned_flow = buy > sell if side == 'LONG' else sell > buy
    opposing_flow = sell if side == 'LONG' else buy
    supporting_flow = buy if side == 'LONG' else sell
    total_flow = opposing_flow + supporting_flow
    adverse_share = opposing_flow / total_flow if total_flow > 0.0 else 0.0
    net_adverse = opposing_flow - supporting_flow
    net_floor = max(
        float(getattr(state, 'p95_value', 3.0) or 3.0) * FLASH_NET_P95_MULT,
        threshold * FLASH_NET_THRESHOLD_RATIO,
    )
    # Guardian uses the same material dominance contract as entry VETO. A
    # 50.1/49.9 tumbling-window split must never trigger a fee-paying cut.
    adverse_flow = bool(
        opposing_flow > threshold
        and adverse_share >= FLASH_ADVERSE_SHARE_MIN
        and net_adverse >= net_floor
    )
    vol = float(getattr(state, 'current_vol_3s', 0.0) or 0.0)
    vol_p90 = float(getattr(state, 'vol_pct90', 0.0) or 0.0)
    if vol_p90 > 0 and vol > vol_p90 and aligned_flow:
        support.append('FLOW')
    if adverse_flow:
        adverse.append('FLASH_FLOW')

    absorption = getattr(state, 'absorption_reaction', {}) or {}
    if absorption.get('active') and now - float(absorption.get('ts', 0.0)) <= 15.0:
        aligned = (
            absorption.get('direction') == side
        )
        (support if aligned else adverse).append('ABSORPTION_REACTION')

    footprint = getattr(state, 'fp_last_imbalance', {}) or {}
    if now - float(footprint.get('ts', 0.0)) <= FOOTPRINT_GUARDIAN_TTL_SECONDS:
        aligned = (
            (side == 'LONG' and footprint.get('dir') == 'buy')
            or (side == 'SHORT' and footprint.get('dir') == 'sell')
        )
        if footprint.get('dir') in ('buy', 'sell'):
            (support if aligned else adverse).append('FOOTPRINT')

    obi_result = _persistent_obi(state, side, now)
    if obi_result == 'support':
        book_support = True
    elif obi_result == 'adverse':
        book_adverse = True

    wall = getattr(state, 'wall_pull_flag', {}) or {}
    if wall.get('active') and now - float(wall.get('ts', 0.0)) <= 1.0:
        dangerous = (
            (side == 'LONG' and wall.get('side') == 'buy')
            or (side == 'SHORT' and wall.get('side') == 'sell')
        )
        if dangerous and wall.get('confirmed_for_veto') is True:
            book_adverse = True

    # OBI và wall-pull cùng nguồn depth: chỉ được tính một họ bằng chứng.
    if book_adverse:
        adverse.append('BOOK')
    elif book_support:
        support.append('BOOK')

    macro = getattr(state, 'macro_bias', 'NEUTRAL')
    if macro == side and now - float(getattr(state, 'thoi_gian_vi_mo_cuoi', 0.0) or 0.0) <= 15.0:
        support.append('OI_FUNDING')

    support = list(dict.fromkeys(support))
    adverse = list(dict.fromkeys(adverse))
    if len(adverse) >= 2:
        status = 'SHARK_ADVERSE'
    elif len(support) >= 2 and not adverse:
        status = 'SHARK_SUPPORTIVE'
    else:
        status = 'NEUTRAL'
    result = {
        'side': side,
        'status': status,
        'support_count': len(support),
        'adverse_count': len(adverse),
        'support': support,
        'adverse': adverse,
        'ts': now,
    }
    state.shark_context = result
    return result
