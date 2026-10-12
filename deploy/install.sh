#!/usr/bin/env bash
# 헤츠너 새 서버(Ubuntu 24.04)에 한 번: bash install.sh admin.도메인.com
#   PostgreSQL 16 · 파이썬 venv · systemd 3개(봇·감시·관리 페이지) · Caddy(HTTPS) · 방화벽 · 매일 DB 백업.
#   비밀값은 만들어서 /opt/escrow/.env 에만 (화면에 안 찍음). BOT_TOKEN 등 오너 값은 설치 뒤 직접 넣고 재시작.
set -euo pipefail
DOMAIN=${1:-}
APP=/opt/escrow
REPO=${REPO:-https://github.com/moneyally/escrow-bot.git}
[ "$(id -u)" = 0 ] || { echo "root 로 실행"; exit 1; }
[ -n "$DOMAIN" ] || { echo "사용: bash install.sh admin.도메인 (도메인 없으면 서버IP를 점 대신 - 로: 1-2-3-4.sslip.io)"; exit 1; }

APT="apt-get -o DPkg::Lock::Timeout=600"
$APT update -q
DEBIAN_FRONTEND=noninteractive $APT install -yq postgresql python3-venv python3-pip git ufw debian-keyring \
    debian-archive-keyring apt-transport-https curl
if ! command -v caddy >/dev/null; then
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
  $APT update -q && $APT install -yq caddy
fi

id escrow >/dev/null 2>&1 || useradd --system --home "$APP" --shell /usr/sbin/nologin escrow
# 코드: 이미 올라와 있으면(부트스트랩 업로드) 그대로, 없으면 저장소에서
if [ ! -d "$APP/escrow" ]; then git clone "$REPO" "$APP"; fi
chown -R escrow: "$APP"
sudo -u escrow python3 -m venv "$APP/.venv"
sudo -u escrow "$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"

# DB: 앱 전용 계정 (비번 무작위)
DBPASS=$(openssl rand -hex 24)
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='escrow'" | grep -q 1; then
  sudo -u postgres psql -qc "CREATE ROLE escrow LOGIN PASSWORD '$DBPASS'"
  sudo -u postgres psql -qc "CREATE DATABASE escrow OWNER escrow"
  NEWDB=1
else
  NEWDB=0
fi

if [ ! -f "$APP/.env" ]; then
  cp "$APP/.env.example" "$APP/.env"
  sed -i "s#^DATABASE_URL=.*#DATABASE_URL=postgresql://escrow:$DBPASS@127.0.0.1:5432/escrow#" "$APP/.env"
  sed -i "s#^ADMIN_SECRET=.*#ADMIN_SECRET=$(openssl rand -hex 32)#" "$APP/.env"
  sed -i "s#^ADMIN_BASE_URL=.*#ADMIN_BASE_URL=https://$DOMAIN#" "$APP/.env"
elif [ "$NEWDB" = 1 ]; then
  sed -i "s#^DATABASE_URL=.*#DATABASE_URL=postgresql://escrow:$DBPASS@127.0.0.1:5432/escrow#" "$APP/.env"
fi
# .env 의 비밀번호와 DB 계정 비밀번호를 항상 맞춤 (계정은 있는데 .env 를 새로 만든 경우 등)
CUR=$(grep '^DATABASE_URL=' "$APP/.env" | sed -E 's#.*://escrow:([^@]*)@.*#\1#')
[ -n "$CUR" ] && sudo -u postgres psql -qc "ALTER ROLE escrow PASSWORD '$CUR'"
chown escrow: "$APP/.env"; chmod 600 "$APP/.env"

cp "$APP"/deploy/escrow-*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable -q escrow-bot escrow-watcher escrow-admin
systemctl restart escrow-admin escrow-watcher
# 봇은 BOT_TOKEN 이 있어야 켬 (없으면 계속 재시작만 함)
if grep -qE '^BOT_TOKEN=.+' "$APP/.env"; then systemctl restart escrow-bot; fi

cat > /etc/caddy/Caddyfile <<CADDY
$DOMAIN {
    encode gzip
    header {
        Strict-Transport-Security "max-age=31536000"
        X-Frame-Options "DENY"
        X-Content-Type-Options "nosniff"
        Referrer-Policy "no-referrer"
    }
$( [ -n "${BOOT_ROUTE:-}" ] && printf '    handle_path /_boot/* {  # boot\n        reverse_proxy 127.0.0.1:8099  # boot\n    }  # boot\n' )
    handle {
            reverse_proxy 127.0.0.1:8090
    }
}
CADDY
systemctl reload caddy || systemctl restart caddy

ufw allow OpenSSH >/dev/null; ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null; ufw --force enable >/dev/null

# 매일 04:10 DB 백업, 14일 보관
install -d -o postgres -m 700 /var/backups/escrow
cat > /etc/cron.d/escrow-backup <<'CRON'
10 4 * * * postgres pg_dump -Fc escrow > /var/backups/escrow/escrow-$(date +\%F).dump && find /var/backups/escrow -name '*.dump' -mtime +14 -delete
CRON

echo
echo "설치 끝. 남은 것:"
echo "  1) nano $APP/.env 에서 BOT_TOKEN · ADMIN_IDS · PAY_ADDRESS · BOT_NAME · SUPPORT 채우기"
echo "  2) systemctl restart escrow-bot escrow-watcher escrow-admin"
echo "  3) 관리자 계정으로 봇에 /login → 관리 페이지 https://$DOMAIN"
