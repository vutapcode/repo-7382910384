"""CLI entrypoint for the isolated Gemini shadow service."""

import argparse
import asyncio
import json
import logging

from .client import GeminiShadowClient
from .config import ShadowConfig
from .data import RecorderReader
from .storage import ShadowStore
from .worker import ShadowWorker


def parse_args():
    parser = argparse.ArgumentParser(description='SMC2026 Gemini shadow analyst')
    parser.add_argument('--once', action='store_true', help='Process one live batch then exit')
    parser.add_argument('--dry-run', action='store_true', help='Build inputs without API calls/writes')
    parser.add_argument('--replay-limit', type=int, help='Analyze the newest N historical cycles')
    return parser.parse_args()


async def async_main(args):
    config = ShadowConfig()
    reader = RecorderReader(config.data_root, config.cycles_path)
    store = ShadowStore(config.output_root)
    client = None
    if not args.dry_run:
        if not config.api_key:
            logging.error('[GEMINI SHADOW] missing systemd credential gemini_api_key')
            store.health('DISABLED_MISSING_CREDENTIAL', model=config.model)
            return 2
        client = GeminiShadowClient(config.api_key, config)
    worker = ShadowWorker(config, reader, store, client=client)
    try:
        if args.dry_run or args.once or args.replay_limit is not None:
            outcomes = await worker.run_once(
                replay_limit=args.replay_limit,
                include_regime=args.replay_limit is None,
                dry_run=args.dry_run,
            )
            print(json.dumps(outcomes, ensure_ascii=False, indent=2))
            return 0 if all(row.get('result') != 'ERROR' for row in outcomes) else 1
        await worker.run_forever()
        return 0
    finally:
        if client is not None:
            await client.close()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s',
    )
    raise SystemExit(asyncio.run(async_main(parse_args())))


if __name__ == '__main__':
    main()
