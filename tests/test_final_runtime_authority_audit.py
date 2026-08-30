import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "final_runtime_authority_audit",
    ROOT / "ops" / "final_runtime_authority_audit.py",
)
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


class FinalRuntimeAuthorityAuditTest(unittest.TestCase):
    def test_service_running_from_old_checkout_is_not_called_canonical(self):
        findings = audit.classify_service(
            "wstrade-health.service", "active", 42,
            "/home/ubuntu/WStrade_backup", Path("/home/ubuntu/WStrade"),
        )
        self.assertEqual(findings[0]["code"], "SERVICE_CODE_ROOT_MISMATCH")

    def test_collect_only_environment_is_fail_closed(self):
        env = (
            "WSTRADE_MODE=SHADOW SMC_ENABLE_TRADING=false "
            "SMC_MAINNET_ARMED=false SMC_MAINNET_EXCLUSIVE_ACCOUNT=false"
        )
        self.assertEqual(audit.safety_findings(env), [])
        self.assertEqual(
            audit.safety_findings(env.replace("SMC_MAINNET_ARMED=false", "SMC_MAINNET_ARMED=true"))[0]["code"],
            "REAL_MONEY_NOT_FAIL_CLOSED",
        )

    def test_unbounded_large_active_journal_is_failure(self):
        findings = audit.journal_findings(2_000_000_000, False)
        self.assertEqual(findings[0]["code"], "ACTIVE_JOURNAL_UNBOUNDED")
        self.assertEqual(findings[0]["severity"], "FAIL")

    def test_absolute_timestamp_is_not_latency(self):
        report = audit.inspect_follow_age([
            {"ignition": {"futures_response_ms": 1_788_091_435_602}},
            {"boundary": {"futures_follow_age": 240.0}},
        ])
        self.assertEqual(report["observed"], 2)
        self.assertEqual(report["absolute_timestamp_values"], 1)
        self.assertEqual(report["latency_values"], 1)


if __name__ == "__main__":
    unittest.main()
