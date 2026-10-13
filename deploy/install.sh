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
# 앞에서 반쯤 설치된 패키지가 있으면 먼저 마무리 (첫 부팅 자동 업데이트와 겹친 경우 등)
DEBIAN_FRONTEND=noninteractive dpkg --configure -a 2>&1 | tail -n 40 || true
$APT update -q
DEBIAN_FRONTEND=noninteractive $APT install -yq postgresql python3-venv python3-pip git ufw debian-keyring \
    debian-archive-keyring apt-transport-https curl rsync gnupg fail2ban
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

cp "$APP"/deploy/escrow-*.service "$APP"/deploy/escrow-*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable -q escrow-watcher escrow-admin
systemctl restart escrow-admin escrow-watcher
# 봇은 BOT_TOKEN 이 있어야 켬 (없이 켜 두면 재부팅 때마다 재시작만 반복)
if grep -qE '^BOT_TOKEN=.+' "$APP/.env"; then systemctl enable -q escrow-bot; systemctl restart escrow-bot; else systemctl disable -q escrow-bot || true; fi
# 새 코드 자동 반영 (GitHub main → 2분마다 확인). 배포 키가 없으면 만들어 둠 (저장소를 비공개로 바꾸면 GitHub 에 등록)
chmod +x "$APP/deploy/update.sh"
[ -f /root/.ssh/escrow_deploy ] || { install -d -m 700 /root/.ssh; ssh-keygen -q -t ed25519 -N "" -C "escrow-server-deploy" -f /root/.ssh/escrow_deploy; }
[ -f "$APP/.deployed_rev" ] || echo "${DEPLOYED_REV:-unknown}" > "$APP/.deployed_rev"
systemctl enable -q --now escrow-update.timer
echo "배포 키(공개): $(cat /root/.ssh/escrow_deploy.pub)"

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

ufw limit OpenSSH >/dev/null; ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null; ufw --force enable >/dev/null
# SSH: 비밀번호 로그인 끔 (root 비밀번호가 메일로 나갔음 — 키로만. 급할 땐 헤츠너 웹 콘솔은 그대로 됨) + 무차별 대입 차단
cat > /etc/ssh/sshd_config.d/10-escrow.conf <<'SSH'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
SSH
systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
systemctl enable -q --now fail2ban || true

# 매일 04:10 DB 백업, 14일 보관
install -d -o postgres -m 700 /var/backups/escrow
cat > /etc/cron.d/escrow-backup <<'CRON'
10 4 * * * postgres pg_dump -Fc escrow > /var/backups/escrow/escrow-$(date +\%F).dump && find /var/backups/escrow -name '*.dump' -mtime +14 -delete
# 서버 밖 사본: 암호화해서 첫 관리자 텔레그램으로 (실패하면 알림)
30 4 * * * escrow cd /opt/escrow && /opt/escrow/.venv/bin/python -m escrow.backup >> /var/log/escrow-backup.log 2>&1
CRON
touch /var/log/escrow-backup.log && chown escrow: /var/log/escrow-backup.log

echo
echo "설치 끝. 남은 것:"
echo "  1) nano $APP/.env 에서 BOT_TOKEN · ADMIN_IDS · PAY_ADDRESS · BOT_NAME · SUPPORT 채우기"
echo "  2) systemctl restart escrow-bot escrow-watcher escrow-admin"
echo "  3) 관리자 계정으로 봇에 /login → 관리 페이지 https://$DOMAIN"
