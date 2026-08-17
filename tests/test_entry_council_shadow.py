
from pathlib import Path
import ast, unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT/"loi_he_thong"/"entry_council_shadow.py"

class TestEntryCausalV2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = SRC.read_text(encoding="utf-8")
        ast.parse(cls.text)

    def test_version_and_phases(self):
        self.assertIn("ENTRY_COUNCIL_CAUSAL_V2", self.text)
        for x in ("PRESSURE_BUILDING","ACCEPTANCE","RELEASE","WAIT_CHASE"):
            self.assertIn(x, self.text)

    def test_normal_quorum_is_price_plus_flow(self):
        self.assertIn('s1_status == "PASS" and s2_status == "PASS"', self.text)
        self.assertIn('"CAUSAL_PRICE_FLOW_QUORUM"', self.text)

    def test_fast_lane_is_rare(self):
        for x in ("FAST_BIAS_MIN_CONF", 'len(pf["strong_supporters"]) == 3',
                  'len(flow["strong_supporters"]) >= 1',
                  'not flow["strong_opponents"]'):
            self.assertIn(x, self.text)

    def test_s3_is_validator_not_quorum(self):
        self.assertIn('"S3_causal_response_validator"', self.text)
        self.assertNotIn('int(s3_status == "PASS")', self.text)

    def test_legacy_features_absent(self):
        for x in ("trend_m15","structure_transition","poc","vah","val","obi","wall_pull_flag"):
            self.assertNotIn(x, self.text)

if __name__ == "__main__":
    unittest.main()
