"""
[AI_CONTEXT]
- MODULE: khoi_dong (Nhạc Trưởng)
- ROLE: File chạy chính của toàn bộ dự án. Gọi và điều phối 4 Khối hoạt động nhịp nhàng.
- RULE: Không chứa logic toán học hay giao dịch, chỉ chứa lệnh "Gọi" và vòng lặp (Loop).
"""

import asyncio
import faulthandler
import json
import logging
import importlib.util
from pathlib import Path
import os
import signal
import tempfile
import time
from dotenv import load_dotenv
from recorder.metadata import code_version, strategy_config_version
from loi_he_thong.runtime_lock import DuplicateInstanceError, acquire_runtime_lock
from loi_he_thong import mainnet_safety, strategy_profile

try:
    import uvloop
except ImportError:
    uvloop = None

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

CURRENT_DIR = Path(__file__).parent
BOT_HEARTBEAT_PATH = Path(os.getenv(
    'SMC_BOT_HEARTBEAT_PATH', '/home/ubuntu/smc2026_data/health/bot_runtime.json'
))

def load_module(module_name, file_path):
    """Hàm hỗ trợ import file từ các thư mục bắt đầu bằng số (như 1_tai_du_lieu)"""
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

# --- IMPORT MODULES ---
tai_nen_offline = load_module("tai_nen_offline", CURRENT_DIR / "1_tai_du_lieu" / "tai_nen_offline" / "tai_nen_offline.py")
tai_so_lenh = load_module("tai_so_lenh", CURRENT_DIR / "1_tai_du_lieu" / "tai_so_lenh" / "tai_so_lenh.py")
tai_nen_live = load_module("tai_nen_live", CURRENT_DIR / "1_tai_du_lieu" / "tai_nen_live" / "tai_nen_live.py")
tai_gia_tick = load_module("tai_gia_tick", CURRENT_DIR / "1_tai_du_lieu" / "tai_gia_tick" / "tai_gia_tick.py")
tai_vi_mo = load_module("tai_vi_mo", CURRENT_DIR / "1_tai_du_lieu" / "tai_vi_mo" / "tai_vi_mo.py")
tai_coinbase = load_module("tai_coinbase", CURRENT_DIR / "1_tai_du_lieu" / "tai_coinbase" / "tai_coinbase.py")
tri_oracle = load_module("tri_oracle", CURRENT_DIR / "2_suy_luan_mapping" / "map_dong_tien" / "tri_oracle.py")
tai_dong_tien = load_module("tai_dong_tien", CURRENT_DIR / "1_tai_du_lieu" / "tai_dong_tien" / "tai_dong_tien.py")

# Khối 2
delta_cvd = load_module("delta_cvd", CURRENT_DIR / "2_suy_luan_mapping" / "map_dong_tien" / "delta_cvd.py")
flash_flow = load_module("flash_flow", CURRENT_DIR / "2_suy_luan_mapping" / "map_dong_tien" / "flash_flow.py")
footprint = load_module("footprint", CURRENT_DIR / "2_suy_luan_mapping" / "map_dong_tien" / "footprint.py")
map_so_lenh = load_module("map_so_lenh", CURRENT_DIR / "2_suy_luan_mapping" / "map_so_lenh.py")
map_nen_live = load_module("map_nen_live", CURRENT_DIR / "2_suy_luan_mapping" / "map_nen_live.py")
map_vi_mo = load_module("map_vi_mo", CURRENT_DIR / "2_suy_luan_mapping" / "map_vi_mo.py")
POC_VAH_VAL = load_module("POC_VAH_VAL", CURRENT_DIR / "2_suy_luan_mapping" / "map-nen-offline" / "POC_VAH_VAL.py")
ATR = load_module("ATR", CURRENT_DIR / "2_suy_luan_mapping" / "map-nen-offline" / "ATR.py")
BOS_CHoCH = load_module("BOS_CHoCH", CURRENT_DIR / "2_suy_luan_mapping" / "map-nen-offline" / "BOS_CHoCH.py")
bo_nho_ram = load_module("bo_nho_ram", CURRENT_DIR / "loi_he_thong" / "bo_nho_ram.py")

# Khối 2.5 (Trung Tâm Chỉ Huy)
chon_che_do = load_module("chon_che_do", CURRENT_DIR / "2_suy_luan_mapping" / "tong_ket_chi_huy" / "chon_che_do.py")
chi_huy_truong = load_module("chi_huy_truong", CURRENT_DIR / "2_suy_luan_mapping" / "tong_ket_chi_huy" / "chi_huy_truong.py")
map_gia_tick = load_module("map_gia_tick", CURRENT_DIR / "2_suy_luan_mapping" / "map_gia_tick.py")

# Khối 3 (Thực Thi)
binance_api = load_module("binance_api", CURRENT_DIR / "3_thuc_thi" / "binance_api.py")
dat_lenh = load_module("dat_lenh", CURRENT_DIR / "3_thuc_thi" / "dat_lenh.py")
bao_ve_khan_cap = load_module("bao_ve_khan_cap", CURRENT_DIR / "3_thuc_thi" / "ve_si_lenh" / "bao_ve_khan_cap.py")
tho_san_trailing = load_module("tho_san_trailing", CURRENT_DIR / "3_thuc_thi" / "ve_si_lenh" / "tho_san_trailing.py")
dong_bo_trang_thai = load_module("dong_bo_trang_thai", CURRENT_DIR / "3_thuc_thi" / "quan_ly_vi_the" / "dong_bo_trang_thai.py")
nhat_ky_giao_dich = load_module("nhat_ky_giao_dich", CURRENT_DIR / "3_thuc_thi" / "quan_ly_vi_the" / "nhat_ky_giao_dich.py")
giam_sat_he_thong = load_module("giam_sat_he_thong", CURRENT_DIR / "3_thuc_thi" / "giam_sat_he_thong.py")

state = bo_nho_ram.state
requested_execution = os.getenv('SMC_ENABLE_TRADING', 'false').lower() in (
    '1', 'true', 'yes', 'on'
)
state.execution_allowed = bool(
    requested_execution
    and (
        mainnet_safety.execution_venue() != 'MAINNET'
        or mainnet_safety.mainnet_armed()
    )
)
state.code_version = code_version(CURRENT_DIR)
state.strategy_config_version = strategy_config_version()
state.strategy_profile = strategy_profile.current_profile()

api = binance_api.BinanceAPI(
    api_key=mainnet_safety.credential('binance_api_key', 'BINANCE_API_KEY'),
    secret_key=mainnet_safety.credential('binance_api_secret', 'BINANCE_API_SECRET'),
    testnet=mainnet_safety.execution_venue() != 'MAINNET',
)
state.execution_venue = (
    'BINANCE_FUTURES_TESTNET' if api.testnet else 'BINANCE_FUTURES_MAINNET'
)


async def supervise(name, factory):
    """Restart task khi code/network ném exception; không để gather kéo sập toàn bot."""
    while True:
        try:
            await factory()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state.system_ready = False
            state.trading_enabled = False
            logging.exception("❌ [SUPERVISOR] %s chết: %s. Restart sau 2s.", name, exc)
            await asyncio.sleep(2)


def _write_bot_heartbeat(payload):
    BOT_HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix='bot_runtime_', suffix='.tmp', dir=BOT_HEARTBEAT_PATH.parent
    )
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump(
                payload, handle, ensure_ascii=False, separators=(',', ':'),
                default=str,
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, BOT_HEARTBEAT_PATH)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


async def vong_lap_runtime_heartbeat():
    """Low-frequency operational heartbeat, isolated from the trading hot path."""
    while True:
        now = time.time()
        payload = {
            'schema_version': 1,
            'updated_at_ms': int(now * 1000),
            'pid': os.getpid(),
            'run_id': getattr(state, 'run_id', None),
            'code_version': getattr(state, 'code_version', None),
            'strategy_config_version': getattr(
                state, 'strategy_config_version', None
            ),
            'strategy_profile': getattr(state, 'strategy_profile', None),
            'scorer_version': os.getenv('SMC_SCORER_VERSION', 'CONTINUOUS_V2'),
            'entry_lifecycle': os.getenv('SMC_ENTRY_LIFECYCLE', 'LEGACY'),
            'execution_venue': getattr(state, 'execution_venue', None),
            'system_ready': bool(getattr(state, 'system_ready', False)),
            'trading_enabled': bool(getattr(state, 'trading_enabled', False)),
            'readiness_reason': getattr(state, 'last_readiness_reason', None),
            'mode': getattr(state, 'current_mode', None),
            'position_status': getattr(state, 'position_status', None),
            'decision_revision': int(getattr(state, 'decision_revision', 0) or 0),
            'mainnet_safety': {
                'loss_streak': int(
                    getattr(state, 'mainnet_loss_streak', 0) or 0
                ),
                'cooldown_until': float(
                    getattr(state, 'mainnet_cooldown_until', 0.0) or 0.0
                ),
                'daily': dict(
                    getattr(state, 'mainnet_daily_safety', {}) or {}
                ),
                'max_planned_loss_usdt': (
                    mainnet_safety.max_planned_loss_usdt()
                ),
            },
        }
        await asyncio.to_thread(_write_bot_heartbeat, payload)
        await asyncio.sleep(5.0)


def parse_btc_filters(exchange_info):
    symbol = next(
        (item for item in exchange_info.get('symbols', []) if item.get('symbol') == 'BTCUSDT'),
        None,
    )
    if symbol is None:
        return {}
    filters = {item['filterType']: item for item in symbol.get('filters', [])}
    lot = filters.get('MARKET_LOT_SIZE') or filters.get('LOT_SIZE', {})
    price = filters.get('PRICE_FILTER', {})
    notional = filters.get('MIN_NOTIONAL', {})
    return {
        'step_size': float(lot.get('stepSize', 0.001)),
        'min_qty': float(lot.get('minQty', 0.001)),
        'max_qty': float(lot.get('maxQty', 0.0)),
        'tick_size': float(price.get('tickSize', 0.1)),
        'min_notional': float(notional.get('notional', 0.0)),
    }


async def khoi_tao_tai_khoan():
    state.account_ready = False
    state.balance_usdt = await api.get_balance()
    hedge_mode = await api.get_position_mode()
    info, status = await api.get_exchange_info()
    if hedge_mode is not None:
        state.account_hedge_mode = hedge_mode
    if status == 200 and isinstance(info, dict):
        state.exchange_filters = parse_btc_filters(info)
    config_ready, config_reason = mainnet_safety.validate_static_config(
        state.exchange_filters
    )
    mainnet_ready = True
    mainnet_reason = 'TESTNET'
    if not api.testnet:
        mainnet_ready, mainnet_reason = await mainnet_safety.prepare_mainnet_account(
            api, state
        )
        hedge_mode = await api.get_position_mode()
        if hedge_mode is not None:
            state.account_hedge_mode = hedge_mode
    required_filters = ('step_size', 'min_qty', 'tick_size', 'min_notional')
    filters_ready = all(
        float(state.exchange_filters.get(name, 0.0) or 0.0) > 0
        for name in required_filters
    )
    state.account_ready = (
        state.balance_usdt > 0
        and hedge_mode is not None
        and status == 200
        and filters_ready
        and config_ready
        and mainnet_ready
        and (api.testnet or hedge_mode is True)
    )
    logging.info(
        "⚙️ [ACCOUNT] Balance=%.4f | Mode=%s | Filters=%s",
        state.balance_usdt,
        'HEDGE' if state.account_hedge_mode else 'ONE-WAY',
        state.exchange_filters,
    )
    if not state.account_ready:
        state.last_readiness_reason = (
            config_reason if not config_ready else mainnet_reason
        )
        logging.critical(
            '⛔ [ACCOUNT] Khởi tạo tài khoản chưa đầy đủ (%s); entry bị khóa fail-closed.',
            state.last_readiness_reason,
        )


def seconds_to_next_boundary(interval_seconds, settle_seconds=1.0):
    """Căn REST refresh sau biên đóng nến, không trôi theo giờ startup."""
    now = time.time()
    return interval_seconds - (now % interval_seconds) + settle_seconds


def apply_market_structure(structure):
    """Chỉ tăng version khi cấu trúc M15 thực sự đổi."""
    previous = (
        state.trend_m15,
        round(float(state.swing_high_m15 or 0.0), 8),
        round(float(state.swing_low_m15 or 0.0), 8),
        getattr(state, 'structure_transition', 'NONE'),
        round(float(getattr(state, 'structure_broken_level', 0.0) or 0.0), 8),
        int(getattr(state, 'structure_break_streak', 0) or 0),
    )
    current = (
        structure['trend'],
        round(float(structure['swing_high'] or 0.0), 8),
        round(float(structure['swing_low'] or 0.0), 8),
        structure.get('transition', 'NONE'),
        round(float(structure.get('broken_level', 0.0) or 0.0), 8),
        int(structure.get('break_streak', 0) or 0),
    )
    (
        state.trend_m15,
        state.swing_high_m15,
        state.swing_low_m15,
        state.structure_transition,
        state.structure_broken_level,
        state.structure_break_streak,
    ) = current
    state.structure_last_close_time = int(structure.get('last_close_time', 0) or 0)
    continuous = dict(structure.get('continuous', {}) or {})
    continuous_event = {
        'active': bool(
            float(continuous.get('strength', continuous.get('trend_strength', 0.0)) or 0.0) > 0.0
            and float(continuous.get('direction', 0.0) or 0.0) != 0.0
        ),
        'direction': float(continuous.get('direction', 0.0) or 0.0),
        'strength': float(continuous.get('trend_strength', 0.0) or 0.0),
        'trend_strength': float(continuous.get('trend_strength', 0.0) or 0.0),
        'break_strength': float(continuous.get('break_strength', 0.0) or 0.0),
        'quality': float(continuous.get('quality', 0.0) or 0.0),
        'ts': float(state.structure_last_close_time) / 1000.0,
        'ttl': 3600.0,
        'source_event_id': continuous.get('source_event_id'),
        'source_family': 'M15_CLOSED',
        'trend': structure['trend'],
        'transition': structure.get('transition', 'NONE'),
    }
    previous_continuous = getattr(state, 'continuous_m15', {}) or {}
    continuous_changed = any(
        previous_continuous.get(key) != continuous_event.get(key)
        for key in (
            'direction', 'strength', 'break_strength', 'quality',
            'source_event_id', 'trend', 'transition',
        )
    )
    state.continuous_m15 = continuous_event
    if continuous_changed:
        state.continuous_evidence_revision = int(
            getattr(state, 'continuous_evidence_revision', 0)
        ) + 1
    structure_changed = current != previous
    if structure_changed:
        state.structure_version += 1
        state.decision_revision += 1
        if hasattr(state, 'journal_events'):
            state.journal_events.append({
                'ts': time.time(),
                'event': 'M15_STRUCTURE_CHANGED',
                'position_cycle_id': None,
                'payload': {
                    'previous': list(previous),
                    'current': list(current),
                    'last_close': float(structure.get('last_close', 0.0) or 0.0),
                    'break_buffer': float(
                        structure.get('break_buffer', 0.0) or 0.0
                    ),
                    'broken_level': float(state.structure_broken_level),
                    'break_streak': int(state.structure_break_streak),
                    'last_close_time': int(state.structure_last_close_time),
                    'structure_version': int(state.structure_version),
                },
            })
    if state.structure_transition != 'NONE':
        logging.warning(
            "⚠️ [M15 STRUCTURE] %s close=%.2f level=%.2f streak=%d buffer=%.2f",
            state.structure_transition,
            float(structure.get('last_close', 0.0) or 0.0),
            float(state.structure_broken_level),
            int(state.structure_break_streak),
            float(structure.get('break_buffer', 0.0) or 0.0),
        )

def doc_json(filepath):
    """Đọc trạm trung gian (ROM)"""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logging.error(f"Lỗi đọc {filepath.name}: {e}")
        return []

async def vong_lap_dong_tien():
    """Vòng lặp chạy cực nhanh liên tục quét Deque dòng tiền từ Khối 1"""
    # [VÁ LỖI] Đợi đến khi có luồng giá Tick (best_bid) đầu tiên để tránh lỗi chia 0 trong flash_flow
    while getattr(state, 'best_bid', 0.0) == 0.0:
        await asyncio.sleep(0.1)
        
    while True:
        if state.danh_sach_khop_lenh:
            lenh_khop = state.danh_sach_khop_lenh.popleft()
            delta_cvd.cap_nhat_cvd(lenh_khop, state)
            tri_oracle.cap_nhat_tri_oracle(state)
            flash_flow.cap_nhat_nguong_ca_map(lenh_khop, state)
            footprint.cap_nhat_footprint(lenh_khop, state)
        else:
            delta_cvd.kiem_tra_idle(state)
            await asyncio.sleep(0.001)

async def vong_lap_so_lenh():
    """Vòng lặp tiêu thụ Sổ lệnh và truyền cho Khối 2"""
    while True:
        if state.hang_doi_so_lenh:
            snapshot = state.hang_doi_so_lenh.popleft()
            map_so_lenh.cap_nhat_so_lenh(snapshot, state)
        else:
            await asyncio.sleep(0.01)

async def vong_lap_nen_live():
    """Vòng lặp tiêu thụ Nến live và truyền cho Khối 2"""
    while True:
        if state.hang_doi_nen_live:
            data = state.hang_doi_nen_live.popleft()
            nen = data['nen']
            khung = data['khung_thoi_gian']
            if khung == '1m':
                map_nen_live.cap_nhat_nen_m1(nen, state)
            elif khung == '15m':
                # [VÁ LỖI] Event-driven: Chỉ kích hoạt khi nến 15m báo đóng
                if nen.get('x') == True:
                    open_time = nen.get('t', 0)
                    if open_time != getattr(state, 'last_processed_m15_live', 0):
                        state.last_processed_m15_live = open_time
                        logging.info("⚡ [M15 LIVE] Nến đóng! Gọi Khối 1 kéo REST cập nhật xu hướng tức thời...")
                        
                        async def update_trend_task():
                            klines_m15 = await tai_nen_offline.cap_nhat_nen("15m", 200, 2, is_update=True)
                            if klines_m15:
                                # Merge data
                                existing = getattr(state, 'klines_m15', [])
                                if not existing:
                                    state.klines_m15 = klines_m15
                                else:
                                    existing_map = {k[0]: k for k in existing}
                                    for k in klines_m15:
                                        existing_map[k[0]] = k
                                    state.klines_m15 = sorted(list(existing_map.values()), key=lambda x: x[0])[-200:]
                                
                                structure = BOS_CHoCH.get_macro_structure(
                                    state.klines_m15,
                                    break_buffer=float(state.atr_1m or 0.0),
                                )
                                apply_market_structure(structure)
                                
                                state.current_mode = chon_che_do.xac_dinh_che_do(state)
                                
                                logging.info(f"✅ [M15 LIVE] Cập nhật Trend nhanh: {state.trend_m15}. Mode: {state.current_mode.get('modes', [])}")
                        
                        # Await để exception được supervisor quan sát, không tạo task mồ côi.
                        await update_trend_task()
        else:
            await asyncio.sleep(0.01)

async def vong_lap_nen_m1():
    """Vòng lặp M1: Cứ 60s -> Lấy data -> Tính ATR & POC -> Lưu RAM"""
    is_first = True
    while True:
        logging.info("[M1 LOOP] Đang gọi Khối 1 kéo nến...")
        klines_m1 = await tai_nen_offline.cap_nhat_nen(
            "1m", 500 if is_first else 15, 15, is_update=(not is_first)
        )
        
        if klines_m1:
            existing = getattr(state, 'klines_m1', [])
            if is_first or not existing:
                state.klines_m1 = klines_m1
            else:
                existing_map = {k[0]: k for k in existing}
                for k in klines_m1:
                    existing_map[k[0]] = k
                state.klines_m1 = sorted(list(existing_map.values()), key=lambda x: x[0])[-500:]
                
            sh_m15 = state.swing_high_m15 if state.swing_high_m15 > 0 else float('inf')
            sl_m15 = state.swing_low_m15 if state.swing_low_m15 > 0 else 0.0
            
            atr_result = ATR.tinh_atr_1m(state.klines_m1)
            filtered_m1 = POC_VAH_VAL.select_profile_klines(
                state.klines_m1, sl_m15, sh_m15, atr_result,
            )
            vp_result = POC_VAH_VAL.calculate_volume_profile(filtered_m1)
            
            state.poc = vp_result['poc']
            state.vah = vp_result['vah']
            state.val = vp_result['val']
            state.lvn_zones = vp_result.get('lvn_zones', [])
            previous_poc = float(getattr(state, 'previous_profile_poc', 0.0) or 0.0)
            state.volume_profile_updated_at = time.time()
            state.volume_profile_coverage = min(
                1.0, len(filtered_m1) / 100.0
            )
            state.poc_movement_atr = (
                abs(float(state.poc) - previous_poc) / max(float(atr_result), 1e-9)
                if previous_poc > 0.0 and atr_result > 0.0 else 0.0
            )
            state.previous_profile_poc = float(state.poc)
            state.atr_1m = atr_result
            if state.ema9_m1 == 0.0 and state.klines_m1:
                closes = [float(k[4]) for k in state.klines_m1[-50:]]
                ema = closes[0]
                for close in closes[1:]:
                    ema = (close - ema) * 0.2 + ema
                state.ema9_m1 = ema

            # POC/ATR thay đổi phải tái chọn mode ngay, tránh STANDBY giả lúc startup.
            state.current_mode = chon_che_do.xac_dinh_che_do(state)
            state.decision_revision += 1
            if hasattr(state, 'journal_events'):
                state.journal_events.append({
                    'ts': time.time(),
                    'event': 'DECISION_CONTEXT_M1',
                    'position_cycle_id': None,
                    'payload': {
                        'poc': float(state.poc),
                        'vah': float(state.vah),
                        'val': float(state.val),
                        'atr_1m': float(state.atr_1m),
                        'mode': dict(state.current_mode),
                        'trend_m15': state.trend_m15,
                        'swing_high_m15': float(state.swing_high_m15),
                        'swing_low_m15': float(state.swing_low_m15),
                        'structure_transition': getattr(
                            state, 'structure_transition', 'NONE'
                        ),
                        'structure_broken_level': float(
                            getattr(state, 'structure_broken_level', 0.0) or 0.0
                        ),
                        'structure_break_streak': int(
                            getattr(state, 'structure_break_streak', 0) or 0
                        ),
                        'structure_version': int(state.structure_version),
                        'decision_revision': int(state.decision_revision),
                    },
                })
            logging.info(
                "✅ [M1 LOOP] Xong việc! POC=%.2f, ATR=%.2f, Mode=%s, "
                "Transition=%s. Đi ngủ 60s...",
                state.poc, state.atr_1m,
                state.current_mode.get('modes', []),
                getattr(state, 'structure_transition', 'NONE'),
            )

        is_first = False
        await asyncio.sleep(seconds_to_next_boundary(60, 1.0))

async def vong_lap_nen_m15():
    """Vòng lặp M15: Cứ 15 phút (900s) -> Lấy data -> Quét xu hướng -> Lưu RAM"""
    is_first = True
    while True:
        logging.info("[M15 LOOP] Đang gọi Khối 1 kéo nến M15...")
        klines_m15 = await tai_nen_offline.cap_nhat_nen(
            "15m", 200 if is_first else 2, 2, is_update=(not is_first)
        )
        
        if klines_m15:
            existing = getattr(state, 'klines_m15', [])
            if is_first or not existing:
                state.klines_m15 = klines_m15
            else:
                existing_map = {k[0]: k for k in existing}
                for k in klines_m15:
                    existing_map[k[0]] = k
                state.klines_m15 = sorted(list(existing_map.values()), key=lambda x: x[0])[-200:]
                
            structure = BOS_CHoCH.get_macro_structure(
                state.klines_m15,
                break_buffer=float(state.atr_1m or 0.0),
            )
            apply_market_structure(structure)
            
            # Tính lại chế độ ngay khi nến M15 đóng và lưu vào RAM (Event-driven)
            state.current_mode = chon_che_do.xac_dinh_che_do(state)
            
            logging.info(f"✅ [M15 LOOP] Xong việc! Trend={state.trend_m15}, SH={state.swing_high_m15}, SL={state.swing_low_m15}. Mode: {state.current_mode.get('modes', [])}")

        is_first = False
        await asyncio.sleep(seconds_to_next_boundary(900, 2.0))


async def vong_lap_vi_mo_mapping():
    while True:
        map_vi_mo.cap_nhat_vi_mo(state)
        await asyncio.sleep(5)



async def test_mock_data():
    """Gài dữ liệu giả mạo (Mock Data) để test bắn lệnh của Chỉ Huy Trưởng"""
    await asyncio.sleep(8) # Chờ luồng khởi động
    logging.info("💉 [MOCK DATA] Bắt đầu gài dữ liệu giả mạo để test Radar & Trung Tâm Chỉ Huy...")
    
    while True:
        # Ép bối cảnh Trend Pullback
        state.trend_m15 = 'BULLISH'
        state.poc = 50000.0 # Pullback zone = 50000 ± 50
        state.val = 49950.0
        state.atr_1m = 100.0
        
        # Mô phỏng giá rớt mạnh vào zone (Cắt từ trên xuống)
        state.prev_best_ask = 50100.0
        state.best_ask = 50000.0 # Nằm ngay POC
        state.best_bid = 49999.0
        
        # Xóa rủi ro Veto
        state.p95_value = 1000.0
        state.wall_pull_flag = {'active': False, 'side': None, 'ts': 0.0}
        
        # Bơm điểm CORE (CVD Tích lũy và CVD Tức thời mạnh)
        state.cvd_buy = 500.0 # > p95*0.3 (300) -> Thêm 1 điểm CORE
        state.cvd_sell = 10.0
        state.prev_cvd_buy = 0.0 # Đảm bảo cvd_buy_recent > 300
        
        # Bơm điểm SHARK (Absorption + OBI)
        state.absorption_flag = True
        state.obi = 0.5
        
        await asyncio.sleep(0.5)

async def main():
    state.hang_doi_tin_hieu = asyncio.Queue(maxsize=5)
    logging.info("🚀 BẮT ĐẦU CHẠY BỘ ĐIỀU PHỐI (ORCHESTRATOR)")
    giam_sat_he_thong.start_out_of_band_watchdog(state)
    
    await khoi_tao_tai_khoan()
    
    # Task Khối 1: Hứng luồng mạng (Độc lập tuyệt đối, có cơ chế tự backoff)
    tasks_mang = [
        # --- SPOT MAINNET --- (Chiến lược / Structure / Signal)
        asyncio.create_task(supervise('bookTicker', lambda: tai_gia_tick.hung_gia_tick_futures("btcusdt", state))),
        asyncio.create_task(supervise('depth20', lambda: tai_so_lenh.hung_so_lenh_futures("btcusdt", state))),
        asyncio.create_task(supervise('kline', lambda: tai_nen_live.hung_nen_live_futures("btcusdt", state))),
        asyncio.create_task(supervise('aggTrade_spot', lambda: tai_dong_tien.hung_dong_tien_spot("btcusdt", state))),
        asyncio.create_task(supervise('coinbase_spot', lambda: tai_coinbase.hung_coinbase_spot("BTC-USD", state))),
        
        # --- FUTURES MAINNET --- (Volume / CVD / Dòng tiền)
        asyncio.create_task(supervise('aggTrade_futures', lambda: tai_dong_tien.hung_dong_tien_futures_real("btcusdt", state))),
        
        # --- FUTURES MAINNET EXECUTION --- (Shadow Trading Data Layer)
        asyncio.create_task(supervise('executionBookTicker', lambda: tai_gia_tick.hung_gia_tick_execution("btcusdt", state))),
        asyncio.create_task(supervise('executionDepth20', lambda: tai_so_lenh.hung_so_lenh_futures_execution("btcusdt", state))),
    ]
    
    # Task Khối 2: Chạy nội bộ tiêu thụ Queue
    tasks_noi_bo = [
        asyncio.create_task(supervise('dong_tien_mapping', vong_lap_dong_tien)),
        asyncio.create_task(supervise('vi_mo_input', lambda: tai_vi_mo.tai_du_lieu_vi_mo("BTCUSDT", state))),
        asyncio.create_task(supervise('vi_mo_mapping', vong_lap_vi_mo_mapping)),
        asyncio.create_task(supervise('orderbook_mapping', vong_lap_so_lenh)),
        asyncio.create_task(supervise('kline_mapping', vong_lap_nen_live)),
        asyncio.create_task(supervise('M1_offline', vong_lap_nen_m1)),
        asyncio.create_task(supervise('M15_offline', vong_lap_nen_m15)),
        asyncio.create_task(supervise('radar', lambda: map_gia_tick.vong_lap_radar(state)))
        # asyncio.create_task(test_mock_data())
    ]
    
    # Task Khối 3: Thực thi và bảo vệ
    import os
    mode = os.getenv('SMC_EXECUTION_MODE')
    if mode == 'SHADOW_MAINNET':
        from 3_thuc_thi import dat_lenh_shadow
        tasks_thuc_thi = [
            asyncio.create_task(supervise('shadow_executor', lambda: dat_lenh_shadow.vong_lap_shadow_thuc_thi(state, api))),
            asyncio.create_task(supervise('shadow_guardian', lambda: dat_lenh_shadow.vong_lap_shadow_guardian(state, api))),
            asyncio.create_task(supervise('watchdog', lambda: giam_sat_he_thong.vong_lap_giam_sat(state))),
            asyncio.create_task(supervise('runtime_heartbeat', vong_lap_runtime_heartbeat)),
        ]
    else:
        tasks_thuc_thi = [
            asyncio.create_task(supervise('executor', lambda: dat_lenh.vong_lap_thuc_thi(state, api))),
            asyncio.create_task(supervise('guardian', lambda: bao_ve_khan_cap.vong_lap_bao_ve(state, api))),
            asyncio.create_task(supervise('trailing', lambda: tho_san_trailing.vong_lap_trailing(state, api))),
            asyncio.create_task(supervise('rom_backup', lambda: dong_bo_trang_thai.vong_lap_dong_bo(state))),
            asyncio.create_task(supervise('reconcile', lambda: dong_bo_trang_thai.vong_lap_doi_chieu(state, api))),
            asyncio.create_task(supervise('trade_journal', lambda: nhat_ky_giao_dich.vong_lap_nhat_ky(state, api))),
            asyncio.create_task(supervise('watchdog', lambda: giam_sat_he_thong.vong_lap_giam_sat(state))),
            asyncio.create_task(supervise('runtime_heartbeat', vong_lap_runtime_heartbeat)),
        ]
        if os.getenv('SMC_MINIMAL_MAINNET_AUDIT', 'false').lower() not in (
            '1', 'true', 'yes', 'on'
        ):
            tasks_thuc_thi.append(asyncio.create_task(supervise(
                'shadow', lambda: nhat_ky_giao_dich.vong_lap_shadow(state)
            )))
    
    await asyncio.gather(*(tasks_mang + tasks_noi_bo + tasks_thuc_thi))

if __name__ == "__main__":
    try:
        runtime_lock = acquire_runtime_lock('bot')
    except DuplicateInstanceError as exc:
        logging.critical('⛔ [RUNTIME] %s', exc)
        raise SystemExit(73) from exc
    try:
        faulthandler.register(signal.SIGUSR1, all_threads=True)
        if uvloop is not None:
            uvloop.install()
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("🛑 Bot đã dừng bởi người dùng.")
    finally:
        runtime_lock.close()
