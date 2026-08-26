from types import SimpleNamespace
import unittest

from loi_he_thong import regime_oi_freshness_hook


class RegimeOIFreshnessHookTests(unittest.TestCase):
    def test_normal_poll_allows_interval_plus_jitter(self):
        state = SimpleNamespace(oi_poll_interval_effective_seconds=15.0)
        self.assertEqual(regime_oi_freshness_hook.max_oi_age_seconds(state), 18.0)

    def test_pressure_poll_has_bounded_eight_second_age(self):
        state = SimpleNamespace(oi_poll_interval_effective_seconds=5.0)
        self.assertEqual(regime_oi_freshness_hook.max_oi_age_seconds(state), 8.0)

    def test_age_bound_is_never_unbounded(self):
        state = SimpleNamespace(oi_poll_interval_effective_seconds=120.0)
        self.assertEqual(regime_oi_freshness_hook.max_oi_age_seconds(state), 18.0)


if __name__ == "__main__":
    unittest.main()
