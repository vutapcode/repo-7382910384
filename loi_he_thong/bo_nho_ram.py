"""
[AI_CONTEXT]
- MODULE: loi_he_thong
- ROLE: Không gian lưu trữ siêu tốc (State). Nơi MỌI biến số tập trung.
- I/O: IN/OUT: Shared Memory
- RULE: CHỈ tuân thủ ranh giới của khối, không cắm chéo.
"""

from collections import deque
import uuid

class ViThe:
    def __init__(self):
        self.active = False
        self.symbol = "BTCUSDT"
        self.side = ""
        self.qty = 0.0
        # entry_price/soft levels luôn thuộc venue chiến lược Mainnet.
        self.entry_price = 0.0
        self.strategy_entry_price = 0.0
        # Fill thực tế thuộc venue đặt lệnh Testnet.
        self.execution_entry_price = 0.0
        # hard_sl là trigger thực gửi lên Testnet; strategy_hard_sl giữ mốc
        # Mainnet tương ứng để audit, không được POST trực tiếp.
        self.hard_sl = 0.0
        self.strategy_hard_sl = 0.0
        self.soft_sl = 0.0
        self.soft_tp1 = 0.0
        self.soft_tp2 = 0.0
        self.tp1_allocation = 0.50
        self.tp1_checkpoint_monetizable = False
        self.tp1_checkpoint_lock_net_bps = 0.0
        self.runner_policy = "LEGACY_TP2"
        self.sl_order_id = None
        self.tp_order_id = None
        self.hard_sl_algo_id = None
        self.hard_sl_client_algo_id = None
        # Với setup vượt VAH/VAL và POC hút về value: SL1 phần mềm cắt tối đa
        # 90%, Hard SL exchange trở thành SL2 xa cho tail 10%.
        self.split_sl_enabled = False
        self.split_sl1_done = False
        self.split_sl1_fraction = 0.90
        self.split_sl1 = 0.0
        self.split_sl2 = 0.0
        self.standard_hard_sl = 0.0
        self.opened_at = 0.0
        self.initial_qty = 0.0
        # Bảo vệ sớm (shark/time-stop) chỉ được cắt cộng dồn 50% entry gốc.
        # Phần còn lại dành cho SL hoặc exit chiến lược bình thường.
        self.protection_closed_qty = 0.0
        self.protection_reasons_done = []
        self.tp1_done = False
        self.trailing_active = False
        self.add_on_done = False
        self.add_on_attempted = False
        self.mode = ""
        self.setup_id = ""
        self.setup_semantic_key = ""
        self.opportunity_id = ""
        self.setup_zone = 0.0
        # Chênh lệch entry execution - strategy, chỉ dùng audit/đối chiếu.
        # Guardian tuyệt đối không dùng offset này để dịch tín hiệu Mainnet.
        self.venue_price_offset = 0.0
        self.setup_generation = 0
        self.position_cycle_id = ""
        self.entry_order_id = None
        self.entry_client_order_id = None
        self.tp2_extended = False
        self.shark_adverse_since = 0.0
        self.shark_support_since = 0.0
        self.guardian_policy = {}
        self.strategy_profile = ""
        self.entry_continuous_score = {}
        self.dynamic_exit_plan = {}
        self.breakout_target = 0.0
        self.breakout_target2 = 0.0
        self.aug13_guardian_last_assessment_at = 0.0
        self.aug13_guardian_last_error_log_at = 0.0
        self.aug13_exit_candidate_since = 0.0

class SharedState:
    def __init__(self):
        # Boot identity is never restored from disk. It prevents an order from
        # borrowing evidence/fills from a previous process after restart.
        self.run_id = uuid.uuid4().hex
        # Heartbeat được thread watchdog độc lập quan sát; nếu event loop bị
        # starvation thì entry phải fail-closed dù chính asyncio không chạy.
        self.event_loop_heartbeat_mono = 0.0
        self.journal_loop_heartbeat_mono = 0.0
        self.journal_last_persist_mono = 0.0
        self.event_loop_lag_seconds = 0.0
        self.event_loop_stalled = False
        self.process_cpu_ratio = 0.0
        self.cpu_runaway = False
        self.cpu_runaway_seconds = 0.0
        # Whole-host CPU budget.  The governor normalizes these values like
        # Lightsail CPUUtilization (100% == all allocated vCPUs busy).
        self.host_cpu_15m_pct = 0.0
        self.host_cpu_1h_pct = 0.0
        self.cpu_budget_15m_remaining = 0.0
        self.cpu_budget_1h_remaining = 0.0
        self.governor_mode = 'WARMUP'
        self.host_cpu_entry_allowed = False
        self.live_entry_cpu_allowed = False
        self.shadow_entry_cpu_allowed = True
        self.shadow_cpu_scheduler_mode = 'WARMUP'
        self.shadow_entry_eval_interval_seconds = 0.0
        self.shadow_entry_idle_skips = 0
        self.host_cpu_hard_limit_respected = True
        self.host_cpu_top_processes = []
        self.production_workload_blockers = []
        self.lightsail_cpu_last_seen = None
        self.lightsail_metric_age_seconds = None
        self.lightsail_metric_fresh = False
        self.host_cpu_snapshot = {}
        self.journal_stalled = False
        self.journal_age_seconds = 0.0
        self.out_of_band_watchdog_started = False
        # --- KHỐI 1: TÀI NGUYÊN (RAW DATA) ---
        self.klines_m15 = []
        self.klines_m1 = []
        
        # bookTicker (Từ tai_gia_tick.py)
        self.best_bid = 0.0
        self.best_ask = 0.0
        self.best_bid_qty = 0.0
        self.best_ask_qty = 0.0
        self.thoi_gian_tick_cuoi = 0.0
        # Giá Testnet chỉ dùng quan sát chất lượng khớp/khả năng execution.
        # Guardian/Trailing ra quyết định bằng best_bid/best_ask Mainnet ở trên.
        self.execution_best_bid = 0.0
        self.execution_best_bid_qty = 0.0
        self.execution_bids = []
        self.execution_asks = []
        self.execution_bids_top_10 = []
        self.execution_asks_top_10 = []
        self.execution_best_ask = 0.0
        self.execution_best_ask_qty = 0.0
        self.execution_price_time = 0.0
        self.execution_venue = 'UNKNOWN'
        
        # aggTrade (Từ tai_dong_tien.py)
        self.danh_sach_khop_lenh = deque(maxlen=10000)
        # Timeline event-time phục vụ đối chiếu depth ↔ aggTrade; bounded RAM.
        self.trade_flow_timeline = deque(maxlen=5000)
        # Nén aggTrade theo giây cho flow 15s/60s. Pop thủ công để giữ đúng
        # rolling window và chặn RAM ở khoảng 65 phần tử.
        self.flow_1s_buffer = deque()
        self.last_trade_event_time_s = 0.0
        self.thoi_gian_dong_tien_cuoi = 0.0
        
        # kline live (Từ tai_nen_live.py)
        self.nen_live_1m = None
        self.nen_live_15m = None
        self.thoi_gian_nen_cuoi = 0.0
        
        # orderbook depth10 (Từ tai_so_lenh.py)
        self.bids_top_10 = []
        self.asks_top_10 = []
        self.thoi_gian_so_lenh_cuoi = 0.0
        # Retained compatibility storage for the retired depth research module.
        # The Tier-S launcher does not load it and no live decision may read it.
        self.futures_depth_bids_top_20 = []
        self.futures_depth_asks_top_20 = []
        self.futures_depth_metrics = {}
        self.futures_depth_updated_at = 0.0
        self.futures_depth_epoch = 0
        self.futures_depth_synced = False
        self.futures_depth_gap_count = 0

        # Vĩ Mô (Từ tai_vi_mo.py)
        self.open_interest = 0.0
        self.funding_rate = 0.0
        self.thoi_gian_vi_mo_cuoi = 0.0

        # --- Khối 2: Mapping / Tính toán (Đầu ra) ---
        self.ema9_m1 = 0.0          # EMA 9 của nến 1m (Dành cho Trailing Stop Khối 4)
        self.sweep_m1 = {'flag': False, 'direction': None, 'ts': 0} # Cờ báo hiệu nến M1 vừa quét thanh khoản (Dành cho cham_diem.py)
        self.breakout_m1 = {'flag': False, 'direction': None, 'ts': 0}
        # Ba nến đóng gần nhất đủ để nhận diện displacement dạng stair-step;
        # deque bounded giữ hot path O(1) và không làm RAM tăng theo thời gian.
        self.breakout_m1_history = deque(maxlen=3)
        
        self.atr_1m = 0.0           # Độ biến động trung bình nến 1m
        self.poc = 0.0              # Point of Control
        self.vah = 0.0              # Value Area High
        self.val = 0.0              # Value Area Low
        self.lvn_zones = []         # Low Volume Nodes
        
        self.trend_m15 = 'NEUTRAL'  # Xu hướng cấu trúc: BULLISH, BEARISH, NEUTRAL
        self.swing_high_m15 = 0.0   # Đỉnh cấu trúc (Buy Side Liquidity)
        self.swing_low_m15 = 0.0    # Đáy cấu trúc (Sell Side Liquidity)
        
        # CVD (Từ delta_cvd.py)
        self.cvd_day = None
        self.cvd_buy = 0.0
        self.cvd_sell = 0.0
        self.cvd_buy_30m = 0.0      # CVD Buy cuộn 30 phút
        self.cvd_sell_30m = 0.0     # CVD Sell cuộn 30 phút
        import collections
        # Không dùng maxlen: phần tử phải được pop thủ công để đồng thời trừ khỏi tổng rolling.
        self.cvd_30m_buffer = collections.deque()
        
        # [TRI-ORACLE] Tri-Oracle Divergence fields
        self.danh_sach_khop_lenh_futures = collections.deque(maxlen=500)
        self.futures_cvd_buy_total = 0.0
        self.futures_cvd_sell_total = 0.0
        self.spot_cvd_buy_total = 0.0
        self.spot_cvd_sell_total = 0.0
        self.coinbase_cvd_buy_total = 0.0
        self.coinbase_cvd_sell_total = 0.0
        self.long_liquidation_quote_total = 0.0
        self.short_liquidation_quote_total = 0.0
        self.liquidation_events = collections.deque(maxlen=128)
        self.coinbase_cvd_1m   = 0.0
        self.coinbase_volume_1m = 0.0
        self.coinbase_flow_1m_coverage_sec = 0.0
        self.coinbase_cvd_5m   = 0.0
        self.futures_cvd_1m    = 0.0
        self.tri_oracle_signal = 'NEUTRAL'
        self.thoi_gian_coinbase_cuoi = 0.0
        self.thoi_gian_dong_tien_futures_cuoi = 0.0
        
        # Lịch sử Volume 3s cho tính toán pct90
        self.vol_3s_history = collections.deque(maxlen=600)
        self.current_vol_3s = 0.0
        self.last_3s_window_ts = 0.0
        self.vol_pct90 = 0.0
        
        # CVD Delta 3s (riêng biệt, dùng cho cham_diem.py)
        self.current_cvd_sell_3s = 0.0
        self.current_cvd_buy_3s = 0.0
        
        # Flash Flow (Từ flash_flow.py)
        self.dt_deque = deque()       # Lưu (thời_gian, bin_index)
        self.dt_histogram = [0] * 501 # 500 Bins (0.1 BTC/bin, max 50 BTC) + 1 giỏ overflow
        self.p95_value = 3.0          # Giá trị Ngưỡng mặc định lúc mới bật
        self.dt_total_count = 0       # Tổng số lệnh đã xử lý
        self.dt_overflow_count = 0    # Số lệnh rơi vào overflow bin (>50 BTC)
        self.last_p95_ts = 0          # Timestamp lần cuối tính P95 (ms)
        
        # Footprint (Từ map_dong_tien/footprint.py)
        self.fp_candles = deque(maxlen=100)
        self.fp_current_candle = None
        self.fp_last_imbalance = {'dir': None, 'ts': 0, 'used': False}
        self.fp_last_eval_mono = 0.0
        self.continuous_footprint = {
            'active': False, 'direction': 0.0, 'ts': 0.0,
            'source_event_id': None, 'source_family': 'AGGTRADE',
        }
        
        # --- BIẾN VÀ LÕI TRUNG TÂM CHỈ HUY (Pre-Phase 3) ---
        self.vi_the_hien_tai = ViThe()
        self.hang_doi_tin_hieu = None  # Sẽ được khởi tạo bằng asyncio.Queue trong khoi_dong.py
        self.co_lenh_mo = False      # Khối 3 sẽ set = False khi đóng lệnh
        self.dang_xu_ly_dong_lenh = False # Cờ Lock chống bắn đúp Eject
        self.pending_close = None
        # Journal chỉ ghi vào deque/RAM ở hot path; task nền mới ghi đĩa/API.
        self.trade_cycles = {}
        self.journal_events = deque(maxlen=5000)
        # Telemetry nghiên cứu dùng queue riêng: quá tải shadow không được làm
        # mất cycle/order/fill live.
        self.continuous_shadow_events = deque(maxlen=2000)
        self.continuous_shadow_registry = {}
        self.side_calibration_shadow_registry = {}
        self.unresolved_forensic_fill_ids = deque(maxlen=1000)
        self.continuous_shadow_schedule = {}
        self.continuous_shadow_drop_count = 0
        self.continuous_shadow_health_errors = 0
        # Opportunity Scout/ML meta dùng đường telemetry riêng. Queue đầy chỉ
        # được drop mẫu nghiên cứu, tuyệt đối không backpressure live journal.
        self.ml_meta_events = deque(maxlen=2000)
        self.ml_meta_registry = {}
        self.ml_meta_drop_count = 0
        self.ml_meta_health_errors = 0
        self.ml_meta_last_collect_mono = 0.0
        self.journal_last_trade_time_ms = 0
        self.shadow_position = None
        # Shadow riêng cho tín hiệu bị fee floor chặn. Danh sách này chạy song
        # song với shadow của lệnh đã execute và không đụng trạng thái giao dịch.
        self.fee_blocked_shadow_positions = []
        self.fee_blocked_shadow_clusters = {}
        self.guardian_counterfactuals = []
        self.last_signal_time = 0.0  # Mốc entry gần nhất; cooldown chính quản lý theo setup_id
        self.prev_best_bid = 0.0     # Phục vụ bắt tín hiệu Cắt vào Zone
        self.prev_best_ask = 0.0
        self.arm_state = 'IDLE'      # Trạng thái Radar (IDLE, PRE_ARM, FULL_ARM)
        self.active_setups = {}
        # Funnel setup + counterfactual 15/30/45 phút cho cơ hội không execute.
        self.setup_outcomes = deque(maxlen=1000)
        self.setup_followups = deque(maxlen=300)
        self.structure_version = 0
        self.structure_transition = 'NONE'
        self.structure_broken_level = 0.0
        self.structure_break_streak = 0
        self.structure_last_close_time = 0
        self.decision_revision = 0
        # Revision song song, tuyệt đối không điều khiển cadence scorer live.
        self.continuous_evidence_revision = 0
        # Confirmed adverse Flash Flow outlives the rolling 3-second detector
        # through a bounded, continuously decaying memory.  It never acts as
        # a hard cooldown; the scorer decides its current weight.
        self.adverse_flow_memory_by_bias = {'LONG': {}, 'SHORT': {}}
        self.setup_generation = 0
        self.breakout_opportunities = {}
        self.breakout_opportunity_sequence = 0
        self.attempted_breakout_events = {}
        self.setup_cooldowns = {}
        self.rearm_blocks = {}
        # Recent exact setup terminal states are retained for an already-filled
        # position. Radar removes terminal setups from active_setups, otherwise
        # Guardian would lose the structural invalidation that happened in the
        # entry/Hard-SL race window.
        self.setup_terminal_by_identity = {}
        # One terminal maker intent per semantic opportunity. Radar clears the
        # block only when structure identity changes; rescore is not a sample.
        self.intent_terminal_opportunities = {}
        self.execution_in_flight = False
        self.execution_setup_id = None
        self.execution_generation = 0
        self.execution_client_order_id = None
        self.execution_unknown = False
        self.execution_unknown_since = 0.0
        self.last_execution_release_mono = 0.0
        self.consumed_market_events = {}
        self.current_mode = {'modes': ['STANDBY'], 'bias': 'NONE'}
        self.arm_diagnostics = {}
        self.system_ready = False
        self.trading_enabled = False
        self.execution_allowed = True
        self.reconcile_ready = False
        self.account_ready = False
        self.account_hedge_mode = True
        self.exchange_filters = {}
        self.balance_usdt = 0.0
        self.last_readiness_reason = "Đang khởi tạo"
        
        # Hàng đợi xử lý cho Khối 2 (Backpressure - Chống tràn RAM)
        self.hang_doi_so_lenh = deque(maxlen=50) # Drop-oldest nếu đầy
        self.hang_doi_nen_live = deque(maxlen=50) # Drop-oldest nếu đầy
        
        # --- KẾT QUẢ GIAI ĐOẠN 1 ---
        # Sổ lệnh (Từ map_so_lenh.py)
        self.obi = 0.0
        self.obi_top3 = 0.0
        self.obi_top10 = 0.0
        self.obi_history = deque(maxlen=30)
        self.wall_pull_flag = {'active': False, 'side': None, 'ts': 0}
        self.absorption_flag = False  # Giữ tương thích với code cũ
        self.absorption_event = {'active': False, 'side': None, 'ts': 0.0}
        self.absorption_trackers = deque(maxlen=12)
        self.absorption_reaction = {
            'active': False, 'classification': None,
            'direction': None, 'ts': 0.0, 'event_id': None,
        }
        self.flow_price_history = deque(maxlen=120)
        self.flow_price_last_sample = 0.0
        self.flow_divergence = {
            'active': False, 'direction': None, 'ts': 0.0, 'event_id': None,
        }
        self.continuous_flow_divergence = {
            'active': False, 'direction': 0.0, 'ts': 0.0,
            'source_event_id': None, 'source_family': 'AGGTRADE_PRICE',
        }
        self.value_area_excursions = {'LONG': None, 'SHORT': None}
        self.value_area_sweep = {
            'active': False, 'direction': None, 'ts': 0.0, 'event_id': None,
        }
        self.continuous_value_area_sweep = {
            'active': False, 'direction': 0.0, 'ts': 0.0,
            'source_event_id': None, 'source_family': 'PRICE_REACTION',
        }
        # Continuation context: flow nhiều cửa sổ + phản ứng cấu trúc tại
        # POC/VAH/VAL. Chỉ tạo CORE sau xác nhận, không đọc một snapshot OBI.
        self.persistent_flow = {
            'active': False, 'direction': None, 'ts': 0.0, 'event_id': None,
        }
        self.continuous_persistent_flow = {
            'active': False, 'direction': 0.0, 'ts': 0.0,
            'source_event_id': None, 'source_family': 'AGGTRADE',
        }
        self.flow_price_trap = {
            'active': False, 'blocked_bias': None, 'ts': 0.0,
            'event_id': None,
        }
        # 5 Hz x 190 giay + buffer nho. Continuous V2 chi doc cua so causal
        # 15/60/180s; bounded deque ngan RAM tang vo han.
        self.trend_price_history = deque(maxlen=1000)
        self.trend_context_last_update = 0.0
        self.trend_context_sequence = 0
        self.trend_zone_probe = None
        self.zone_reaction = {
            'active': False, 'direction': None, 'ts': 0.0, 'event_id': None,
        }
        self.continuous_zone_reaction = {
            'active': False, 'direction': 0.0, 'ts': 0.0,
            'source_event_id': None, 'source_family': 'PRICE_REACTION',
        }
        self.zone_acceptance_trap = {
            'active': False, 'blocked_bias': None, 'ts': 0.0,
            'event_id': None,
        }
        self.reversal_event_sequence = 0
        self.reversal_last_update = 0.0
        self.continuous_absorption_reaction = {
            'active': False, 'direction': 0.0, 'ts': 0.0,
            'source_event_id': None, 'parent_event_id': None,
            'source_family': 'DEPTH_AGGTRADE_PRICE',
        }
        self.continuous_m15 = {
            'active': False, 'direction': 0.0, 'ts': 0.0,
            'source_event_id': None, 'source_family': 'M15_CLOSED',
        }
        self.continuous_sweep_m1 = {
            'active': False, 'direction': 0.0, 'ts': 0.0,
            'source_event_id': None, 'source_family': 'PRICE_REACTION',
        }
        self.continuous_breakout_m1 = {
            'active': False, 'direction': 0.0, 'ts': 0.0,
            'source_event_id': None, 'source_family': 'M1_CLOSED',
        }
        
        # Nến live (Từ map_nen_live.py)
        self.ema9_m1 = 0.0
        
        # --- BIẾN VÁ LỖI GIAI ĐOẠN 1 ---
        # Dedupe nến M15
        self.last_processed_m15_live = 0
        self.last_processed_m1_live = 0
        
        # Sổ lệnh bẫy giá (Spoof/Absorption)
        self.prev_bid_vol = 0.0
        self.prev_ask_vol = 0.0
        self.prev_so_lenh_time_s = 0.0
        self.start_time = 0.0
        self.prev_bids_dict = {}
        self.prev_asks_dict = {}
        self.pending_pulls = deque(maxlen=20)
        self.market_event_sequence = 0
        self.market_log_times = {}
        self.log_ts_count = 0
        
        # Snapshot CVD fallback cho map_so_lenh (event-time chính, 0.5–1.0s)
        self.so_lenh_cvd_sell_snapshot = 0.0
        self.so_lenh_cvd_buy_snapshot = 0.0

        # Vĩ mô: lưu baseline để đánh giá OI tăng/giảm và freshness.
        self.prev_open_interest = 0.0
        self.prev_open_interest_updated_at = 0.0
        self.open_interest_change_pct = 0.0
        self.open_interest_change_window_seconds = 0.0
        self.macro_bias = 'NEUTRAL'
        self.last_mapped_macro_ts = 0.0
        self.macro_history = deque(maxlen=181)  # 15 phút @ 5 giây/mẫu
        self.positioning_cvd_divergence = {
            'active': False, 'direction': None, 'ts': 0.0,
            'event_id': None,
        }
        self.liquidation_recovery = {
            'active': False, 'direction': None, 'ts': 0.0,
            'event_id': None,
        }
        self.shark_context = {
            'side': None, 'status': 'NEUTRAL', 'support_count': 0, 'adverse_count': 0,
            'support': [], 'adverse': [], 'ts': 0.0,
        }

# Khởi tạo biến toàn cục (Singleton)
state = SharedState()
