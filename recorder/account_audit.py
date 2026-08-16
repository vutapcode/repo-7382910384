"""Signed, read-only Binance Futures Testnet account snapshot for investigations.

This CLI is deliberately separate from the always-on public market recorder.
It reads credentials only at runtime and never serializes their raw values.
"""

import argparse
import hashlib
import json
import os
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from binance.error import ClientError
from binance.um_futures import UMFutures
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = PROJECT_ROOT / '.env'
DEFAULT_OUTPUT = Path(__file__).resolve().parent / 'account_snapshot.json'
DEFAULT_CYCLES = (
    PROJECT_ROOT / '3_thuc_thi' / 'quan_ly_vi_the' / 'nhat_ky' / 'cycles.json'
)
TESTNET_BASE_URL = 'https://testnet.binancefuture.com'
READ_ONLY_ENDPOINTS = (
    'GET /fapi/v2/balance',
    'GET /fapi/v2/positionRisk',
    'GET /fapi/v1/openOrders',
    'GET /fapi/v1/allOrders',
    'GET /fapi/v1/userTrades',
    'GET /fapi/v1/income',
    'GET /fapi/v1/openAlgoOrders',
    'GET /fapi/v1/allAlgoOrders',
)


def _iso(timestamp_ms):
    value = int(timestamp_ms or 0)
    if value <= 0:
        return None
    return datetime.fromtimestamp(value / 1000.0, timezone.utc).isoformat()


def _float(value):
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _fingerprint(value):
    if not value:
        return None
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def credential_reference(env_file, api_key, secret_key):
    """Describe how to authenticate without returning either credential."""
    return {
        'storage': 'dotenv_reference_only',
        'env_file': str(Path(env_file).resolve()),
        'api_key_env': 'BINANCE_API_KEY',
        'secret_key_env': 'BINANCE_API_SECRET',
        'api_key_present': bool(api_key),
        'secret_key_present': bool(secret_key),
        'api_key_sha256_prefix': _fingerprint(api_key),
        'raw_credentials_embedded': False,
    }


def _safe(label, func, *args, **kwargs):
    try:
        return func(*args, **kwargs), None
    except ClientError as exc:
        return None, {
            'endpoint': label,
            'http_status': exc.status_code,
            'code': exc.error_code,
            'message': exc.error_message,
        }
    except Exception as exc:
        return None, {'endpoint': label, 'message': str(exc)}


def fetch_account_data(client, symbol, limit):
    """Call only signed GET endpoints and return their raw response objects."""
    calls = {
        'balances': (client.balance, (), {}),
        'positions': (client.get_position_risk, (), {'symbol': symbol}),
        'open_orders': (client.get_orders, (), {'symbol': symbol}),
        'all_orders': (client.get_all_orders, (symbol,), {'limit': limit}),
        'trades': (client.get_account_trades, (symbol,), {'limit': min(limit, 1000)}),
        'income': (
            client.get_income_history, (), {'symbol': symbol, 'limit': min(limit, 1000)}
        ),
        'open_algos': (
            client.sign_request,
            ('GET', '/fapi/v1/openAlgoOrders', {'symbol': symbol}),
            {},
        ),
        'all_algos': (
            client.sign_request,
            ('GET', '/fapi/v1/allAlgoOrders', {'symbol': symbol, 'limit': min(limit, 100)}),
            {},
        ),
    }
    data = {}
    errors = []
    for label, (func, args, kwargs) in calls.items():
        result, error = _safe(label, func, *args, **kwargs)
        data[label] = result
        if error:
            errors.append(error)
    data['errors'] = errors
    return data


def _items(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ('orders', 'rows', 'data'):
            if isinstance(value.get(key), list):
                return value[key]
    return []


def _pick(item, fields):
    return {field: item.get(field) for field in fields if field in item}


def _group_trade_orders(trades):
    grouped = {}
    for trade in sorted(trades, key=lambda item: int(item.get('time', 0) or 0)):
        order_id = str(trade.get('orderId'))
        group = grouped.setdefault(order_id, {
            'order_id': trade.get('orderId'),
            'side': trade.get('side'),
            'position_side': trade.get('positionSide'),
            'time_ms': int(trade.get('time', 0) or 0),
            'quantity': 0.0,
            'quote_quantity': 0.0,
            'realized_pnl': 0.0,
            'commission': 0.0,
            'fill_count': 0,
        })
        group['time_ms'] = max(group['time_ms'], int(trade.get('time', 0) or 0))
        group['quantity'] += _float(trade.get('qty'))
        group['quote_quantity'] += _float(trade.get('quoteQty'))
        group['realized_pnl'] += _float(trade.get('realizedPnl'))
        group['commission'] += _float(trade.get('commission'))
        group['fill_count'] += 1
    for group in grouped.values():
        group['average_price'] = (
            group['quote_quantity'] / group['quantity'] if group['quantity'] else 0.0
        )
        group['time_utc'] = _iso(group['time_ms'])
    return sorted(grouped.values(), key=lambda item: item['time_ms'])


def _cycle_index(cycles_path):
    path = Path(cycles_path)
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    cycles = payload.get('cycles', []) if isinstance(payload, dict) else []
    index = {}
    for cycle in cycles:
        for order in cycle.get('actual', {}).get('orders', []):
            order_id = order.get('order_id')
            if order_id is not None:
                index[str(order_id)] = cycle
    return index


def derive_round_trips(trades, orders, cycles_path):
    """Pair sequential hedge-mode entry/close groups and attach bot reasons."""
    order_lookup = {str(item.get('orderId')): item for item in orders}
    cycle_lookup = _cycle_index(cycles_path)
    open_entry = {}
    round_trips = []
    for group in _group_trade_orders(trades):
        position_side = group['position_side']
        side = group['side']
        is_entry = (
            (position_side == 'LONG' and side == 'BUY')
            or (position_side == 'SHORT' and side == 'SELL')
        ) and abs(group['realized_pnl']) < 1e-12
        if is_entry:
            open_entry[position_side] = group
            continue
        is_close = (
            (position_side == 'LONG' and side == 'SELL')
            or (position_side == 'SHORT' and side == 'BUY')
            or abs(group['realized_pnl']) >= 1e-12
        )
        if not is_close:
            continue
        entry = open_entry.pop(position_side, None)
        if entry is None:
            # Keep probe/manual closes in raw orders and trades, but do not
            # mislabel them as a complete strategy round trip.
            continue
        cycle = cycle_lookup.get(str(group['order_id']), {})
        exit_order = order_lookup.get(str(group['order_id']), {})
        entry_order = order_lookup.get(str(entry['order_id']), {}) if entry else {}
        entry_commission = entry['commission'] if entry else 0.0
        total_fees = entry_commission + group['commission']
        net_pnl = group['realized_pnl'] - total_fees
        holding_ms = group['time_ms'] - entry['time_ms']
        round_trips.append({
            'position_cycle_id': cycle.get('position_cycle_id'),
            'setup_id': cycle.get('setup_id'),
            'mode': cycle.get('mode'),
            'position_side': position_side,
            'entry_order_id': entry['order_id'],
            'entry_client_order_id': entry_order.get('clientOrderId'),
            'entry_time_ms': entry['time_ms'],
            'entry_time_utc': entry.get('time_utc'),
            'entry_average_price': entry.get('average_price'),
            'exit_order_id': group['order_id'],
            'exit_client_order_id': exit_order.get('clientOrderId'),
            'exit_time_ms': group['time_ms'],
            'exit_time_utc': group['time_utc'],
            'exit_average_price': group['average_price'],
            'quantity': group['quantity'],
            'holding_time_ms': holding_ms,
            'exit_reason': cycle.get('exit_reason'),
            'gross_realized_pnl': group['realized_pnl'],
            'entry_commission': entry_commission,
            'exit_commission': group['commission'],
            'total_fees': total_fees,
            'net_pnl_after_fees': net_pnl,
            'fill_count': entry.get('fill_count', 0) + group['fill_count'],
        })
    return round_trips


def build_snapshot(raw, symbol, env_file, api_key, secret_key, cycles_path):
    order_fields = (
        'orderId', 'clientOrderId', 'side', 'positionSide', 'type', 'status',
        'origQty', 'executedQty', 'avgPrice', 'stopPrice', 'reduceOnly',
        'closePosition', 'time', 'updateTime',
    )
    trade_fields = (
        'id', 'orderId', 'side', 'positionSide', 'price', 'qty', 'quoteQty',
        'realizedPnl', 'commission', 'commissionAsset', 'maker', 'time',
    )
    income_fields = (
        'symbol', 'incomeType', 'income', 'asset', 'time', 'tranId', 'tradeId',
    )
    algo_fields = (
        'algoId', 'clientAlgoId', 'algoStatus', 'orderType', 'side',
        'positionSide', 'triggerPrice', 'actualOrderId', 'actualPrice',
        'createTime', 'triggerTime', 'updateTime',
    )
    orders = sorted(
        _items(raw.get('all_orders')),
        key=lambda item: int(item.get('updateTime', item.get('time', 0)) or 0),
    )
    trades = sorted(
        _items(raw.get('trades')), key=lambda item: int(item.get('time', 0) or 0)
    )
    income = sorted(
        _items(raw.get('income')), key=lambda item: int(item.get('time', 0) or 0)
    )
    algos = sorted(
        _items(raw.get('all_algos')),
        key=lambda item: int(item.get('updateTime', item.get('createTime', 0)) or 0),
    )
    positions = _items(raw.get('positions'))
    balances = [
        item for item in _items(raw.get('balances'))
        if item.get('asset') == 'USDT' or abs(_float(item.get('balance'))) > 0
    ]
    round_trips = derive_round_trips(trades, orders, cycles_path)
    trade_groups = _group_trade_orders(trades)
    close_group_count = sum(
        1 for group in trade_groups
        if (
            (group['position_side'] == 'LONG' and group['side'] == 'SELL')
            or (group['position_side'] == 'SHORT' and group['side'] == 'BUY')
            or abs(group['realized_pnl']) >= 1e-12
        )
    )
    recent_round_trips = round_trips[-20:]
    return {
        'schema_version': 1,
        'snapshot_kind': 'BINANCE_FUTURES_TESTNET_ACCOUNT_AUDIT',
        'generated_at_ms': int(time.time() * 1000),
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'venue': 'BINANCE_FUTURES_TESTNET',
        'base_url': TESTNET_BASE_URL,
        'symbol': symbol,
        'access': {
            'mode': 'signed_user_data_read_only',
            'credential_reference': credential_reference(
                env_file, api_key, secret_key
            ),
            'endpoints': list(READ_ONLY_ENDPOINTS),
            'write_endpoint_called': False,
            'refresh_command': 'python3 -m recorder.account_audit',
        },
        'errors': list(raw.get('errors', [])),
        'summary': {
            'orders_returned': len(orders),
            'trades_returned': len(trades),
            'income_rows_returned': len(income),
            'order_statuses': dict(Counter(str(x.get('status')) for x in orders)),
            'active_positions': sum(
                1 for item in positions
                if abs(_float(item.get('positionAmt'))) > 0
            ),
            'open_orders': len(_items(raw.get('open_orders'))),
            'open_algos': len(_items(raw.get('open_algos'))),
            'round_trips_derived': len(round_trips),
            'unmatched_close_groups': max(0, close_group_count - len(round_trips)),
            'recent_round_trips_net_pnl': sum(
                item['net_pnl_after_fees'] for item in recent_round_trips
            ),
            'latest_trade_time_ms': int(trades[-1].get('time', 0) or 0) if trades else None,
            'latest_trade_time_utc': _iso(trades[-1].get('time')) if trades else None,
        },
        'balances': [
            _pick(item, ('asset', 'balance', 'availableBalance', 'crossWalletBalance',
                         'crossUnPnl', 'maxWithdrawAmount'))
            for item in balances
        ],
        'positions': [
            _pick(item, ('symbol', 'positionSide', 'positionAmt', 'entryPrice',
                         'breakEvenPrice', 'markPrice', 'unRealizedProfit',
                         'liquidationPrice', 'leverage', 'marginType', 'updateTime'))
            for item in positions
        ],
        'recent_round_trips': recent_round_trips,
        'recent_orders': [_pick(item, order_fields) for item in orders[-50:]],
        'recent_trades': [_pick(item, trade_fields) for item in trades[-100:]],
        'recent_income': [_pick(item, income_fields) for item in income[-100:]],
        'recent_algos': [_pick(item, algo_fields) for item in algos[-50:]],
    }


def write_snapshot(path, payload):
    """Atomically replace a private offline JSON snapshot."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f'{target.stem}_', suffix='.tmp', dir=target.parent
    )
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, target)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def parse_args():
    parser = argparse.ArgumentParser(
        description='Write a sanitized signed-account Testnet audit snapshot.'
    )
    parser.add_argument('--env-file', type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--cycles', type=Path, default=DEFAULT_CYCLES)
    parser.add_argument('--symbol', default='BTCUSDT')
    parser.add_argument('--limit', type=int, default=1000)
    return parser.parse_args()


def main():
    args = parse_args()
    load_dotenv(args.env_file)
    api_key = os.getenv('BINANCE_API_KEY', '')
    secret_key = os.getenv('BINANCE_API_SECRET', '')
    if not api_key or not secret_key:
        raise SystemExit(
            f'Missing BINANCE_API_KEY/BINANCE_API_SECRET in {args.env_file}'
        )
    client = UMFutures(
        key=api_key,
        secret=secret_key,
        base_url=TESTNET_BASE_URL,
        timeout=15,
    )
    raw = fetch_account_data(client, args.symbol.upper(), max(1, args.limit))
    snapshot = build_snapshot(
        raw, args.symbol.upper(), args.env_file, api_key, secret_key, args.cycles
    )
    write_snapshot(args.output, snapshot)
    print(json.dumps({
        'output': str(args.output.resolve()),
        'mode': snapshot['access']['mode'],
        'errors': len(snapshot['errors']),
        **snapshot['summary'],
    }, ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    main()
