# Mainnet credentials

SMC2026 reads Mainnet credentials only through systemd `LoadCredential`.
It intentionally ignores `BINANCE_API_KEY` and `BINANCE_API_SECRET` from `.env`
when `SMC_EXECUTION_VENUE=MAINNET`.

Create these two files immediately before the authorized live run:

- `/home/ubuntu/.config/smc2026/credentials/binance_api_key`
- `/home/ubuntu/.config/smc2026/credentials/binance_api_secret`

The directory must be mode `0700`; each file must be mode `0600`, owned by
`ubuntu`.  Values must be from the dedicated Mainnet subaccount key restricted
to Futures trading and the VPS IP, with withdrawals disabled.

Do not put credential values in this repository, `.env`, chat, journal, backup,
or systemd unit.  The bot unit remains disabled with both
`SMC_MAINNET_ARMED=false` and `SMC_ENABLE_TRADING=false` until an explicit run.
