#!/usr/bin/env bash
set -euo pipefail

repo=/home/ubuntu/WStrade
units=/etc/systemd/system

# ProtectHome/ReadWritePaths namespaces are assembled before ExecStartPre runs,
# so every writable bind target must already exist at unit start.
install -d -m 0700 -o ubuntu -g ubuntu \
  /home/ubuntu/.local/state/wstrade \
  /home/ubuntu/.local/state/smc2026/runtime \
  /home/ubuntu/.local/state/smc2026/mainnet_shadow \
  /home/ubuntu/smc2026_data/health \
  /home/ubuntu/wstrade_trade_log

# The legacy unit points at /home/ubuntu/SMC2026 and must not coexist with WStrade.
systemctl stop smc2026-bot.service smc2026-recorder.service smc2026-health.service 2>/dev/null || true
systemctl disable smc2026-bot.service smc2026-recorder.service smc2026-health.service 2>/dev/null || true
ubuntu_uid=$(id -u ubuntu)
if [[ -S "/run/user/$ubuntu_uid/bus" ]]; then
  runuser -u ubuntu -- env XDG_RUNTIME_DIR="/run/user/$ubuntu_uid" \
    systemctl --user stop smc2026-bot.service smc2026-recorder.service smc2026-health.service 2>/dev/null || true
  runuser -u ubuntu -- env XDG_RUNTIME_DIR="/run/user/$ubuntu_uid" \
    systemctl --user disable smc2026-bot.service smc2026-recorder.service smc2026-health.service 2>/dev/null || true
fi

install -m 0644 "$repo/ops/systemd/wstrade-bot.service" "$units/wstrade-bot.service"
install -m 0644 "$repo/ops/systemd/wstrade-recorder.service" "$units/wstrade-recorder.service"
install -m 0644 "$repo/ops/systemd/wstrade-health.service" "$units/wstrade-health.service"
install -m 0644 "$repo/ops/systemd/wstrade-trade-audit.service" "$units/wstrade-trade-audit.service"
systemctl daemon-reload

echo "AUTO_PROMOTE units installed but not enabled or started. Activate with:"
echo "  sudo $repo/ops/activate_direct_live.sh"
