#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "Run as root: sudo /home/ubuntu/WStrade/ops/activate_direct_live.sh" >&2
  exit 2
fi

credential_dir=/etc/wstrade/credentials
install -d -m 0700 -o root -g root "$credential_dir"

read -r -s -p "Binance Futures Mainnet API key: " api_key
echo
read -r -s -p "Binance Futures Mainnet API secret: " api_secret
echo

if [[ -z "$api_key" || -z "$api_secret" ]]; then
  unset api_key api_secret
  echo "Both key and secret are required; nothing started." >&2
  exit 2
fi

umask 077
printf '%s\n' "$api_key" > "$credential_dir/binance_api_key"
printf '%s\n' "$api_secret" > "$credential_dir/binance_api_secret"
unset api_key api_secret
chown root:root "$credential_dir/binance_api_key" "$credential_dir/binance_api_secret"
chmod 0600 "$credential_dir/binance_api_key" "$credential_dir/binance_api_secret"

systemctl daemon-reload
systemctl enable \
  wstrade-recorder.service wstrade-bot.service wstrade-health.service
# Restart instead of only using --now: AUTO_PROMOTE may already be collecting
# shadow data with empty fallback credentials, and LoadCredential is resolved
# only when the bot process starts.
systemctl restart wstrade-recorder.service wstrade-health.service
systemctl restart wstrade-bot.service

echo "AUTO_PROMOTE enabled and started. Shadow collection begins immediately;"
echo "Mainnet remains gated by replay, 72h soak, CPU, edge and account checks. Check:"
echo "  systemctl status wstrade-bot.service --no-pager"
echo "  journalctl -u wstrade-bot.service -f"
