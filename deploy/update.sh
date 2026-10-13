#!/usr/bin/env bash
# 새 코드 자동 반영 (escrow-update.timer 가 2분마다 root 로 실행). 손으로: bash /opt/escrow/deploy/update.sh
#   1) GitHub main 을 받아 봄 (배포 키 SSH → 안 되면 HTTPS: 저장소가 공개일 때만 됨)
#   2) 이미 반영된 버전이면 끝
#   3) 새 코드를 따로 풀어서 '불러오기 검사' (문법·import 오류면 반영 안 함)
#   4) /opt/escrow 에 덮어쓰기 (.env·.venv·백업은 그대로) → 패키지 바뀌었으면 설치 → 서비스 재시작 → 켜졌는지 확인
#   실패하면 이전 코드로 되돌리고 관리자에게 텔레그램 알림.
set -euo pipefail
APP=/opt/escrow
SRC=/opt/escrow-src
KEY=/root/.ssh/escrow_deploy
REPO_SSH=${REPO_SSH:-git@github.com:moneyally/escrow-bot.git}
REPO_HTTPS=${REPO_HTTPS:-https://github.com/moneyally/escrow-bot.git}
BRANCH=${BRANCH:-main}
LOG=/var/log/escrow-update.log
exec 9>/run/escrow-update.lock
flock -n 9 || exit 0

say() {   # 관리자들에게 텔레그램 (봇 토큰·관리자 ID 는 .env 에서)
  local tok ids
  tok=$(grep -E '^BOT_TOKEN=' "$APP/.env" | cut -d= -f2- || true)
  ids=$(grep -E '^ADMIN_IDS=' "$APP/.env" | cut -d= -f2- | tr ',' ' ' || true)
  [ -n "$tok" ] || return 0
  for id in $ids; do
    curl -s -m 10 "https://api.telegram.org/bot$tok/sendMessage" --data-urlencode "chat_id=$id" --data-urlencode "text=$1" >/dev/null || true
  done
}

[ -f "$KEY" ] || { install -d -m 700 /root/.ssh; ssh-keygen -q -t ed25519 -N "" -C "escrow-server-deploy" -f "$KEY"; }
[ -d "$SRC/.git" ] || git init -q "$SRC"
export GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15"
if ! git -C "$SRC" fetch -q --depth 1 "$REPO_SSH" "$BRANCH" 2>/dev/null; then
  git -C "$SRC" fetch -q --depth 1 "$REPO_HTTPS" "$BRANCH" 2>>"$LOG" || { echo "$(date -Is) 받기 실패 (배포 키 미등록 + 비공개 저장소?)" >>"$LOG"; exit 0; }
fi
NEW=$(git -C "$SRC" rev-parse FETCH_HEAD)
OLD=$(cat "$APP/.deployed_rev" 2>/dev/null || echo none)
[ "$NEW" = "$OLD" ] && exit 0
echo "$(date -Is) $OLD → $NEW" >>"$LOG"

STAGE=$(mktemp -d /opt/escrow-stage.XXXX)
BACK=$(mktemp -d /opt/escrow-prev.XXXX)
trap 'rm -rf "$STAGE"' EXIT
git -C "$SRC" archive FETCH_HEAD | tar -x -C "$STAGE"
# 불러오기 검사: 새 코드가 지금 venv 로 import 되는지 (패키지가 바뀌면 설치 뒤 다시 검사)
check() { (cd "$1" && sudo -u escrow env HOME="$APP" "$APP/.venv/bin/python" -c "import escrow.bot, escrow.watcher, escrow.admin.app, escrow.backup") >>"$LOG" 2>&1; }
REQ_CHANGED=0
cmp -s "$STAGE/requirements.txt" "$APP/requirements.txt" || REQ_CHANGED=1
chown -R escrow: "$STAGE"
if [ "$REQ_CHANGED" = 1 ]; then sudo -u escrow "$APP/.venv/bin/pip" install -q -r "$STAGE/requirements.txt" >>"$LOG" 2>&1; fi
if ! check "$STAGE"; then
  say "🛠 서버 갱신 안 함: 새 코드를 불러오지 못했어요 (${NEW:0:7}). 이전 버전 그대로 돌아가요."
  echo "$NEW" >"$APP/.failed_rev"; exit 1
fi

# 반영 (되돌릴 수 있게 지금 코드 보관)
rsync -a --delete --exclude .env --exclude .venv --exclude backups --exclude .deployed_rev --exclude .failed_rev "$APP/" "$BACK/"
rsync -a --delete --exclude .env --exclude .venv --exclude backups --exclude .deployed_rev --exclude .failed_rev "$STAGE/" "$APP/"
chown -R escrow: "$APP"; chmod 600 "$APP/.env"
cp "$APP"/deploy/escrow-*.service "$APP"/deploy/escrow-*.timer /etc/systemd/system/ 2>/dev/null || true
systemctl daemon-reload
# 원격 점검 창구(관리 페이지 프로세스)가 서비스 로그를 읽을 수 있게 (읽기 전용 그룹)
id -nG escrow | grep -qw systemd-journal || usermod -aG systemd-journal escrow
SERVICES="escrow-admin escrow-watcher"
grep -qE '^BOT_TOKEN=.+' "$APP/.env" && SERVICES="$SERVICES escrow-bot"
systemctl restart $SERVICES
sleep 8
if ! systemctl is-active -q $SERVICES; then
  rsync -a --delete --exclude .env --exclude .venv --exclude backups --exclude .deployed_rev --exclude .failed_rev "$BACK/" "$APP/"
  chown -R escrow: "$APP"; systemctl restart $SERVICES
  say "🛠 서버 갱신 실패: 새 버전(${NEW:0:7})이 켜지지 않아 이전 버전으로 되돌렸어요."
  echo "$NEW" >"$APP/.failed_rev"; rm -rf "$BACK"; exit 1
fi
rm -rf "$BACK"
echo "$NEW" >"$APP/.deployed_rev"
rm -f "$APP/.failed_rev"
# 알림 문구: 커밋 메시지의 '알림:' 줄(한국어 요약)만 보냄. 없으면 버전만.
NOTE=$(git -C "$SRC" log -1 --format=%B FETCH_HEAD 2>/dev/null | grep -m1 '^알림:' | cut -c1-200 | sed 's/^알림: *//' || true)
say "✅ 서버 갱신 끝${NOTE:+: $NOTE} (${NEW:0:7})"
# 첫 설치용 부트 통로가 닫혔으면 남은 파일 정리 (1회용 토큰 포함)
if ! systemctl is-active -q escrow-boot 2>/dev/null; then rm -f /root/boot.token /root/boot.py /root/boot.sh; fi
