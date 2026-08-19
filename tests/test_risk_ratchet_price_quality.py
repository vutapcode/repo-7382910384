import unittest
from types import SimpleNamespace
from loi_he_thong import risk_ratchet_price_quality_hook as h

class T(unittest.TestCase):
    def test_outlier(self):
        p=SimpleNamespace(side="LONG")
        self.assertTrue(h._is_favorable_outlier(p,102.0,101.0,6.0))

    def test_exit_passthrough(self):
        def assess(*a,**k): return {"decision":"EXIT","reason":"HARD_SL"}
        r=SimpleNamespace(assess=assess)
        h.install(r)
        out=r.assess(SimpleNamespace(),99.0)
        self.assertEqual(out,{"decision":"EXIT","reason":"HARD_SL"})

if __name__=="__main__":
    unittest.main()
