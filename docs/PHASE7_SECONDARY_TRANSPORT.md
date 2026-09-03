# Phase 7 secondary execution transport — research only

Status: `AUTHENTICATED_TESTING_NOT_APPROVED`; authority=false.

Current WStrade live executor submits through `api.new_order(...)` with a stable
`newClientOrderId`. When the transport wrapper returns the timeout/unknown code
used by the executor (`599`), the executor queries the same client order ID
before deciding what happened. Phase 7 preserves that invariant and does not
modify the active executor.

Official Binance USDⓈ-M interfaces checked for this preparation:

- REST new order: `POST /fapi/v1/order` on `https://fapi.binance.com`.
- Authenticated WebSocket API new order: `order.place` on
  `wss://ws-fapi.binance.com/ws-fapi/v1`.
- WebSocket `order.status` can query an order by exchange/client identity.
- User-data streams deliver account/order state updates; they are not the same
  order-submit transport as `order.place`.
- `newClientOrderId` is supported by the USDⓈ-M WebSocket order API and must be
  reused across any reconciled fallback attempt.

Contract:

1. One canonical `client_order_id` belongs to one logical intent.
2. A submit result is only `ACK`, `REJECT`, or `UNKNOWN`.
3. `UNKNOWN` means reconcile exchange state before any second submit.
4. Primary and fallback must never submit concurrently.
5. Fallback may occur only after exchange state is verified `NOT_FOUND`, and it
   must use the same logical order identity.
6. A primary timeout does not prove the order failed to reach Binance.
7. Transport health has no Market Truth, Bias, Entry-direction, Guardian, or
   Hard Risk authority.

REST and WebSocket API are both Binance control-plane paths. They are therefore
not automatically independent failure domains: common authentication, account,
matching-engine, or Binance control-plane incidents may break both.

Do not promote the secondary transport until authenticated latency benchmarks,
correlated-failure measurements, idempotency proof, timeout/reconcile tests,
exchange-state recovery proof, and explicit user approval for authenticated
testing exist. No API key or real order is used in Phase 7 preparation.
