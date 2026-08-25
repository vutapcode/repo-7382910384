import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ops import lightsail_cpu_probe
from ops.lightsail_cpu_probe import averages


class LightsailCpuProbeTests(unittest.TestCase):
    def test_requires_complete_15m_and_1h_datapoint_coverage(self):
        now = datetime(2026, 8, 22, tzinfo=timezone.utc)
        rows = [
            {"timestamp": now - timedelta(minutes=5 * index), "average": 10 + index}
            for index in range(12)
        ]
        cpu15, cpu1h, count15, count1h = averages(rows, now=now)
        self.assertEqual(count15, 3)
        self.assertEqual(count1h, 12)
        self.assertAlmostEqual(cpu15, 11.0)
        self.assertAlmostEqual(cpu1h, 15.5)

        cpu15, cpu1h, _, count1h = averages(rows[:11], now=now)
        self.assertIsNotNone(cpu15)
        self.assertIsNone(cpu1h)
        self.assertEqual(count1h, 11)

    def test_refresh_labels_rolling_window_boundaries(self):
        now = datetime(2026, 8, 22, tzinfo=timezone.utc)
        rows = [
            {"timestamp": now - timedelta(minutes=5 * index), "average": 10.0}
            for index in range(12)
        ]
        client = SimpleNamespace(
            get_instance_metric_data=lambda **_kwargs: {"metricData": rows}
        )
        boto3 = SimpleNamespace(client=lambda *_args, **_kwargs: client)
        config_module = SimpleNamespace(Config=lambda **kwargs: kwargs)
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            "os.environ", {
                "WSTRADE_LIGHTSAIL_INSTANCE_NAME": "test",
                "AWS_REGION": "test",
            }
        ), patch.dict(sys.modules, {
            "boto3": boto3,
            "botocore": SimpleNamespace(config=config_module),
            "botocore.config": config_module,
        }), patch.object(
            lightsail_cpu_probe, "OUTPUT", Path(temp) / "lightsail.json"
        ):
            payload = lightsail_cpu_probe.refresh(now=now)
        self.assertEqual(
            payload["window_15m_start_ms"],
            int((now - timedelta(minutes=15)).timestamp() * 1000),
        )
        self.assertEqual(
            payload["window_1h_start_ms"],
            int((now - timedelta(hours=1)).timestamp() * 1000),
        )
        self.assertEqual(
            payload["window_semantics"],
            "ROLLING_HOST_METRIC_NOT_BOT_RESTART_SCOPED",
        )


if __name__ == "__main__":
    unittest.main()
