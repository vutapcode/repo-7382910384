import json
import stat
import tempfile
import unittest
from pathlib import Path

from recorder.account_audit import build_snapshot, write_snapshot


class AccountAuditTests(unittest.TestCase):
    def test_snapshot_never_embeds_raw_credentials_and_derives_round_trip(self):
        raw = {
            'balances': [{'asset': 'USDT', 'balance': '1000', 'availableBalance': '990'}],
            'positions': [{'symbol': 'BTCUSDT', 'positionSide': 'LONG', 'positionAmt': '0'}],
            'open_orders': [],
            'open_algos': {'orders': []},
            'all_algos': {'orders': []},
            'all_orders': [
                {'orderId': 1, 'clientOrderId': 'smc_entry', 'side': 'BUY',
                 'positionSide': 'LONG', 'status': 'FILLED', 'time': 1000,
                 'updateTime': 1000},
                {'orderId': 2, 'clientOrderId': 'smc_close', 'side': 'SELL',
                 'positionSide': 'LONG', 'status': 'FILLED', 'time': 2000,
                 'updateTime': 2000},
            ],
            'trades': [
                {'id': 10, 'orderId': 1, 'side': 'BUY', 'positionSide': 'LONG',
                 'price': '100', 'qty': '1', 'quoteQty': '100',
                 'realizedPnl': '0', 'commission': '0.04', 'time': 1000},
                {'id': 11, 'orderId': 2, 'side': 'SELL', 'positionSide': 'LONG',
                 'price': '99', 'qty': '1', 'quoteQty': '99',
                 'realizedPnl': '-1', 'commission': '0.0396', 'time': 2000},
            ],
            'income': [],
            'errors': [],
        }
        with tempfile.TemporaryDirectory() as temp:
            cycles = Path(temp) / 'cycles.json'
            cycles.write_text(json.dumps({'cycles': []}))
            snapshot = build_snapshot(
                raw, 'BTCUSDT', Path(temp) / '.env',
                'LIVE_API_KEY_VALUE', 'LIVE_SECRET_VALUE', cycles,
            )
        encoded = json.dumps(snapshot)
        self.assertNotIn('LIVE_API_KEY_VALUE', encoded)
        self.assertNotIn('LIVE_SECRET_VALUE', encoded)
        self.assertFalse(
            snapshot['access']['credential_reference']['raw_credentials_embedded']
        )
        trip = snapshot['recent_round_trips'][0]
        self.assertAlmostEqual(trip['total_fees'], 0.0796)
        self.assertAlmostEqual(trip['net_pnl_after_fees'], -1.0796)

    def test_snapshot_writer_is_private_and_atomic(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'snapshot.json'
            write_snapshot(target, {'ok': True})
            self.assertEqual(json.loads(target.read_text()), {'ok': True})
            mode = stat.S_IMODE(target.stat().st_mode)
            self.assertEqual(mode, 0o600)


if __name__ == '__main__':
    unittest.main()
