"""Commander: score snapshot và giành quyền EXECUTING đúng một lần/setup."""

import asyncio
import hashlib
import importlib.util
import logging
import os
import time
from pathlib import Path

from loi_he_thong import mainnet_safety

try:
    from loi_he_thong.order_identity import client_order_id as forensic_order_id
except ModuleNotFoundError:
    _identity_spec = importlib.util.spec_from_file_location(
        'commander_order_identity',
        Path(__file__).resolve().parents[2] / 'loi_he_thong' / 'order_identity.py',
    )
    _identity_mod = importlib.util.module_from_spec(_identity_spec)
    _identity_spec.loader.exec_module(_identity_mod)
    forensic_order_id = _identity_mod.client_order_id


CURRENT_DIR = Path(__file__).resolve().parent


def load_module(module_name, file_path):
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


chon_che_do = load_module("chon_che_do", CURRENT_DIR / "chon_che_do.py")
kiem_duyet_veto = load_module("kiem_duyet_veto", CURRENT_DIR / "kiem_duyet_veto.py")
cham_diem_mod = load_module("cham_diem", CURRENT_DIR / "cham_diem.py")
continuous_mod = load_module(
    "cham_diem_continuous", CURRENT_DIR / "cham_diem_continuous.py"
)
continuous_v2_mod = load_module(
    "cham_diem_continuous_v2", CURRENT_DIR / "cham_diem_continuous_v2.py"
)
snapshot_mod = load_module("decision_snapshot", CURRENT_DIR / "decision_snapshot.py")
risk_mod = load_module(
    "continuous_risk",
    CURRENT_DIR.parents[1] / "3_thuc_thi" / "quan_ly_vi_the" / "tinh_toan_rui_ro.py",
)

xac_dinh_che_do = chon_che_do.xac_dinh_che_do
kiem_tra_veto = kiem_duyet_veto.kiem_tra_veto
cham_diem = cham_diem_mod.cham_diem

RETENTION_MIN_POLICY_QTY_BTC = 0.001
RETENTION_MAX_NOTIONAL_PCT = 2.0



def _has_momentum_reclaim(snapshot, armed_bias, armed_mode):
    """
    Kiem tra xem gia co vua nay ra khoi zone theo huong co loi khong.
    Chi ap dung cho NEUTRAL-FADE va TREND-PULLBACK. BREAKOUT bo qua.
    Nguyen tac: Tranh 'bat dao roi' khi gia con dang cam dau vao zone.
    """
    # Khong ap dung cho BREAKOUT (can toc do, khong cho xac nhan)
    if 'BREAKOUT' in str(armed_mode).upper():
        return True
    bid = float(getattr(snapshot, 'best_bid', 0.0) or 0.0)
    ask = float(getattr(snapshot, 'best_ask', 0.0) or 0.0)
    prev_bid = float(getattr(snapshot, 'prev_best_bid', bid) or bid)
    prev_ask = float(getattr(snapshot, 'prev_best_ask', ask) or ask)
    if bid <= 0.0 or ask <= 0.0 or prev_bid <= 0.0:
        return True  # Fail-open: thieu data thi khong chan
    mid = (bid + ask) / 2.0
    prev_mid = (prev_bid + prev_ask) / 2.0
    if armed_bias == 'LONG':
        # Gia phai dang nhich len (mid >= prev_mid) de xac nhan nay
        return mid >= prev_mid
    elif armed_bias == 'SHORT':
        # Gia phai dang nhich xuong de xac nhan nay
        return mid <= prev_mid
    return True


def _scorer_version():
    return str(os.getenv('SMC_SCORER_VERSION', 'CORE_V1') or 'CORE_V1').upper()


def _continuous_enabled():
    return _scorer_version() in (
        continuous_mod.LIVE_VERSION, continuous_v2_mod.LIVE_VERSION,
    )


def _opportunity_retention_enabled():
    return str(
        os.getenv('SMC_ENTRY_LIFECYCLE', 'LEGACY')
    ).strip().upper() == 'OPPORTUNITY_RETENTION_V1'


def _retention_allocation(score, setup, snapshot, decision_price):
    """Permit the explicit 0.001 BTC probe only inside the 2% policy cap."""
    requested = float(score.get('target_notional_pct', 0.0) or 0.0)
    entry_style = score.get('entry_style_policy') or setup.get('entry_style')
    if (
        not _opportunity_retention_enabled()
        or str(entry_style or '').upper() != 'PASSIVE_RETEST'
        or requested <= 0.0
    ):
        return requested, dict(getattr(snapshot, 'exchange_filters', {}) or {}), False
    filters = dict(getattr(snapshot, 'exchange_filters', {}) or {})
    filters['min_qty'] = max(
        float(filters.get('min_qty', 0.0) or 0.0),
        RETENTION_MIN_POLICY_QTY_BTC,
    )
    equity = float(getattr(snapshot, 'balance_usdt', 0.0) or 0.0)
    minimum = risk_mod.quantity_feasibility(equity, requested, decision_price, filters)
    required_pct = float(
        minimum.get('minimum_executable_notional_pct', 0.0) or 0.0
    )
    effective = min(RETENTION_MAX_NOTIONAL_PCT, max(requested, required_pct))
    return effective, filters, effective > requested + 1e-9


def _continuous_module():
    return (
        continuous_v2_mod
        if _scorer_version() == continuous_v2_mod.LIVE_VERSION
        else continuous_mod
    )


def _watch_enabled():
    return _continuous_enabled()


def _minimal_mainnet_audit_enabled():
    return str(
        os.getenv('SMC_MINIMAL_MAINNET_AUDIT', 'false')
    ).strip().lower() in ('1', 'true', 'yes', 'on')


def _score_allows(armed_mode, score, snapshot):
    # Wrapper giữ contract test/legacy; logic thật nằm cùng scorer để Executor
    # không thể diễn giải mode khác Commander.
    return cham_diem_mod.score_allows(armed_mode, score)


def _weak_gap_requires_reaction(setup, score):
    """Gap recovery van duoc arm, nhung CORE=1 phai co rejection tai zone.

    CORE>=2 khong bi doi contract. Direct crossing va sweep/reclaim cung khong
    bi anh huong, nen day la guard nhe dung vao truong hop flow-only qua zone.
    """
    if setup.get('activation_reason') != 'GAP_RECOVERED':
        return False
    if int((score or {}).get('core', 0) or 0) != 1:
        return False
    trend = ((score or {}).get('evidence_quality') or {}).get('trend') or {}
    return not bool(trend.get('zone_reaction'))


def _weak_probe_plus_candidate(score):
    """Pre-qualify live 2.5% tier before its separate economic check."""
    score = score or {}
    trend = ((score.get('evidence_quality') or {}).get('trend') or {})
    obi = ((score.get('evidence_quality') or {}).get('obi') or {})
    adverse = ((score.get('advisory') or {}).get('adverse') or [])
    checks = {
        'one_core': int(score.get('core', 0) or 0) == 1,
        'm15_aligned': float(score.get('m15_modifier', 0.0) or 0.0) >= 0.5,
        'zone_reaction': bool(trend.get('zone_reaction')),
        'clean_rejection': (
            float(trend.get('zone_reaction_displacement_atr', 0.0) or 0.0) >= 0.25
            and float(trend.get('zone_reaction_max_adverse_atr', 999.0) or 0.0) <= 0.10
        ),
        'obi_persistent': bool(
            obi.get('persistent')
            and float(obi.get('aligned_ratio', 0.0) or 0.0) >= 0.90
            and float(obi.get('mean_signed', 0.0) or 0.0) >= 0.50
        ),
        'flow_support': bool(
            float(trend.get('flow_dominance', 0.0) or 0.0) >= 0.85
            and float(trend.get('flow_volume_ratio', 0.0) or 0.0) >= 0.80
        ),
        'no_adverse_advisory': not adverse,
    }
    return {
        'candidate': all(checks.values()),
        'checks': checks,
        'metrics': {
            'm15_modifier': float(score.get('m15_modifier', 0.0) or 0.0),
            'rejection_displacement_atr': float(
                trend.get('zone_reaction_displacement_atr', 0.0) or 0.0
            ),
            'rejection_max_adverse_atr': float(
                trend.get('zone_reaction_max_adverse_atr', 0.0) or 0.0
            ),
            'obi_aligned_ratio': float(obi.get('aligned_ratio', 0.0) or 0.0),
            'obi_mean_signed': float(obi.get('mean_signed', 0.0) or 0.0),
            'flow_dominance': float(trend.get('flow_dominance', 0.0) or 0.0),
            'flow_volume_ratio': float(
                trend.get('flow_volume_ratio', 0.0) or 0.0
            ),
        },
    }


def _position_size_details(armed_mode, score):
    """Conservative, evidence-tiered allocation as a percent of USDT balance.

    CORE decides the main tier.  SHARK evidence may add only a small bonus and
    can never bypass the per-mode cap.  This keeps ordinary trades useful for
    collecting execution data without allowing noisy secondary evidence to
    create an oversized position.
    """
    core = max(0, int(score.get('core', 0) or 0))
    shark = max(0, int(score.get('shark', 0) or 0))
    if core <= 1:
        base, tier = 2, 'WEAK_PROBE'
    elif core == 2:
        base, tier = 3, 'PROBE'
    elif core == 3:
        base, tier = 5, 'CONFIRMED'
    elif core == 4:
        base, tier = 8, 'HIGH_CONVICTION'
    else:
        base, tier = 12, 'MAX_CONVICTION'

    if armed_mode.startswith('TREND'):
        cap = 15
    else:
        cap = 10
    # SHARK không được phóng đại một setup chỉ có một bằng chứng CORE.
    shark_bonus = 0 if core <= 1 else min(shark, 3)
    pre_nerf_size = min(base + shark_bonus, cap)
    nerf = max(0, min(
        100,
        int((score.get('advisory', {}) or {}).get('size_nerf_pct', 0) or 0),
    ))
    plus_candidate = _weak_probe_plus_candidate(score)
    if plus_candidate['candidate']:
        # Tier thật 2.5%; Executor vẫn phải xác nhận economics ở chính quantity
        # này. Nếu edge <12bps nó tự hạ về WEAK_PROBE 2%, không bỏ lệnh.
        tier = 'WEAK_PROBE_PLUS'
        base = 2.5
        size_pct = 2.5
    else:
        size_pct = max(1, int(pre_nerf_size * max(0, 100 - nerf) / 100))
    return {
        'size_pct': size_pct,
        'tier': tier,
        'base_pct': base,
        'shark_bonus_pct': shark_bonus,
        'cap_pct': cap,
        'nerf_pct': nerf,
        'policy_version': 'data_first_v1',
        'weak_probe_plus_candidate': bool(plus_candidate['candidate']),
        'weak_probe_plus_size_pct': 2.5,
        'weak_probe_plus_min_edge_bps': 12.0,
        'fallback_tier': 'WEAK_PROBE' if plus_candidate['candidate'] else None,
        'fallback_size_pct': 2.0 if plus_candidate['candidate'] else None,
        'weak_probe_plus_qualification': plus_candidate,
    }


def _position_size(armed_mode, score):
    return _position_size_details(armed_mode, score)['size_pct']


def _client_order_id(
    setup_id, generation, state=None, opportunity_id=None, role='ENTRY',
):
    if state is None:
        # Compatibility for pure legacy tests; runtime always supplies state.
        digest = hashlib.sha1(
            f"legacy-run:{opportunity_id}:{setup_id}:{generation}:{role}".encode('utf-8')
        ).hexdigest()[:20]
        return f"smc_entry_{digest}"
    return forensic_order_id(
        state, role, opportunity_id=opportunity_id, setup_id=setup_id,
        generation=generation,
    )


def _legacy_setup(state, armed_mode, armed_bias):
    """Giữ contract cũ cho unit/mock, runtime luôn truyền setup thật."""
    state.setup_generation = getattr(state, 'setup_generation', 0) + 1
    return {
        'setup_id': f"legacy:{armed_mode}:{armed_bias}:{state.setup_generation}",
        'generation': state.setup_generation,
        'state': 'ARMED_WINDOW',
        'mode': armed_mode,
        'bias': armed_bias,
        'zone': float(getattr(state, 'poc', 0.0) or 0.0),
        'kind': 'zone',
    }


DECISION_CONTEXT_FIELDS = (
    'best_bid', 'best_ask', 'atr_1m', 'poc', 'vah', 'val', 'trend_m15',
    'swing_high_m15', 'swing_low_m15', 'current_vol_3s', 'vol_pct90',
    'current_cvd_sell_3s', 'current_cvd_buy_3s', 'cvd_buy_30m',
    'cvd_sell_30m', 'p95_value', 'fp_last_imbalance', 'sweep_m1',
    'breakout_m1', 'absorption_event', 'absorption_reaction',
    'flow_divergence', 'value_area_sweep', 'wall_pull_flag', 'obi',
    'obi_top3', 'obi_top10',
    'persistent_flow', 'flow_price_trap', 'zone_reaction',
    'zone_acceptance_trap',
    'open_interest', 'funding_rate', 'macro_bias',
    'positioning_cvd_divergence', 'liquidation_recovery',
    'structure_transition', 'structure_broken_level', 'structure_break_streak',
)


def _emit_decision(state, setup, snapshot, result, score=None, veto_reason=None, **extra):
    """Persist every armed evaluation; maximum rate is bounded by Radar rescore."""
    if not hasattr(state, 'journal_events'):
        return
    if _watch_enabled():
        now_mono = float(getattr(snapshot, 'snapshot_mono', time.monotonic()))
        marker = setup.get('_last_modern_journal') or {}
        current_score = score or setup.get('last_score') or {}
        score_value = float(current_score.get(
            'final_score', current_score.get('score', 0.0)
        ))
        action_value = (score or {}).get(
            'action', (setup.get('last_score') or {}).get(
                'action', 'ENTRY' if current_score.get('activated') else 'WATCH'
            )
        )
        tier_value = (score or {}).get(
            'tier', (setup.get('last_score') or {}).get(
                'tier', current_score.get('display_tier')
            )
        )
        elapsed = now_mono - float(marker.get('ts', 0.0))
        urgent_result = result in (
            'CLAIMED', 'VETO', 'CONTINUOUS_SCORER_ERROR',
            'EXECUTION_QUEUE_FULL',
        ) and result != marker.get('result')
        decision_changed = bool(
            abs(score_value - float(marker.get('score', -999.0))) >= 1.0
            or action_value != marker.get('action')
            or tier_value != marker.get('tier')
            or result != marker.get('result')
            or veto_reason != marker.get('veto_reason')
        )
        minimum_interval = 1.0 if _minimal_mainnet_audit_enabled() else 0.0
        heartbeat_interval = (
            15.0 if _minimal_mainnet_audit_enabled() else 1.0
        )
        changed = bool(
            urgent_result
            or (decision_changed and elapsed >= minimum_interval)
            or elapsed >= heartbeat_interval
        )
        if not changed:
            return
        setup['_last_modern_journal'] = {
            'ts': now_mono, 'score': score_value,
            'action': action_value, 'tier': tier_value,
            'result': result, 'veto_reason': veto_reason,
        }
    context = {
        field: getattr(snapshot, field, None)
        for field in DECISION_CONTEXT_FIELDS
    }
    state.journal_events.append({
        'ts': float(getattr(snapshot, 'snapshot_time', time.time())),
        'event': 'DECISION_EVALUATED',
        'run_id': getattr(state, 'run_id', None),
        'position_cycle_id': None,
        'payload': {
            'setup_id': setup.get('setup_id'),
            'opportunity_id': setup.get('opportunity_id'),
            'generation': int(setup.get('generation', 0)),
            'mode': setup.get('mode'),
            'bias': setup.get('bias'),
            'kind': setup.get('kind'),
            'entry_style': setup.get('entry_style'),
            'result': result,
            'veto_reason': veto_reason,
            'snapshot_revision': int(getattr(snapshot, 'decision_revision', 0) or 0),
            'score': score,
            'code_version': getattr(state, 'code_version', None),
            'strategy_config_version': getattr(
                state, 'strategy_config_version', None
            ),
            'context': context,
            **extra,
        },
    })


def phan_tich_va_ra_lenh(
    state, mode_info, armed_mode, armed_bias, setup=None, decision_snapshot=None,
    continuous_score=None,
):
    """Tính từ snapshot; authority do SMC_SCORER_VERSION chọn tường minh."""
    now_mono = time.monotonic()
    if not getattr(state, 'system_ready', False) or not getattr(state, 'trading_enabled', False):
        return None
    if getattr(state, 'co_lenh_mo', False) or getattr(state, 'execution_in_flight', False):
        return None
    if now_mono - getattr(state, 'last_execution_release_mono', 0.0) < 2.0:
        return None
    if (
        'STANDBY' in mode_info.get('modes', [])
        and armed_mode != 'TRANSITION-BREAKOUT'
    ):
        return None

    setup = setup or _legacy_setup(state, armed_mode, armed_bias)
    watch_state = 'WATCH' if _watch_enabled() else 'ARMED_WINDOW'
    if setup.get('state') != watch_state:
        return None
    setup_id = setup['setup_id']
    semantic_key = setup.get('semantic_key', setup_id)
    generation = int(setup.get('generation', 0))
    cooldown_until = max(
        float(getattr(state, 'setup_cooldowns', {}).get(setup_id, 0.0)),
        float(getattr(state, 'setup_cooldowns', {}).get(semantic_key, 0.0)),
    )
    if now_mono < cooldown_until:
        return None

    snapshot = decision_snapshot or snapshot_mod.capture(state, setup)
    setup['evaluation_count'] = int(setup.get('evaluation_count', 0)) + 1
    is_vetoed, reason = kiem_duyet_veto.kiem_tra_veto(snapshot, armed_bias)
    if is_vetoed:
        # Preserve only confirmed Flash severity; wall/spread remain governed
        # by their own live contracts. This trace decays inside the scorer.
        kiem_duyet_veto.remember_confirmed_flash(
            state, snapshot, armed_bias,
            now=float(getattr(snapshot, 'snapshot_time', time.time()) or time.time()),
        )
        setup['last_veto'] = reason
        setup['veto_count'] = int(setup.get('veto_count', 0)) + 1
        _emit_decision(state, setup, snapshot, 'VETO', veto_reason=reason)
        return None

    # Chấm đúng mode đã ARM. mode_info có thể đồng thời chứa pullback và
    # breakout; truyền mode cụ thể ngăn reaction vùng bị dùng nhầm setup.
    scoring_mode = dict(mode_info or {})
    scoring_mode['mode'] = armed_mode
    legacy_score = cham_diem_mod.cham_diem(snapshot, scoring_mode, armed_bias)
    if _continuous_enabled():
        try:
            active_continuous_mod = _continuous_module()
            score = continuous_score
            if not isinstance(score, dict) or score.get('version') != active_continuous_mod.LIVE_VERSION:
                score = active_continuous_mod.score_continuous(
                    snapshot, setup, scoring_mode, live=True
                )
            if (
                score.get('setup_id') != setup_id
                or int(score.get('setup_generation', -1)) != generation
                or abs(float(score.get('snapshot_time', 0.0)) - float(snapshot.snapshot_time)) > 1e-6
            ):
                raise ValueError('continuous score does not match immutable decision snapshot')
        except Exception as exc:
            setup['continuous_scorer_error'] = str(exc)
            _emit_decision(
                state, setup, snapshot, 'CONTINUOUS_SCORER_ERROR',
                veto_reason='SCORER_EXCEPTION_OR_SCHEMA', error=str(exc),
            )
            logging.exception("❌ [COMMANDER] Continuous scorer fail-closed: %s", exc)
            return None
    else:
        score = legacy_score
    setup['last_score'] = score
    setup['last_score_mono'] = now_mono
    setup['score_count'] = int(setup.get('score_count', 0)) + 1
    if _continuous_enabled():
        previous_best = float(setup.get('max_continuous_score', -1.0))
        current_best = float(score.get('score', 0.0))
    else:
        previous_best = int(setup.get('max_core', 0))
        current_best = int(score['core'])
    if current_best >= previous_best:
        if _watch_enabled():
            setup['best_score'] = dict(score)
        else:
            setup['best_score'] = {
                'total': float(score['total']),
                'core': int(score['core']),
                'effective_core': float(score.get('effective_core', score['core'])),
                'm15_modifier': float(score.get('m15_modifier', 0.0)),
                'poc_modifier': float(score.get('poc_modifier', 0.0)),
                'shark': int(score['shark']),
                'detail': list(score.get('detail', [])),
                'event_ids': list(score.get('event_ids', [])),
                'advisory': dict(score.get('advisory', {})),
                'evidence_quality': dict(score.get('evidence_quality', {})),
            }
    seen_details = setup.setdefault('seen_score_details', [])
    for detail in (legacy_score if _watch_enabled() else score).get('detail', []):
        if detail not in seen_details and len(seen_details) < 24:
            seen_details.append(detail)
    setup['max_core'] = max(int(setup.get('max_core', 0)), int(legacy_score['core']))
    setup['max_shark'] = max(int(setup.get('max_shark', 0)), int(legacy_score['shark']))
    if _continuous_enabled():
        setup['max_continuous_score'] = max(
            float(setup.get('max_continuous_score', 0.0)),
            float(score.get('score', 0.0)),
        )
        score_ready, score_reason = _continuous_module().entry_ready(
            setup, score, now_mono
        )
        score_rejected = not score_ready
    else:
        score_rejected = bool(
            not _score_allows(armed_mode, score, snapshot)
            or _weak_gap_requires_reaction(setup, score)
        )
        score_reason = (
            'GAP_RECOVERY_WAIT_REACTION'
            if _weak_gap_requires_reaction(setup, score) else 'CORE_REJECT'
        )
    if score_rejected:
        setup['core_reject_count'] = int(setup.get('core_reject_count', 0)) + 1
        result = score_reason
        _emit_decision(
            state, setup, snapshot, result, score=score,
        )
        return None

    # Neutral structural breakout mới phải chứng minh expectancy qua shadow
    # trước khi được quyền claim. Ghi đúng một qualified event/setup để không
    # tạo chatter; mọi lane production cũ đi tiếp như trước.
    if setup.get('advisory_only'):
        if not setup.get('shadow_qualified_once'):
            setup['shadow_qualified_once'] = True
            _emit_decision(
                state, setup, snapshot, 'SHADOW_ONLY', score=score,
                shadow_reason='NEUTRAL_STRUCTURAL_REPLAY_NOT_CALIBRATED',
            )
        return None

    venue_size_feasibility = None
    effective_target_notional_pct = float(
        score.get('target_notional_pct', 0.0) or 0.0
    )
    retention_minimum_applied = False
    if _continuous_enabled():
        decision_price = (
            float(snapshot.best_ask) if armed_bias == 'LONG'
            else float(snapshot.best_bid)
        )
        balance_usdt = float(getattr(snapshot, 'balance_usdt', 0.0) or 0.0)
        if mainnet_safety.execution_venue() == 'MAINNET':
            # Mainnet is explicitly fixed at 0.001 BTC.  Let the Executor
            # evaluate stop-risk and fee edge at that exact lot; comparing a
            # scorer percentage with the venue lot here made every real signal
            # impossible on a small, leveraged account.
            size_filters = dict(getattr(snapshot, 'exchange_filters', {}) or {})
            venue_size_feasibility = mainnet_safety.fixed_quantity_feasibility(
                balance_usdt, decision_price, size_filters
            )
        else:
            (
                effective_target_notional_pct,
                size_filters,
                retention_minimum_applied,
            ) = _retention_allocation(score, setup, snapshot, decision_price)
            venue_size_feasibility = risk_mod.quantity_feasibility(
                balance_usdt,
                effective_target_notional_pct,
                decision_price,
                size_filters,
            )
        venue_size_feasibility.update({
            'scorer_target_notional_pct': float(
                score.get('target_notional_pct', 0.0) or 0.0
            ),
            'effective_target_notional_pct': effective_target_notional_pct,
            'retention_minimum_applied': retention_minimum_applied,
            'retention_cap_notional_pct': (
                RETENTION_MAX_NOTIONAL_PCT
                if _opportunity_retention_enabled() else None
            ),
            'fixed_qty_preflight': (
                mainnet_safety.execution_venue() == 'MAINNET'
            ),
        })
        if not venue_size_feasibility['executable']:
            setup['venue_min_wait_count'] = int(
                setup.get('venue_min_wait_count', 0)
            ) + 1
            setup['last_venue_size_feasibility'] = venue_size_feasibility
            _emit_decision(
                state, setup, snapshot, 'CONTINUOUS_VENUE_MIN_WAIT',
                score=score, venue_size_feasibility=venue_size_feasibility,
            )
            return None

    # Không có await giữa kiểm tra và claim: atomic trong một asyncio event loop.
    if getattr(state, 'execution_in_flight', False) or setup.get('state') != watch_state:
        return None
    # [TICK CONFIRMATION] Kiem tra gia dang nay dung huong truoc khi bop co
    # Tranh 'bat dao roi': score du nhung gia van dang cam dau vao zone
    if not _has_momentum_reclaim(snapshot, armed_bias, armed_mode):
        setup['reclaim_reject_count'] = int(setup.get('reclaim_reject_count', 0)) + 1
        _emit_decision(
            state, setup, snapshot, 'RECLAIM_WAIT', score=score,
        )
        return None

    setup['state'] = 'EXECUTING'
    state.execution_in_flight = True
    state.execution_setup_id = setup_id
    state.execution_generation = generation
    client_order_id = _client_order_id(
        setup_id, generation, state=state,
        opportunity_id=setup.get('opportunity_id') or semantic_key,
    )
    state.execution_client_order_id = client_order_id
    state.execution_unknown = False

    if _continuous_enabled():
        allocation_unit = (
            venue_size_feasibility.get('allocation_unit')
            if venue_size_feasibility else score['allocation_unit']
        )
        size_policy = {
            'size_pct': effective_target_notional_pct,
            'target_notional_pct': effective_target_notional_pct,
            'scorer_target_notional_pct': float(score['target_notional_pct']),
            'retention_minimum_applied': retention_minimum_applied,
            'allocation_unit': allocation_unit,
            'allocation_base_usdt': float(
                getattr(snapshot, 'balance_usdt', 0.0) or 0.0
            ),
            'tier': score['display_tier'],
            'base_pct': float(score['target_notional_pct']),
            'cap_pct': (
                RETENTION_MAX_NOTIONAL_PCT
                if retention_minimum_applied else 9.0
            ),
            'nerf_pct': 0,
            'policy_version': (
                'opportunity_retention_minimum_v1'
                if retention_minimum_applied
                else 'continuous_notional_equity_allocation_v2'
            ),
            'score_100': float(score['score']),
            'confidence': float(score['confidence']),
            'activation': float(score['activation']),
            'trade_power_100': float(score['trade_power']),
            'activation_floor_100': float(score['activation_floor']),
            'venue_size_feasibility': venue_size_feasibility,
            'fixed_qty_btc': (
                venue_size_feasibility.get('fixed_qty_btc')
                if venue_size_feasibility else None
            ),
            'edge_quality_stage': 'EXECUTOR_FEE_GATE_PENDING',
        }
    else:
        size_policy = _position_size_details(armed_mode, score)
    size_pct = size_policy['size_pct']
    signal = {
        'action': 'EXECUTE',
        'bias': armed_bias,
        'mode': armed_mode,
        'size_pct': size_pct,
        'size_policy': size_policy,
        'score_version': score.get('version', 'CORE_V1'),
        'score_100': score.get('final_score', score.get('score')),
        'continuous_score': score if _continuous_enabled() else None,
        'passive_intent_ttl_seconds': (
            score.get('passive_intent_ttl_seconds')
            if _continuous_enabled() else None
        ),
        'legacy_score': legacy_score if _watch_enabled() else score,
        'score_total': score.get(
            'final_score', score.get('score', score.get('total'))
        ),
        'score_core': legacy_score['core'],
        'score_effective_core': legacy_score.get('effective_core', legacy_score['core']),
        'score_m15_modifier': legacy_score.get('m15_modifier', 0.0),
        'score_poc_modifier': legacy_score.get('poc_modifier', 0.0),
        'score_shark': legacy_score['shark'],
        'score_detail': legacy_score['detail'],
        'event_ids': score.get(
            'source_event_ids',
            score.get('trigger_event_ids', legacy_score.get('event_ids', [])),
        ),
        'score_advisory': legacy_score.get('advisory', {}),
        'score_evidence_quality': legacy_score.get('evidence_quality', {}),
        'created_at': float(snapshot.snapshot_time),
        'created_mono': float(snapshot.snapshot_mono),
        'snapshot_revision': getattr(snapshot, 'decision_revision', 0),
        'setup_id': setup_id,
        'setup_generation': generation,
        # Frozen setup identity travels with the atomic decision. Executor uses
        # it only for pre-flight identity/price-drift guards; it must not infer
        # a different zone from mutable live mode state milliseconds later.
        'setup_zone': float(setup.get('zone', 0.0) or 0.0),
        'setup_zone_id': setup.get('zone_id'),
        'setup_kind': setup.get('kind'),
        'opportunity_id': setup.get('opportunity_id'),
        'opportunity_event_ids': list(setup.get('opportunity_event_ids', ())),
        'entry_style': (
            score.get('entry_style_policy')
            if _continuous_enabled() and score.get('entry_style_policy')
            else setup.get('entry_style')
        ),
        'passive_entry_price': setup.get('passive_entry_price'),
        'breakout_target': setup.get('breakout_target'),
        'breakout_target2': setup.get('breakout_target2'),
        'breakout_target_basis': setup.get('breakout_target_basis'),
        'minimum_raw_target_bps': setup.get('minimum_raw_target_bps'),
        'decision_poc': float(getattr(snapshot, 'poc', 0.0) or 0.0),
        'decision_vah': float(getattr(snapshot, 'vah', 0.0) or 0.0),
        'decision_val': float(getattr(snapshot, 'val', 0.0) or 0.0),
        'client_order_id': client_order_id,
        'run_id': getattr(state, 'run_id', None),
        'calibration_mode': str(
            os.getenv('SMC_SIDE_CALIBRATION_MODE', 'SHADOW')
        ).upper(),
        'calibration_version': getattr(state, 'side_calibration_version', None),
        'calibration_hash': getattr(state, 'side_calibration_hash', None),
        # Ba lớp giá được journal tách riêng; đây đều là mainnet decision snapshot.
        'signal_price': (
            float(snapshot.best_bid) + float(snapshot.best_ask)
        ) / 2.0,
        'decision_price': (
            float(snapshot.best_ask) if armed_bias == 'LONG' else float(snapshot.best_bid)
        ),
        'entry_reason': (
            'CONTINUOUS_TRADE_POWER_PASS' if _continuous_enabled()
            else 'CORE_SCORE_PASS'
        ),
    }
    try:
        state.hang_doi_tin_hieu.put_nowait(signal)
    except asyncio.QueueFull:
        setup['state'] = watch_state
        state.execution_in_flight = False
        state.execution_setup_id = None
        state.execution_client_order_id = None
        logging.error("❌ [COMMANDER] Hàng đợi tín hiệu đầy; trả setup về ARMED_WINDOW.")
        _emit_decision(state, setup, snapshot, 'EXECUTION_QUEUE_FULL', score=score)
        return None

    logging.info(
        "🔫 [COMMANDER] CLAIM %s %s | setup=%s | scorer=%s "
        "score=%s tier=%s size=%s%%",
        armed_mode, armed_bias, setup_id, signal['score_version'],
        signal['score_total'], size_policy['tier'], size_pct,
    )
    _emit_decision(
        state, setup, snapshot, 'CLAIMED', score=score,
        size_pct=size_pct, size_policy=size_policy,
        client_order_id=client_order_id,
    )
    return signal
