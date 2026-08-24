# WStrade Mainnet credentials

Mainnet credentials are read only from systemd `LoadCredential`. The runtime
ignores `BINANCE_API_KEY` and `BINANCE_API_SECRET` in `.env` on Mainnet.

Create these root-owned files outside the repository:

- `/etc/wstrade/credentials/binance_api_key`
- `/etc/wstrade/credentials/binance_api_secret`

Use directory mode `0700` and file mode `0600`. The key must belong to the
dedicated Futures account, permit Futures trading only, disable withdrawals,
and be restricted to the fixed Lightsail public IP. These Binance-side
permissions cannot be proven from the public market-data API, so verify them in
the Binance API-management UI before installing the service.

The shortest activation path is:

`sudo /home/ubuntu/WStrade/ops/activate_direct_live.sh`

It prompts without echoing the secrets, creates the two root-only files, then
enables and starts recorder, bot and health services.

For Lightsail metric confirmation, configure AWS credentials outside the repo
at `/etc/wstrade/aws_credentials`, then set `AWS_REGION` and
`WSTRADE_LIGHTSAIL_INSTANCE_NAME` in `/home/ubuntu/WStrade/.env`. The AWS
identity needs only Lightsail metric read access plus alarm-management access if
`ops/configure_lightsail_alarms.py` is used.

Never put credential values in the repository, `.env`, chat, journal, backup,
or a systemd unit. The checked-in service uses `AUTO_PROMOTE`: shadow collection
starts immediately, while Mainnet remains blocked until replay, 72-hour soak,
CPU, statistical-edge, credential, dedicated-account and fixed 0.001 BTC
risk/margin checks all pass.
