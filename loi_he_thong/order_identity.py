"""Forensic-safe Binance client order identities (maximum 36 chars)."""

import hashlib


def client_order_id(
    state, role, opportunity_id=None, setup_id=None, generation=0, nonce=None,
):
    run_id = str(getattr(state, 'run_id', '') or 'missing-run')
    payload = '|'.join((
        run_id, str(opportunity_id or ''), str(setup_id or ''),
        str(int(generation or 0)), str(role or 'ORDER').upper(), str(nonce or ''),
    ))
    digest = hashlib.sha256(payload.encode('utf-8')).hexdigest()[:24]
    tag = ''.join(ch for ch in str(role or 'ord').lower() if ch.isalnum())[:5]
    return f"smc_{tag}_{digest}"[:36]
