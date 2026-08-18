"""Lean production task plan for Mainnet Tier-S shadow."""
import asyncio import logging
from loi_he_thong import tier_s_atr_only

VERSION = "TIER_S_RUNTIME_PRUNE_V2"


async def _spot_flow_loop(app):
    state = app.state
    while float(getattr(state, "best_bid", 0.0) or 0.0) <= 0.0:
        await asyncio.sleep(0.1)
    while True:
        if state.danh_sach_khop_lenh:
            trade = state.danh_sach_khop_lenh.popleft()
            app.delta_cvd.cap_nhat_cvd(trade, state)
        else:
            app.delta_cvd.kiem_tra_idle(state)
            await asyncio.sleep(0.001)


async def _lean_main(app):
    state = app.state
    state.hang_doi_tin_hieu = asyncio.Queue(maxsize=5)
    state.tier_s_lean_runtime = True
    state.tier_s_runtime_prune_version = VERSION

    app.giam_sat_he_thong.start_out_of_band_watchdog(state)
    await app.khoi_tao_tai_khoan()

    task_specs = (
        ("bookTicker", lambda: app.tai_gia_tick.hung_gia_tick_futures("btcusdt", state)),
        ("aggTrade_spot", lambda: app.tai_dong_tien.hung_dong_tien_spot("btcusdt", state)),
        ("coinbase_spot", lambda: app.tai_coinbase.hung_coinbase_spot("BTC-USD", state)),
        ("aggTrade_futures", lambda: app.tai_dong_tien.hung_dong_tien_futures_real("btcusdt", state)),
        ("executionBookTicker", lambda: app.tai_gia_tick.hung_gia_tick_execution(
            "btcusdt", state, testnet=app.api.testnet
        ))
        ("spot_cvd", lambda: _spot_flow_loop(app)),
        ("vi_mo_input", lambda: app.tai_vi_mo.tai_du_lieu_vi_mo("BTCUSDT", state)),
        ("M1_atr", lambda: tier_s_atr_only.run(app)),
        ("watchdog", lambda: app.giam_sat_he_thong.vong_lap_giam_sat(state)),
        ("runtime_heartbeat", app.vong_lap_runtime_heartbeat),
    )
    tasks = [
        asyncio.create_task(app.supervise(name, factory), name=f"tier_s:{name}")
        for name, factory in task_specs
    ]
    state.tier_s_active_tasks = tuple(name for name, _ in task_specs)
    logging.info("[TIER-S] lean runtime active: %s", ", ".join(state.tier_s_active_tasks))
    await asyncio.gather(*tasks)
    raise RuntimeError("TIER_S_LEAN_DATA_ORCHESTRATOR_EXITED_UNEXPECTEDLY")


def install_app(app):
    async def _installed_main():
        return await _lean_main(app)
    app.main = _installed_main
    app.state.tier_s_runtime_prune_version = VERSION
    return VERSION


def install(runtime):
    return install_app(runtime.base.app)
