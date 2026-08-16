"""
[AI_CONTEXT]
- MODULE: 2_suy_luan_mapping / tong_ket_chi_huy
- ROLE: Chấm điểm hội lưu (Confluence Scoring). Đánh giá xem lệnh có đáng bắn hay không.
- I/O: Đọc state, mode_info, bias -> Trả về dictionary (Điểm tổng, Điểm Core, Điểm Shark).
"""

import time


FOOTPRINT_TTL_SECONDS = 15.0
REVERSAL_EVENT_TTL_SECONDS = 15.0
VALUE_AREA_SWEEP_TTL_SECONDS = 30.0
POSITIONING_ADVISORY_TTL_SECONDS = 20.0
PERSISTENT_FLOW_TTL_SECONDS = 5.0
ZONE_REACTION_TTL_SECONDS = 15.0
TRAP_ADVISORY_TTL_SECONDS = 8.0
TREND_FLOW_DOMINANCE_MIN = 0.60
MICROFLOW_REVERSAL_SHARE_MAX = 0.75
MICROFLOW_REVERSAL_MIN_BTC = 0.50
MICROFLOW_REVERSAL_P90_FRACTION = 0.25
OBI_THRESHOLD = 0.30
OBI_PERSISTENCE_SECONDS = 2.0
OBI_MIN_SAMPLES = 6
OBI_MIN_COVERAGE_SECONDS = 0.50
OBI_ALIGNED_RATIO_MIN = 0.70
OBI_MEAN_MIN = 0.25
BURST_FOOTPRINT_CORRELATION_SECONDS = 5.0
BREAKOUT_EVENT_TTL_SECONDS = 15.0
M15_CONTEXT_POINTS = 0.5
POC_CONTEXT_MAX_POINTS = 0.7
POC_CONTEXT_DEADBAND_ATR = 0.25
POC_CONTEXT_FULL_DISTANCE_ATR = 3.0


def _m15_context_modifier(state, bias):
    """M15 chỉ là context nửa điểm, không phải bằng chứng CORE."""
    trend = str(getattr(state, 'trend_m15', 'NEUTRAL') or 'NEUTRAL').upper()
    aligned = (
        (bias == 'LONG' and trend == 'BULLISH')
        or (bias == 'SHORT' and trend == 'BEARISH')
    )
    opposed = (
        (bias == 'LONG' and trend == 'BEARISH')
        or (bias == 'SHORT' and trend == 'BULLISH')
    )
    if aligned:
        return M15_CONTEXT_POINTS
    if opposed:
        return -M15_CONTEXT_POINTS
    return 0.0


def _poc_context_modifier(state, bias):
    """POC là lực hút context, không phải bằng chứng CORE độc lập.

    Ngoài deadband, lệnh đi về POC được cộng và lệnh đi xa POC bị trừ.
    Cường độ tăng tuyến tính theo khoảng cách/ATR và bị chặn ở +/-0.7.
    """
    poc = float(getattr(state, 'poc', 0.0) or 0.0)
    atr = float(getattr(state, 'atr_1m', 0.0) or 0.0)
    bid = float(getattr(state, 'best_bid', 0.0) or 0.0)
    ask = float(getattr(state, 'best_ask', 0.0) or 0.0)
    price = (bid + ask) / 2.0 if bid > 0.0 and ask > 0.0 else max(bid, ask)
    if poc <= 0.0 or atr <= 0.0 or price <= 0.0 or bias not in ('LONG', 'SHORT'):
        return 0.0

    distance_atr = abs(price - poc) / atr
    if distance_atr <= POC_CONTEXT_DEADBAND_ATR:
        return 0.0
    ramp = (
        (distance_atr - POC_CONTEXT_DEADBAND_ATR)
        / (POC_CONTEXT_FULL_DISTANCE_ATR - POC_CONTEXT_DEADBAND_ATR)
    )
    magnitude = POC_CONTEXT_MAX_POINTS * min(1.0, max(0.0, ramp))
    toward_poc = (
        (price < poc and bias == 'LONG')
        or (price > poc and bias == 'SHORT')
    )
    return round(magnitude if toward_poc else -magnitude, 4)


def score_allows(armed_mode, score):
    """Một contract duy nhất cho cả Commander và Executor pre-flight."""
    core_score = int((score or {}).get('core', 0) or 0)
    m15_modifier = float((score or {}).get('m15_modifier', 0.0) or 0.0)
    poc_modifier = float((score or {}).get('poc_modifier', 0.0) or 0.0)
    effective_core = float(
        (score or {}).get(
            'effective_core', core_score + m15_modifier + poc_modifier
        )
        or 0.0
    )
    if armed_mode in ('TREND-BREAKOUT', 'TRANSITION-BREAKOUT'):
        trend = ((score or {}).get('evidence_quality', {}) or {}).get(
            'trend', {}
        ) or {}
        independent_confirmation = bool(
            trend.get('legacy_flow')
            or trend.get('persistent_flow')
            or trend.get('footprint')
        )
        return bool(
            core_score >= 2
            and trend.get('breakout')
            and independent_confirmation
        )
    # Pullback/fade có đúng một CORE độc lập được phép lấy mẫu nhỏ. Breakout
    # vẫn giữ contract chặt phía trên: cấu trúc + flow độc lập, CORE >= 2.
    eligible_mode = armed_mode in (
        'TREND-PULLBACK', 'TRANSITION-PULLBACK', 'NEUTRAL-FADE',
    )
    if not eligible_mode:
        return False
    if core_score >= 2:
        return True
    # Weak probe bắt buộc có event ID truy vết được; một aggregate/burst không
    # có danh tính không đủ quyền mở lệnh dù số điểm hiển thị là CORE=1. M15
    # ngược chiều trừ 0.5 nên giữ setup yếu lại; M15 không bao giờ tự tạo CORE.
    return bool(
        core_score == 1
        and effective_core >= 1.0
        and (score or {}).get('event_ids')
    )


def _aligned(event, bias, now, ttl):
    return bool(
        event.get('active')
        and event.get('direction') == bias
        and now - float(event.get('ts', 0.0)) <= ttl
    )


def _append_event_id(event_ids, event):
    if event.get('event_id'):
        event_ids.append(event['event_id'])


def _event_available(state, event):
    event_id = event.get('event_id')
    return not event_id or event_id not in (
        getattr(state, 'consumed_market_events', {}) or {}
    )


def _footprint_aligned(state, bias, now):
    event = getattr(state, 'fp_last_imbalance', {}) or {}
    aligned = (
        _event_available(state, event)
        and
        now - float(event.get('ts', 0.0)) <= FOOTPRINT_TTL_SECONDS
        and (
            (bias == 'LONG' and event.get('dir') == 'buy')
            or (bias == 'SHORT' and event.get('dir') == 'sell')
        )
    )
    return aligned, event


def _obi_quality(state, bias, now):
    """Phân biệt OBI bền qua nhiều snapshot với wall nông tức thời."""
    raw_samples = []
    for item in getattr(state, 'obi_history', ()) or ():
        try:
            ts, value = item[:2]
            ts, value = float(ts), float(value)
        except (TypeError, ValueError, IndexError):
            continue
        if 0.0 <= now - ts <= OBI_PERSISTENCE_SECONDS:
            raw_samples.append((ts, value))
    raw_samples.sort(key=lambda item: item[0])
    sign = 1.0 if bias == 'LONG' else -1.0
    signed_now = sign * float(getattr(state, 'obi', 0.0) or 0.0)
    signed_top3 = sign * float(getattr(state, 'obi_top3', 0.0) or 0.0)
    signed_top10 = sign * float(getattr(state, 'obi_top10', 0.0) or 0.0)
    coverage = (
        raw_samples[-1][0] - raw_samples[0][0]
        if len(raw_samples) >= 2 else 0.0
    )
    signed = [sign * value for _, value in raw_samples]
    aligned_ratio = (
        sum(value >= OBI_THRESHOLD for value in signed) / len(signed)
        if signed else 0.0
    )
    mean_signed = sum(signed) / len(signed) if signed else 0.0
    persistent = bool(
        len(signed) >= OBI_MIN_SAMPLES
        and coverage >= OBI_MIN_COVERAGE_SECONDS
        and signed_now >= OBI_THRESHOLD
        and aligned_ratio >= OBI_ALIGNED_RATIO_MIN
        and mean_signed >= OBI_MEAN_MIN
    )
    # Top 3 hứa hẹn nhưng toàn Top 10 không đồng thuận: dấu hiệu wall
    # nông/spoof-like. Chỉ advisory, tuyệt đối không veto lệnh.
    shallow_mismatch = bool(
        (signed_top3 >= OBI_THRESHOLD or signed_top10 >= OBI_THRESHOLD)
        and signed_now <= 0.05
    )
    return {
        'persistent': persistent,
        'shallow_mismatch': shallow_mismatch,
        'samples': len(signed),
        'coverage_seconds': round(coverage, 3),
        'aligned_ratio': round(aligned_ratio, 3),
        'mean_signed': round(mean_signed, 4),
        'current_signed': round(signed_now, 4),
        'top3_signed': round(signed_top3, 4),
        'top10_signed': round(signed_top10, 4),
    }


def _score_neutral_core(state, bias, now, details, event_ids):
    """CORE reversal cho VAH/VAL; không ép flow ban đầu phải cùng hướng."""
    score = 0
    reaction = getattr(state, 'absorption_reaction', {}) or {}
    if (
        _event_available(state, reaction)
        and _aligned(reaction, bias, now, REVERSAL_EVENT_TTL_SECONDS)
    ):
        score += 1
        details.append("CORE+1: Passive absorption đã có phản ứng giá xác nhận")
        _append_event_id(event_ids, reaction)

    divergence = getattr(state, 'flow_divergence', {}) or {}
    if (
        _event_available(state, divergence)
        and _aligned(
            divergence, bias, now, float(divergence.get('ttl', 2.0) or 2.0)
        )
    ):
        score += 1
        details.append("CORE+1: Aggressive flow lớn nhưng giá không tiến triển")
        _append_event_id(event_ids, divergence)

    value_sweep = getattr(state, 'value_area_sweep', {}) or {}
    m1_sweep = getattr(state, 'sweep_m1', {}) or {}
    value_sweep_aligned = _aligned(
        value_sweep, bias, now, VALUE_AREA_SWEEP_TTL_SECONDS
    ) and _event_available(state, value_sweep)
    m1_sweep_aligned = bool(
        m1_sweep.get('flag')
        and m1_sweep.get('direction') == bias
        and now - float(m1_sweep.get('ts', 0.0)) < 120.0
        and _event_available(state, m1_sweep)
    )
    if value_sweep_aligned or m1_sweep_aligned:
        score += 1
        details.append("CORE+1: Sweep/reclaim vùng thanh khoản đúng hướng")
        _append_event_id(event_ids, value_sweep if value_sweep_aligned else m1_sweep)

    footprint_ok, footprint = _footprint_aligned(state, bias, now)
    if footprint_ok:
        score += 1
        details.append(f"CORE+1: Footprint reversal {footprint.get('dir').upper()}")
        _append_event_id(event_ids, footprint)
    return score


def _score_trend_core(
    state, bias, now, details, event_ids, selected_mode=''
):
    """Giữ logic continuation cũ cho TREND-PULLBACK/BREAKOUT."""
    score = 0
    breakout = getattr(state, 'breakout_m1', {}) or {}
    m1_breakout_confirmed = bool(
        selected_mode in ('TREND-BREAKOUT', 'TRANSITION-BREAKOUT')
        and _event_available(state, breakout)
        and breakout.get('flag')
        and breakout.get('direction') == bias
        and 0.0 <= now - float(breakout.get('ts', 0.0)) <= BREAKOUT_EVENT_TTL_SECONDS
    )
    transition = str(getattr(state, 'structure_transition', 'NONE') or 'NONE')
    structural_breakout_confirmed = bool(
        selected_mode == 'TRANSITION-BREAKOUT'
        and int(getattr(state, 'structure_break_streak', 0) or 0) >= 2
        and float(getattr(state, 'structure_broken_level', 0.0) or 0.0) > 0.0
        and transition == (
            'NEUTRAL_TRANSITION_BULLISH'
            if bias == 'LONG' else 'NEUTRAL_TRANSITION_BEARISH'
        )
    )
    breakout_confirmed = m1_breakout_confirmed or structural_breakout_confirmed
    if breakout_confirmed:
        score += 1
        if structural_breakout_confirmed:
            details.append(
                "CORE+1: Hai nến M15 đóng xác nhận breakout từ NEUTRAL"
            )
        else:
            details.append(
                "CORE+1: Breakout M1 đóng ngoài cấu trúc "
                f"({breakout.get('detection', 'DISPLACEMENT')})"
            )
            _append_event_id(event_ids, breakout)

    vol_3s = float(getattr(state, 'current_vol_3s', 0.0) or 0.0)
    vol_pct90 = float(getattr(state, 'vol_pct90', 0.0) or 0.0)
    sell_recent = float(getattr(state, 'current_cvd_sell_3s', 0.0) or 0.0)
    buy_recent = float(getattr(state, 'current_cvd_buy_3s', 0.0) or 0.0)
    cvd_buy = float(getattr(state, 'cvd_buy_30m', 0.0) or 0.0)
    cvd_sell = float(getattr(state, 'cvd_sell_30m', 0.0) or 0.0)
    aligned_30m = (
        cvd_buy > cvd_sell if bias == 'LONG' else cvd_sell > cvd_buy
    )
    flow_total = buy_recent + sell_recent
    aligned_recent = buy_recent if bias == 'LONG' else sell_recent
    opposing_recent = sell_recent if bias == 'LONG' else buy_recent
    flow_dominance = aligned_recent / flow_total if flow_total > 0.0 else 0.0
    opposing_share = opposing_recent / flow_total if flow_total > 0.0 else 0.0
    opposing_material_floor = max(
        MICROFLOW_REVERSAL_MIN_BTC,
        MICROFLOW_REVERSAL_P90_FRACTION * vol_pct90,
    )
    # Flow 15-60s la tin hieu cham. Khong dung no de vao dung cuoi impulse neu
    # 3s hien tai da dao chieu rat ro. Floor theo P90 giu filter nay mem trong
    # thi truong it volume; chi chan mot micro-reversal vua lon vua ap dao.
    microflow_reversal_conflict = bool(
        flow_total > 0.0
        and opposing_share >= MICROFLOW_REVERSAL_SHARE_MAX
        and opposing_recent >= opposing_material_floor
    )
    # CVD 30m là context chậm, còn cửa sổ 3s quá dễ rung. Chúng thuộc cùng
    # một họ flow và chỉ tạo MỘT CORE khi xác nhận lẫn nhau; không cho hai
    # tín hiệu tương quan tự ghép thành đủ 2 CORE để mở lệnh.
    legacy_flow_confirmed = (
        vol_pct90 > 0.0
        and vol_3s > vol_pct90
        and aligned_30m
        and flow_dominance >= TREND_FLOW_DOMINANCE_MIN
    )
    persistent_flow = getattr(state, 'persistent_flow', {}) or {}
    persistent_flow_confirmed = (
        _event_available(state, persistent_flow)
        and _aligned(
            persistent_flow, bias, now,
            float(persistent_flow.get('ttl', PERSISTENT_FLOW_TTL_SECONDS)
                  or PERSISTENT_FLOW_TTL_SECONDS),
        )
        and not microflow_reversal_conflict
    )
    # Hai cách đo đều thuộc cùng một họ aggressive flow nên chỉ cộng tối đa
    # MỘT CORE. Đường cũ được giữ nguyên để bản nâng cấp không làm mất lệnh.
    if legacy_flow_confirmed or persistent_flow_confirmed:
        score += 1
        if persistent_flow_confirmed:
            details.append(
                "CORE+1: Flow 15s/60s bền và giá tiến triển cùng hướng"
            )
            _append_event_id(event_ids, persistent_flow)
        else:
            details.append(
                "CORE+1: Flow trend xác nhận kép "
                "(CVD 30m + Volume 3s P90, dominance ≥60%)"
            )

    # Reaction tại POC/VAH/VAL là một họ độc lập với flow. Chỉ áp dụng đúng
    # setup pullback đã arm; không để reaction cũ lọt sang breakout.
    zone_reaction = getattr(state, 'zone_reaction', {}) or {}
    if (
        selected_mode in ('TREND-PULLBACK', 'TRANSITION-PULLBACK')
        and _event_available(state, zone_reaction)
        and _aligned(
            zone_reaction, bias, now,
            float(zone_reaction.get('ttl', ZONE_REACTION_TTL_SECONDS)
                  or ZONE_REACTION_TTL_SECONDS),
        )
    ):
        score += 1
        details.append(
            "CORE+1: POC/VAH/VAL đã rejection và dịch chuyển đúng hướng"
        )
        _append_event_id(event_ids, zone_reaction)

    footprint_ok, footprint = _footprint_aligned(state, bias, now)
    if footprint_ok:
        score += 1
        details.append(f"CORE+1: Footprint Stacked Imbalance {footprint.get('dir').upper()}")
        _append_event_id(event_ids, footprint)

    sweep = getattr(state, 'sweep_m1', {}) or {}
    if (
        sweep.get('flag')
        and sweep.get('direction') == bias
        and now - float(sweep.get('ts', 0.0)) < 120.0
        and _event_available(state, sweep)
    ):
        score += 1
        details.append("CORE+1: Sweep râu M1 đúng hướng trend")
        _append_event_id(event_ids, sweep)
    return score, {
        'breakout': breakout_confirmed,
        'breakout_detection': (
            'M15_NEUTRAL_TWO_CLOSE_CONFIRMATION'
            if structural_breakout_confirmed else (
                breakout.get('detection') if m1_breakout_confirmed else None
            )
        ),
        'legacy_flow': legacy_flow_confirmed,
        'persistent_flow': persistent_flow_confirmed,
        'persistent_flow_raw': bool(
            _event_available(state, persistent_flow)
            and _aligned(
                persistent_flow, bias, now,
                float(persistent_flow.get('ttl', PERSISTENT_FLOW_TTL_SECONDS)
                      or PERSISTENT_FLOW_TTL_SECONDS),
            )
        ),
        'microflow_reversal_conflict': microflow_reversal_conflict,
        'opposing_flow_share_3s': round(opposing_share, 4),
        'opposing_flow_material_floor': round(opposing_material_floor, 6),
        'flow_dominance': round(flow_dominance, 4),
        'flow_volume_ratio': (
            round(vol_3s / vol_pct90, 4) if vol_pct90 > 0.0 else 0.0
        ),
        'aligned_flow_3s': round(aligned_recent, 6),
        'opposing_flow_3s': round(
            sell_recent if bias == 'LONG' else buy_recent, 6
        ),
        'zone_reaction': bool(
            selected_mode in ('TREND-PULLBACK', 'TRANSITION-PULLBACK')
            and _event_available(state, zone_reaction)
            and _aligned(
                zone_reaction, bias, now,
                float(zone_reaction.get('ttl', ZONE_REACTION_TTL_SECONDS)
                      or ZONE_REACTION_TTL_SECONDS),
            )
        ),
        'zone_reaction_displacement_atr': (
            float(zone_reaction.get('displacement_atr', 0.0) or 0.0)
            if zone_reaction else 0.0
        ),
        'zone_reaction_max_adverse_atr': (
            float(zone_reaction.get('max_adverse_atr', 0.0) or 0.0)
            if zone_reaction else 0.0
        ),
        'footprint': footprint_ok,
        'footprint_age_seconds': (
            max(0.0, now - float(footprint.get('ts', 0.0)))
            if footprint_ok else None
        ),
    }


def cham_diem(state, mode_info, bias):
    """CORE quyết định entry; SHARK chỉ bổ sung độ mạnh/size."""
    if bias not in ('LONG', 'SHORT'):
        return {
            'total': 0.0, 'core': 0, 'effective_core': 0.0,
            'm15_modifier': 0.0, 'poc_modifier': 0.0,
            'shark': 0, 'detail': [],
        }

    details = []
    event_ids = []
    advisory_support = []
    advisory_adverse = []
    advisory_event_ids = []
    shark_score = 0
    now = float(getattr(state, 'snapshot_time', time.time()))
    modes = (mode_info or {}).get('modes', [])
    selected_mode = (mode_info or {}).get('mode', '')
    # Khi Commander chấm một setup đã ARM, ``mode`` là mode cụ thể của setup
    # còn ``modes`` vẫn mô tả regime nền và có thể chứa NEUTRAL-FADE. Ưu tiên
    # mode cụ thể để TRANSITION-BREAKOUT không bị chấm nhầm bằng neutral scorer.
    neutral_fade = (
        selected_mode == 'NEUTRAL-FADE'
        or (not selected_mode and 'NEUTRAL-FADE' in modes)
    )
    if neutral_fade:
        core_score = _score_neutral_core(state, bias, now, details, event_ids)
        trend_quality = {}
    else:
        core_score, trend_quality = _score_trend_core(
            state, bias, now, details, event_ids, selected_mode
        )

    # Raw matched volume at a displayed wall is telemetry, not independent
    # conviction.  The price-confirmed absorption_reaction is already scored as
    # CORE in reversal modes; counting the raw event again inflated size from a
    # single book/flow episode.

    obi_quality = _obi_quality(state, bias, now)
    if obi_quality['persistent']:
        shark_score += 1
        details.append("SHARK+1: OBI bền qua nhiều snapshot cùng hướng")
    elif obi_quality['shallow_mismatch']:
        advisory_adverse.append('OBI_SHALLOW_MISMATCH')
        details.append(
            "ADVISORY ADVERSE: OBI Top 3 có wall nhưng Top 10 không đồng thuận"
        )

    # Wall vừa biến mất và OBI đổi là cùng một họ depth, không phải hai bằng
    # chứng độc lập. Wall thô chỉ nerf size; VETO nằm ở kiem_duyet_veto và bắt
    # buộc thêm price follow-through + aggTrade corroboration.
    wall_pull = getattr(state, 'wall_pull_flag', {}) or {}
    wall_age = now - float(wall_pull.get('ts', 0.0) or 0.0)
    dangerous_wall = (
        (bias == 'LONG' and wall_pull.get('side') == 'buy')
        or (bias == 'SHORT' and wall_pull.get('side') == 'sell')
    )
    if (
        wall_pull.get('active')
        and 0.0 <= wall_age <= 1.0
        and dangerous_wall
        and _event_available(state, wall_pull)
    ):
        if 'OBI_SHALLOW_MISMATCH' in advisory_adverse:
            advisory_adverse.remove('OBI_SHALLOW_MISMATCH')
        wall_family = (
            'WALL_PULL_CONFIRMED'
            if wall_pull.get('confirmed_for_veto') is True
            else 'WALL_PULL_UNCONFIRMED'
        )
        advisory_adverse.append(wall_family)
        details.append(
            "ADVISORY ADVERSE: Wall Pull "
            + ("đã có giá+flow xác nhận" if wall_family == 'WALL_PULL_CONFIRMED'
               else "chưa đủ xác nhận độc lập")
        )
        if wall_pull.get('event_id'):
            advisory_event_ids.append(wall_pull['event_id'])

    oi = float(getattr(state, 'open_interest', 0.0) or 0.0)
    macro_bias = getattr(state, 'macro_bias', 'NEUTRAL')
    macro_fresh = now - float(getattr(state, 'thoi_gian_vi_mo_cuoi', 0.0) or 0.0) <= 15.0
    if oi > 0.0 and macro_fresh and macro_bias == bias:
        shark_score += 1
        details.append("SHARK+1: OI/Funding tươi cùng hướng")

    # Flow đập mạnh nhưng giá đi ngược, hoặc giá chấp nhận xuyên qua vùng, là
    # dấu hiệu bẫy. Chúng chỉ giảm tiền đánh, không veto/giảm CORE để tránh miss.
    for field, family, label in (
        ('flow_price_trap', 'FLOW_PRICE_TRAP', 'Flow mạnh nhưng giá thất bại'),
        ('zone_acceptance_trap', 'ZONE_ACCEPTANCE_TRAP', 'Giá chấp nhận xuyên vùng'),
    ):
        event = getattr(state, field, {}) or {}
        ttl = float(event.get('ttl', TRAP_ADVISORY_TTL_SECONDS)
                    or TRAP_ADVISORY_TTL_SECONDS)
        if (
            event.get('active')
            and event.get('blocked_bias') in ('LONG', 'SHORT')
            and now - float(event.get('ts', 0.0)) <= ttl
        ):
            adverse = event.get('blocked_bias') == bias
            (advisory_adverse if adverse else advisory_support).append(family)
            details.append(
                f"ADVISORY {'ADVERSE' if adverse else 'SUPPORT'}: {label}"
            )
            if event.get('event_id'):
                advisory_event_ids.append(event['event_id'])

    # Burst 3s và footprint tươi thường là hai cách nhìn cùng một
    # cú bounce. Vẫn giữ đủ 2 CORE để không giảm tần suất; nếu
    # chưa có flow bền hay rejection vùng thì chỉ hạ tiền thăm dò.
    footprint_age = trend_quality.get('footprint_age_seconds')
    correlated_bounce = bool(
        selected_mode in ('TREND-PULLBACK', 'TRANSITION-PULLBACK')
        and trend_quality.get('legacy_flow')
        and trend_quality.get('footprint')
        and not trend_quality.get('persistent_flow')
        and not trend_quality.get('zone_reaction')
        and footprint_age is not None
        and footprint_age <= BURST_FOOTPRINT_CORRELATION_SECONDS
    )
    if correlated_bounce:
        advisory_adverse.append('CORRELATED_BURST_FLOW_FOOTPRINT')
        details.append(
            "ADVISORY ADVERSE: Flow 3s + Footprint có thể cùng một cú bounce"
        )

    # Positioning mới chỉ được phép nerf size khi ngược hướng. Cùng hướng chỉ log,
    # không tăng SHARK/CORE cho tới khi telemetry chứng minh có edge.
    for field, family, label in (
        ('positioning_cvd_divergence', 'CVD_DIVERGENCE_5M', 'CVD divergence 5m'),
        ('liquidation_recovery', 'LIQUIDATION_RECOVERY_5M', 'OI liquidation/recovery 5m'),
    ):
        event = getattr(state, field, {}) or {}
        if (
            event.get('active')
            and event.get('direction') in ('LONG', 'SHORT')
            and now - float(event.get('ts', 0.0)) <= POSITIONING_ADVISORY_TTL_SECONDS
        ):
            aligned = event.get('direction') == bias
            (advisory_support if aligned else advisory_adverse).append(family)
            details.append(
                f"ADVISORY {'SUPPORT' if aligned else 'ADVERSE'}: {label} "
                f"→ {event.get('direction')}"
            )
            if event.get('event_id'):
                advisory_event_ids.append(event['event_id'])

    m15_modifier = _m15_context_modifier(state, bias)
    poc_modifier = _poc_context_modifier(state, bias)
    effective_core = float(core_score) + m15_modifier + poc_modifier
    if m15_modifier > 0.0:
        details.append("M15+0.5: Context M15 cùng hướng")
    elif m15_modifier < 0.0:
        details.append("M15-0.5: Context M15 ngược hướng")
    if poc_modifier > 0.0:
        details.append(
            f"POC+{poc_modifier:.2f}: Hướng giao dịch quay về POC"
        )
    elif poc_modifier < 0.0:
        details.append(
            f"POC{poc_modifier:.2f}: Hướng giao dịch chạy xa POC"
        )

    return {
        'total': effective_core + shark_score,
        'core': core_score,
        'effective_core': effective_core,
        'm15_modifier': m15_modifier,
        'poc_modifier': poc_modifier,
        'shark': shark_score,
        'detail': details,
        'event_ids': list(dict.fromkeys(event_ids)),
        'evidence_quality': {
            'trend': trend_quality,
            'obi': obi_quality,
        },
        'advisory': {
            'support': list(dict.fromkeys(advisory_support)),
            'adverse': list(dict.fromkeys(advisory_adverse)),
            'size_nerf_pct': min(
                40, len(set(advisory_adverse)) * 20
            ),
            'event_ids': list(dict.fromkeys(advisory_event_ids)),
        },
    }
