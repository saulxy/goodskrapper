#!/usr/bin/env bash
# GoodSkrapper installer for Raspberry Pi OS Lite (Bookworm / Bullseye)
# Usage:  cd goodSkrapper && bash deploy/install.sh
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="${SUDO_USER:-$USER}"

echo "==> Installing system packages"
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip tzdata

echo "==> Creating virtualenv in $DIR/.venv"
python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install --upgrade pip -q
# piwheels (preconfigured on Raspberry Pi OS) provides prebuilt ARM wheels
"$DIR/.venv/bin/pip" install -r "$DIR/requirements.txt" -q

mkdir -p "$DIR/data"
if [ ! -f "$DIR/config.yaml" ]; then
  cp "$DIR/config.example.yaml" "$DIR/config.yaml"
  chmod 600 "$DIR/config.yaml"
  echo "==> Created $DIR/config.yaml - EDIT IT (bot token, chat id, targets, eBay keys)"
fi
sudo chown -R "$RUN_USER":"$RUN_USER" "$DIR/data" "$DIR/config.yaml"

echo "==> Installing systemd services"
for svc in goodskrapper-worker goodskrapper-dashboard; do
  sed -e "s|__DIR__|$DIR|g" -e "s|__USER__|$RUN_USER|g" "$DIR/deploy/$svc.service" \
    | sudo tee "/etc/systemd/system/$svc.service" > /dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable goodskrapper-worker goodskrapper-dashboard

cat <<EOF

Done. Next steps:
  1. nano $DIR/config.yaml
  2. $DIR/.venv/bin/python -m goodskrapper telegram-chat-id   # find your channel id
  3. $DIR/.venv/bin/python -m goodskrapper telegram-test
  4. sudo systemctl start goodskrapper-worker goodskrapper-dashboard
  5. Open http://$(hostname -I | awk '{print $1}'):8080
Logs:  journalctl -u goodskrapper-worker -f
EOF
