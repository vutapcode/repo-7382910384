"""Fetch Lightsail CPUUtilization and atomically publish 15m/1h averages."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import time


OUTPUT = Path(os.getenv(
    "WSTRADE_LIGHTSAIL_CPU_PATH",
    "/home/ubuntu/smc2026_data/health/lightsail_cpu.json",
))


def _atomic(payload):
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix="lightsail_cpu_", suffix=".tmp", dir=OUTPUT.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, OUTPUT)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def averages(datapoints, now=None):
    now = datetime.now(timezone.utc) if now is None else now
    rows = []
    for row in datapoints or ():
        stamp = row.get("timestamp")
        value = row.get("average")
        if stamp is None or value is None:
            continue
        if isinstance(stamp, str):
            stamp = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        rows.append((stamp.astimezone(timezone.utc), float(value)))
    def mean(seconds, minimum):
        cutoff = now - timedelta(seconds=seconds)
        values = [value for stamp, value in rows if stamp > cutoff]
        return (sum(values) / len(values), len(values)) if len(values) >= minimum else (None, len(values))
    cpu15, count15 = mean(900, 3)
    cpu1h, count1h = mean(3600, 12)
    return cpu15, cpu1h, count15, count1h


def refresh(now=None):
    instance = os.getenv("WSTRADE_LIGHTSAIL_INSTANCE_NAME", "").strip()
    region = os.getenv("AWS_REGION", os.getenv("AWS_DEFAULT_REGION", "")).strip()
    if not instance or not region:
        raise RuntimeError("LIGHTSAIL_INSTANCE_OR_REGION_MISSING")
    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError("BOTO3_NOT_INSTALLED") from exc
    now = datetime.now(timezone.utc) if now is None else now
    from botocore.config import Config
    client = boto3.client(
        "lightsail", region_name=region,
        config=Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 2}),
    )
    response = client.get_instance_metric_data(
        instanceName=instance,
        metricName="CPUUtilization",
        period=300,
        startTime=now - timedelta(minutes=70),
        endTime=now,
        unit="Percent",
        statistics=["Average"],
    )
    cpu15, cpu1h, count15, count1h = averages(response.get("metricData"), now=now)
    if cpu15 is None or cpu1h is None:
        raise RuntimeError("LIGHTSAIL_CPU_DATAPOINTS_INCOMPLETE")
    payload = {
        "schema_version": 1,
        "source": "AWS_LIGHTSAIL_CPUUTILIZATION",
        "updated_at_ms": int(time.time() * 1000),
        "cpu_15m_pct": cpu15,
        "cpu_1h_pct": cpu1h,
        "datapoints_15m": count15,
        "datapoints_1h": count1h,
        "window_15m_start_ms": int((now - timedelta(minutes=15)).timestamp() * 1000),
        "window_1h_start_ms": int((now - timedelta(hours=1)).timestamp() * 1000),
        "window_semantics": "ROLLING_HOST_METRIC_NOT_BOT_RESTART_SCOPED",
    }
    _atomic(payload)
    return payload


def main():
    refresh()


if __name__ == "__main__":
    main()
