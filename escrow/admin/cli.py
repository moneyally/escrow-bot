"""서버에서 직접: python -m escrow.admin.cli login <관리자 텔레그램 ID>
봇이 꺼져 있을 때 관리 페이지 1회용 로그인 링크(5분)를 만든다. ADMIN_IDS 에 있는 사람만."""
from __future__ import annotations

import asyncio
import hashlib
import secrets
import sys

from .. import config as cfgmod
from ..db import DB


async def make_link(db: DB, base_url: str, tg_id: int) -> str:
    token = secrets.token_urlsafe(32)
    await db.execute("INSERT INTO admin_logins(token_hash, tg_id, expires_at) VALUES($1,$2, now() + interval '5 minutes')",
                     hashlib.sha256(token.encode()).hexdigest(), tg_id)
    return f"{base_url}/login?t={token}"


async def _main(args: list[str]) -> None:
    cfg = cfgmod.load()
    if len(args) != 2 or args[0] != "login" or not args[1].isdigit():
        raise SystemExit("사용법: python -m escrow.admin.cli login <관리자 텔레그램 ID>")
    tg_id = int(args[1])
    if tg_id not in cfg.admin_ids:
        raise SystemExit("ADMIN_IDS 에 없는 ID 예요 (.env 먼저)")
    db = await DB.connect(cfg.database_url, max_size=1)
    try:
        print(await make_link(db, cfg.admin_base_url, tg_id))
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(_main(sys.argv[1:]))
