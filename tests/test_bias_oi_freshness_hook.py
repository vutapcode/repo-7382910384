import inspect
import unittest

from loi_he_thong import bias_oi_freshness_hook as hook
from loi_he_thong import ignition_core


class BiasOiFreshnessHookTests(unittest.TestCase):
    def test_install_changes_only_bias_oi_age(self):
        class BiasModule:
            OI_AGE = 12.0

        before = {
            "remember": ignition_core._remember_bias,
            "oi": ignition_core._oi_intent,
            "phase": ignition_core._phase_measurement,
            "result": ignition_core._result_from_episode,
        }
        self.assertEqual(hook.install(BiasModule), 18.0)
        self.assertEqual(BiasModule.OI_AGE, 18.0)
        self.assertEqual(BiasModule.BIAS_OI_FRESHNESS_POLICY, hook.VERSION)
        self.assertIs(ignition_core._remember_bias, before["remember"])
        self.assertIs(ignition_core._oi_intent, before["oi"])
        self.assertIs(ignition_core._phase_measurement, before["phase"])
        self.assertIs(ignition_core._result_from_episode, before["result"])
        self.assertIn("episode", inspect.signature(
            ignition_core._phase_measurement
        ).parameters)


if __name__ == "__main__":
    unittest.main()
