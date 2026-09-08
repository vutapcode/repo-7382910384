"""Active Binance Futures MAINNET Tier-S module loader plus inert legacy shells."""
import asyncio
import importlib
import importlib.util
from pathlib import Path
import sys

CURRENT_DIR = Path(__file__).resolve().parent.parent
_MODULES_BY_PATH = {}


def _resolved_module_path(module):
    raw = getattr(module, "__file__", None)
    if not raw:
        return None
    try:
        return Path(raw).resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def _canonical_import_name(file_path):
    """Return an importable repo-relative name where Python permits one."""
    try:
        relative = Path(file_path).resolve().relative_to(CURRENT_DIR)
    except ValueError:
        return None
    parts = relative.with_suffix("").parts
    if not parts or not all(part.isidentifier() for part in parts):
        return None
    return ".".join(parts)

def load_module(module_name, file_path):
    resolved = Path(file_path).resolve()
    cached = _MODULES_BY_PATH.get(resolved)
    if cached is not None:
        sys.modules.setdefault(module_name, cached)
        return cached

    # Modules imported before bootstrap (for example Ignition through
    # execution revalidation) must be reused, not executed a second time under
    # a runtime alias.  File identity is canonical even for historical source
    # directories whose names are not valid Python identifiers.
    for existing in tuple(sys.modules.values()):
        if existing is not None and _resolved_module_path(existing) == resolved:
            _MODULES_BY_PATH[resolved] = existing
            sys.modules.setdefault(module_name, existing)
            return existing

    canonical_name = _canonical_import_name(resolved)
    if canonical_name:
        try:
            existing = importlib.import_module(canonical_name)
        except ModuleNotFoundError:
            existing = None
        if existing is not None:
            if _resolved_module_path(existing) != resolved:
                raise ImportError(
                    f"canonical module path mismatch for {canonical_name}"
                )
            _MODULES_BY_PATH[resolved] = existing
            sys.modules.setdefault(module_name, existing)
            return existing

    load_name = canonical_name or module_name
    spec = importlib.util.spec_from_file_location(load_name, resolved)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {module_name} from {file_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[load_name] = module
    sys.modules.setdefault(module_name, module)
    try:
        spec.loader.exec_module(module)
    except Exception:
        if sys.modules.get(load_name) is module:
            sys.modules.pop(load_name, None)
        if sys.modules.get(module_name) is module:
            sys.modules.pop(module_name, None)
        raise
    _MODULES_BY_PATH[resolved] = module
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
# Fail-closed at the module-registry boundary before any launcher policy can run.
bo_nho_ram.state.execution_allowed = False

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
# Preserve the historical all-caps spelling exported by the kernel contract.
BOS_CHOCH = BOS_CHoCH
chon_che_do = LegacyInert()
map_gia_tick = LegacyInert()
tho_san_trailing = LegacyInert()
