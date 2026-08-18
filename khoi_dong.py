"""Compatibility shim onto the canonical lean Tier-S bootstrap."""
from loi_he_thong import tier_s_bootstrap_modules as _m
from loi_he_thong import tier_s_bootstrap_runtime as _r

CURRENT_DIR = _m.CURRENT_DIR
load_module = _m.load_module

tai_nen_offline = _m.tai_nen_offline
tai_gia_tick = _m.tai_gia_tick
tai_vi_mo = _m.tai_vi_mo
tai_coinbase = _m.tai_coinbase
tai_dong_tien = _m.tai_dong_tien
delta_cvd = _m.delta_cvd
ATR = _m.ATR
bo_nho_ram = _m.bo_nho_ram
binance_api = _m.binance_api
dat_lenh = _m.dat_lenh
bao_ve_khan_cap = _m.bao_ve_khan_cap
dong_bo_trang_thai = _m.dong_bo_trang_thai
nhat_ky_giao_dich = _m.nhat_ky_giao_dich
giam_sat_he_thong = _m.giam_sat_he_thong
chi_huy_truong = _m.chi_huy_truong

tai_so_lenh = _m.tai_so_lenh
tai_nen_live = _m.tai_nen_live
tri_oracle = _m.tri_oracle
flash_flow = _m.flash_flow
footprint = _m.footprint
map_so_lenh = _m.map_so_lenh
map_nen_live = _m.map_nen_live
map_vi_mo = _m.map_vi_mo
POC_VAH_VAL = _m.POC_VAH_VAL
BOS_CHoCH = _m.BOS_CHoCH
chon_che_do = _m.chon_che_do
map_gia_tick = _m.map_gia_tick
tho_san_trailing = _m.tho_san_trailing

state = _r.state
api = _r.api
supervise = _r.supervise
vong_lap_runtime_heartbeat = _r.vong_lap_runtime_heartbeat
khoi_tao_tai_khoan = _r.khoi_tao_tai_khoan
seconds_to_next_boundary = _r.seconds_to_next_boundary
parse_btc_filters = _r.parse_btc_filters
acquire_runtime_lock = _r.acquire_runtime_lock
DuplicateInstanceError = _r.DuplicateInstanceError
uvloop = _r.uvloop

async def main():
    return await _r.main()

if __name__ == "__main__":
    _r.run_direct()
