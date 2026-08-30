#!/usr/bin/env python3
"""Idempotently create the WStrade 26% warning and 30% critical alarms."""

import os


def configure():
    instance = os.getenv("WSTRADE_LIGHTSAIL_INSTANCE_NAME", "").strip()
    region = os.getenv("AWS_REGION", os.getenv("AWS_DEFAULT_REGION", "")).strip()
    if not instance or not region:
        raise RuntimeError("LIGHTSAIL_INSTANCE_OR_REGION_MISSING")
    import boto3
    client = boto3.client("lightsail", region_name=region)
    common = {
        "metricName": "CPUUtilization",
        "monitoredResourceName": instance,
        "comparisonOperator": "GreaterThanOrEqualToThreshold",
        "treatMissingData": "breaching",
        "contactProtocols": ["Email"],
        "notificationTriggers": ["ALARM"],
        "notificationEnabled": True,
    }
    alarms = (
        ("wstrade-cpu-warning-26", 26.0, 3, 3),
        ("wstrade-cpu-critical-30", 30.0, 1, 1),
    )
    for name, threshold, periods, datapoints in alarms:
        client.put_alarm(
            alarmName=name, threshold=threshold,
            evaluationPeriods=periods, datapointsToAlarm=datapoints, **common,
        )
    return [name for name, *_ in alarms]


if __name__ == "__main__":
    for alarm in configure():
        print(alarm)
