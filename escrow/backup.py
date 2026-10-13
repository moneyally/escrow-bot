"""매일 DB 백업을 서버 밖(첫 관리자의 텔레그램 1:1)으로: python -m escrow.backup  (cron, postgres 가 아니라 escrow 계정)

- pg_dump → gpg 대칭 암호화(AES256, 비밀번호 = .env BACKUP_PASS) → 봇이 첫 관리자에게 파일로 보냄 (전달·저장 막음)
- BACKUP_PASS 가 없으면 만들어서 .env 에 넣고, 관리자에게 한 번만 알려줌 ('이 비밀번호를 따로 적어 두세요')
- 실패하면 관리자에게 알림. 서버 안 사본(/opt/escrow/backups, 14일)은 install.sh 의 cron 이 따로 만듦.
복구: gpg -d escrow-날짜.dump.gpg > x.dump  →  pg_restore -d escrow x.dump
"""
from __future__ import annotations

import os
import secrets
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import httpx

from . import config as cfgmod

ENV = Path(os.environ.get("ESCROW_ENV", "/opt/escrow/.env"))


def _say(token: str, chat: int, text: str) -> None:
    httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
               data={"chat_id": chat, "text": text, "protect_content": "true"}, timeout=30)


def _ensure_pass(token: str, admin: int) -> str:
    pw = os.environ.get("BACKUP_PASS", "").strip()
    if pw:
        return pw
    pw = secrets.token_urlsafe(24)
    with ENV.open("a", encoding="utf-8") as f:
        f.write(f"\nBACKUP_PASS={pw}\n")
    _say(token, admin, "🔐 매일 보내는 DB 백업 파일의 비밀번호예요. 서버가 통째로 사라졌을 때 복구에 필요하니 따로 적어 두세요.\n\n"
                       f"{pw}\n\n(이 메시지는 한 번만 보내요)")
    return pw


def run() -> int:
    cfg = cfgmod.load(str(ENV))
    if not cfg.bot_token or not cfg.admin_ids:
        print("BOT_TOKEN·ADMIN_IDS 가 없어 건너뜀")
        return 0
    admin = sorted(cfg.admin_ids)[0]
    try:
        pw = _ensure_pass(cfg.bot_token, admin)
        u = urlparse(cfg.database_url)
        env = {**os.environ, "PGPASSWORD": u.password or ""}
        with tempfile.TemporaryDirectory() as tmp:
            dump = Path(tmp) / "db.dump"
            out = Path(tmp) / f"escrow-{date.today()}.dump.gpg"
            subprocess.run(["pg_dump", "-Fc", "-h", u.hostname or "127.0.0.1", "-p", str(u.port or 5432), "-U", u.username or "escrow",
                            "-f", str(dump), (u.path or "/escrow").lstrip("/")], env=env, check=True, capture_output=True, timeout=600)
            subprocess.run(["gpg", "--batch", "--yes", "--symmetric", "--cipher-algo", "AES256", "--pinentry-mode", "loopback",
                            "--passphrase-fd", "0", "-o", str(out), str(dump)], input=pw.encode(), check=True, capture_output=True,
                           timeout=600, env={**os.environ, "GNUPGHOME": tmp})
            with out.open("rb") as f:
                r = httpx.post(f"https://api.telegram.org/bot{cfg.bot_token}/sendDocument",
                               data={"chat_id": admin, "caption": f"DB 백업 {date.today()} (암호화됨)", "protect_content": "true",
                                     "disable_notification": "true"},
                               files={"document": (out.name, f, "application/octet-stream")}, timeout=300)
            if not r.json().get("ok"):
                raise RuntimeError(f"텔레그램 전송 실패: {r.text[:200]}")
        print("백업 보냄")
        return 0
    except Exception as e:   # noqa: BLE001 — 실패는 반드시 알림
        err = e.stderr.decode(errors="replace")[:200] if isinstance(e, subprocess.CalledProcessError) and e.stderr else str(e)[:200]
        try:
            _say(cfg.bot_token, admin, f"🚨 DB 백업 실패: {type(e).__name__} {err}")
        except Exception:   # noqa: BLE001
            pass
        print("백업 실패", e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
