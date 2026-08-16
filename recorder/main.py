"""Entrypoint for the SMC2026 read-only market recorder."""

import asyncio
import logging
import signal

from loi_he_thong.runtime_lock import DuplicateInstanceError, acquire_runtime_lock
from recorder.collector import BinanceRecorder
from recorder.config import RecorderConfig
from recorder.decision_tap import DecisionTap
from recorder.health import HealthState, health_loop
from recorder.features import FeatureEngine
from recorder.metadata import code_version, config_version
from recorder.storage import AppendOnlyStore


async def run():
    config = RecorderConfig()
    config.data_root.mkdir(parents=True, exist_ok=True)
    health = HealthState(config)
    store = AppendOnlyStore(config, health)
    retention = await store.prune_once()
    code_id = code_version()
    config_id = config_version(config)
    health.code_version = code_id
    health.config_version = config_id
    feature_engine = FeatureEngine(
        config, store.publish, health, code_id, config_id
    )
    collector = BinanceRecorder(
        config, store, health, feature_engine, code_id, config_id
    )
    tap = DecisionTap(config, collector.emit, health)
    await collector.start_session()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    writer = asyncio.create_task(store.writer_loop(), name='writer')
    tasks = [
        asyncio.create_task(store.compactor_loop(), name='compactor'),
        asyncio.create_task(store.retention_loop(), name='retention'),
        asyncio.create_task(health_loop(health), name='health'),
        asyncio.create_task(collector.public_loop(), name='public_ws'),
        asyncio.create_task(collector.market_loop(), name='market_ws'),
        asyncio.create_task(collector.macro_poll_loop(), name='macro_rest'),
        asyncio.create_task(tap.loop(), name='decision_tap'),
    ]
    logging.info(
        '[RECORDER] started symbol=%s root=%s public-only=true retention=%sh '
        'startup_deleted=%s startup_freed_bytes=%s',
        config.symbol, config.data_root, config.retention_hours,
        retention['files_deleted'], retention['bytes_deleted'],
    )
    try:
        await stop.wait()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        feature_engine.close()
        try:
            await asyncio.wait_for(store.stop_writer(), timeout=5.0)
        except asyncio.TimeoutError:
            health.error('shutdown', 'QUEUE_DRAIN_TIMEOUT')
            writer.cancel()
        await asyncio.gather(writer, return_exceptions=True)
        await collector.close_session()
        logging.info('[RECORDER] stopped cleanly')


def main():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s',
    )
    try:
        runtime_lock = acquire_runtime_lock('recorder')
    except DuplicateInstanceError as exc:
        logging.critical('[RECORDER] %s', exc)
        raise SystemExit(73) from exc
    try:
        asyncio.run(run())
    finally:
        runtime_lock.close()


if __name__ == '__main__':
    main()
