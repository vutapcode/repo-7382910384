"""Active Binance Futures MAINNET Tier-S module loader plus inert legacy shells."""
import asyncio
import importlib.util
from pathlib import Path

CURRENT_DIR = Path(__file__).resolve().parent.parent

def load_module(module_name, file_path):
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {module_name} from {file_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

class LegacyInert:
    """Compatibility shell for retired modules touched only by launcher neutralizers."""
    def __getattr__(self, name):
        async def _idle(*_args, **_kwargs):
            while True:
                await asyncio.sleep(60.0)
        if name.startswith("hung_") or name.startswith("vong_lap_"):
            return _idle
        return lambda *_args, **_kwargs: None

# Active Tier-S data/inference dependencies.
tai_nen_offline = load_module("tai_nen_offline", CURRENT_DIR/"1_tai_du_lieu"/"tai_nen_offline"/"tai_nen_offline.py")
tai_gia_tick = load_module("tai_gia_tick", CURRENT_DIR/"1_tai_du_lieu"/"tai_gia_tick"/"tai_gia_tick.py")
tai_vi_mo = load_module("tai_vi_mo", CURRENT_DIR/"1_tai_du_lieu"/"tai_vi_mo"/"tai_vi_mo.py")
tai_coinbase = load_module("tai_coinbase", CURRENT_DIR/"1_tai_du_lieu"/"tai_coinbase"/"tai_coinbase.py")
tai_dong_tien = load_module("tai_dong_tien", CURRENT_DIR/"1_tai_du_lieu"/"tai_dong_tien"/"tai_dong_tien.py")
delta_cvd = load_module("delta_cvd", CURRENT_DIR/"2_suy_luan_mapping"/"map_dong_tien"/"delta_cvd.py")
ATR = load_module("ATR", CURRENT_DIR/"2_suy_luan_mapping"/"map-nen-offline"/"ATR.py")
bo_nho_ram = load_module("bo_nho_ram", CURRENT_DIR/"loi_he_thong"/"bo_nho_ram.py")

# Mainnet execution/account primitives.
binance_api = load_module("binance_api", CURRENT_DIR/"3_thuc_thi"/"binance_api.py")
giam_sat_he_thong = load_module("giam_sat_he_thong", CURRENT_DIR/"3_thuc_thi"/"giam_sat_he_thong.py")

# Retired/testnet-era compatibility names. No physical legacy implementation is loaded.
dat_lenh = LegacyInert()
bao_ve_khan_cap = LegacyInert()
dong_bo_trang_thai = LegacyInert()
nhat_ky_giao_dich = LegacyInert()
chi_huy_truong = LegacyInert()
tai_so_lenh = LegacyInert()
tai_nen_live = LegacyInert()
tri_oracle = LegacyInert()
flash_flow = LegacyInert()
footprint = LegacyInert()
map_so_lenh = LegacyInert()
map_nen_live = LegacyInert()
map_vi_mo = LegacyInert()
POC_VAH_VAL = LegacyInert()
BOS_CHoCH = LegacyInert()
chon_che_do = LegacyInert()
map_gia_tick = LegacyInert()
tho_san_trailing = LegacyInert()
