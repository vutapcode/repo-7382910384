import importlib.util
from collections import deque
from pathlib import Path
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("gs", ROOT/"3_thuc_thi"/"ve_si_lenh"/"guardian_s_tier.py")
gs = importlib.util.module_from_spec(spec); spec.loader.exec_module(gs)
spec = importlib.util.spec_from_file_location("gl", ROOT/"guardian_s_launcher.py")
gl = importlib.util.module_from_spec(spec); spec.loader.exec_module(gl)

def make(now=1000.0, price=100.0):
    p=SimpleNamespace(side="LONG",active=True,qty=.001,position_cycle_id="c",opened_at=990.0,
                      guardian_s_candidate_since=0.0,guardian_s_signature=())
    s=SimpleNamespace(best_bid=price-.005,best_ask=price+.005,coinbase_price=price,
                      thoi_gian_coinbase_ticker_cuoi=now,atr_1m=.5,open_interest=1000.0,
                      flow_1s_buffer=deque(),danh_sach_khop_lenh_futures=deque(),
                      coinbase_flow_3s_ts=now,coinbase_volume_3s=0.0,coinbase_cvd_3s=0.0)
    s.guardian_s_ident=("c","LONG",990.0)
    s.guardian_s_prices=deque([
        {"ts":now-3.2,"spot":100.0,"coinbase":100.0,"futures":100.0},
        {"ts":now-1.2,"spot":100.0,"coinbase":100.0,"futures":100.0},
        {"ts":now-.35,"spot":100.0,"coinbase":100.0,"futures":100.0}],maxlen=256)
    s.guardian_s_oi=deque([{"ts":now-11,"oi":1000.0,"spot":100.0}],maxlen=128)
    return s,p

def fut(s,now,px,buy):
    s.danh_sach_khop_lenh_futures=deque([{"thoi_gian_ms":int(now*1000),"gia":px,
        "khoi_luong":5.0,"ban_chu_dong":not buy}])

class TestGuardianS(unittest.TestCase):
    def test_flow_without_price_conversion_holds(self):
        s,p=make(); s.flow_1s_buffer.append({"ts":999.8,"buy":1.0,"sell":9.0})
        fut(s,1000,100.0,False); s.coinbase_cvd_3s=-8.; s.coinbase_volume_3s=10.
        r=gs.assess(s,p,1000.0)
        self.assertEqual((r["decision"],r["reason"]),("HOLD","ADVERSE_FLOW_NOT_CONVERTED_TO_PRICE"))

    def test_two_s_converge_then_exit_and_legacy_fields_do_not_vote(self):
        s,p=make(price=99.9); s.coinbase_price=99.9
        s.flow_1s_buffer.append({"ts":999.8,"buy":1.0,"sell":9.0})
        s.coinbase_cvd_3s=-8.; s.coinbase_volume_3s=10.; fut(s,1000,99.9,False)
        first=gs.assess(s,p,1000.0); self.assertEqual(first["decision"],"WATCH")
        s.trend_m15="BEARISH"; s.structure_transition="TRANSITION_BEARISH"; s.ema9_m1=999999.
        s.poc=1.; s.obi=-.999; s.wall_pull_flag={"active":True}
        s.thoi_gian_coinbase_ticker_cuoi=1000.6; s.coinbase_flow_3s_ts=1000.6
        s.flow_1s_buffer.append({"ts":1000.5,"buy":1.0,"sell":9.0}); fut(s,1000.6,99.9,False)
        second=gs.assess(s,p,1000.6)
        self.assertEqual(second["decision"],"EXIT")
        self.assertEqual(set(second["votes"]),{"S1_price_acceptance","S2_executed_flow","S3_price_x_oi"})

    def test_launcher_gates_only_discretionary_early_exit(self):
        self.assertEqual(gl.gate_legacy_reason("SOFT_SL",{"decision":"HOLD"}),(True,"SOFT_SL"))
        self.assertEqual(gl.gate_legacy_reason("TRI_ORACLE_EJECT",{"decision":"HOLD"}),(False,"TRI_ORACLE_EJECT"))
        self.assertEqual(gl.gate_legacy_reason("AUG13_CAUSAL_REVERSAL",{"decision":"EXIT"}),(True,"TIER_S_CAUSAL_EXIT"))

if __name__=="__main__": unittest.main()
