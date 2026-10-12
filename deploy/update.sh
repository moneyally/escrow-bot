#!/usr/bin/env bash
# 새 코드 받기: bash /opt/escrow/deploy/update.sh  (DB 표는 각 프로세스가 켜질 때 자동으로 맞춤)
set -euo pipefail
APP=/opt/escrow
cd "$APP"
sudo -u escrow git pull --ff-only
sudo -u escrow "$APP/.venv/bin/pip" install -q -r requirements.txt
cp deploy/escrow-*.service /etc/systemd/system/ && systemctl daemon-reload
systemctl restart escrow-bot escrow-watcher escrow-admin
sleep 3
systemctl --no-pager --lines=0 status escrow-bot escrow-watcher escrow-admin | grep -E "●|Active"
