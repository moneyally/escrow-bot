"""🔌 원격 점검 창구 (읽기 전용) — 클로드 작업 환경이 서버 상태를 보는 유일한 길. 쓰기·실행 경로 없음.

- 주소: https://<관리 페이지>/diag/v1/<route>?k=v   헤더 Authorization: Bearer <토큰>
- 토큰: 관리자가 봇 1:1 에 /diag → 새 토큰(이전 것은 무효). 서버엔 sha256 만 저장 (kv 'diag_token').
  오너가 그 토큰을 클로드 환경 비밀값 ESCROW_DIAG_TOKEN 에 넣음 → tools/diag.py 로 조회.
- 출력의 비밀값 모양(봇 토큰·32자 hex 등)은 [가림].
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from .. import core, helper

SECRET_RE = re.compile(r"(?<!\d)\d{6,12}:[A-Za-z0-9_-]{30,}|\b[0-9a-f]{32}\b|postgresql://[^\s\"']+|BACKUP_PASS=\S+|api_hash\S*")
UNITS = ("escrow-bot", "escrow-watcher", "escrow-admin", "escrow-update", "postgresql", "caddy")
APP_DIR = Path(__file__).resolve().parents[2]
_hits: dict[str, list[float]] = {}


def redact(v):
    if isinstance(v, str):
        return SECRET_RE.sub("[가림]", v)
    if isinstance(v, dict):
        return {k: redact(x) for k, x in v.items() if k not in ("session", "api_hash")}
    if isinstance(v, list):
        return [redact(x) for x in v]
    return v


def _rows(rows) -> list[dict]:
    return [{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in dict(r).items()} for r in rows]


async def _sh(*cmd: str) -> str:
    p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await asyncio.wait_for(p.communicate(), 20)
    return out.decode(errors="replace")


def router(db) -> APIRouter:
    r = APIRouter()

    async def auth(request: Request):
        ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0].strip()
        now = time.time()
        hits = [t for t in _hits.get(ip, []) if now - t < 60][-120:]
        _hits[ip] = hits + [now]
        if len(hits) >= 120:
            raise HTTPException(429, "너무 자주")
        want = await db().fetchval("SELECT value->>'sha256' FROM kv WHERE key='diag_token'")
        got = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not want or not got or not hmac.compare_digest(hashlib.sha256(got.encode()).hexdigest(), want):
            raise HTTPException(401, "토큰 없음/틀림 — 봇 1:1 에 /diag")

    @r.get("/diag/v1/{route}")
    async def handle(route: str, request: Request):
        await auth(request)
        q = dict(request.query_params)
        fn = ROUTES.get(route)
        if not fn:
            raise HTTPException(404, f"없는 경로. 가능: {', '.join(ROUTES)}")
        return redact(await fn(db(), q))

    return r


def _int(q: dict, k: str, d: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(q.get(k, d))))
    except ValueError:
        return d


async def r_health(db, q):
    act = (await _sh("systemctl", "is-active", *UNITS)).split()
    kv = {x["key"]: x["value"] for x in await db.fetch(
        "SELECT key, value FROM kv WHERE key IN ('watch_alive','watch_cursor_ms','wallet_balance','daily_report_at','watch_alarm_at')")}
    async with db.tx() as c:
        st = await core.stats(c, (kv.get("wallet_balance") or {}).get("micro"))
    rev = (APP_DIR / ".deployed_rev").read_text().strip() if (APP_DIR / ".deployed_rev").exists() else None
    failed = (APP_DIR / ".failed_rev").read_text().strip() if (APP_DIR / ".failed_rev").exists() else None
    alive = kv.get("watch_alive")
    return {"version": rev, "failed_version": failed, "units": dict(zip(UNITS, act)),
            "watcher_age_s": (int(time.time()) - int(alive)) if alive else None, "kv": kv, "stats": st,
            "helper": helper.public(await helper.load(db))}


async def r_deals(db, q):
    lim = _int(q, "limit", 30, 1, 200)
    rows = await db.fetch("SELECT id, code, state, creator, partner, room_id, fee_micro, close_reason, created_at, updated_at "
                          "FROM deals WHERE ($1 = '' OR state = $1) ORDER BY id DESC LIMIT $2", q.get("state", ""), lim)
    return {"deals": _rows(rows)}


async def r_deal(db, q):
    d = await db.fetchrow("SELECT * FROM deals WHERE code=$1", (q.get("code") or "").upper())
    if not d:
        raise HTTPException(404, "그 거래코드 없음")
    i = d["id"]
    return {"deal": _rows([d])[0],
            "parties": _rows(await db.fetch("SELECT * FROM deal_parties WHERE deal_id=$1", i)),
            "slots": _rows(await db.fetch("SELECT * FROM deposit_slots WHERE deal_id=$1 ORDER BY id", i)),
            "payouts": _rows(await db.fetch("SELECT * FROM payouts WHERE deal_id=$1 ORDER BY id", i)),
            "approvals": _rows(await db.fetch("SELECT * FROM approvals WHERE deal_id=$1 ORDER BY id", i)),
            "events": _rows(await db.fetch("SELECT * FROM events WHERE deal_id=$1 ORDER BY id", i)),
            "outbox": _rows(await db.fetch("SELECT id, kind, chat_id, left(text, 120) AS text, sent_at, attempts, error FROM outbox "
                                           "WHERE deal_id=$1 ORDER BY id", i))}


async def r_outbox(db, q):
    rows = await db.fetch("SELECT id, kind, chat_id, deal_id, left(text, 120) AS text, attempts, error, created_at, sent_at FROM outbox "
                          "WHERE error IS NOT NULL OR sent_at IS NULL ORDER BY id DESC LIMIT $1", _int(q, "limit", 50, 1, 300))
    return {"problems": _rows(rows)}


async def r_events(db, q):
    rows = await db.fetch("SELECT * FROM events WHERE ($1 = '' OR kind = $1) ORDER BY id DESC LIMIT $2",
                          q.get("kind", ""), _int(q, "limit", 50, 1, 500))
    return {"events": _rows(rows)}


async def r_rooms(db, q):
    return {"rooms": _rows(await db.fetch("SELECT chat_id, title, state, deal_id, note, updated_at FROM rooms ORDER BY updated_at DESC"))}


async def r_chain(db, q):
    rows = await db.fetch("SELECT * FROM chain_tx ORDER BY block_ts DESC LIMIT $1", _int(q, "limit", 50, 1, 300))
    return {"chain_tx": _rows(rows)}


async def r_settings(db, q):
    async with db.tx() as c:
        return {"settings": await core.settings(c)}


async def r_logs(db, q):
    unit = q.get("unit", "escrow-bot")
    if unit not in UNITS:
        raise HTTPException(400, f"unit: {', '.join(UNITS)}")
    cmd = ["journalctl", "-u", unit, "--no-pager", "-o", "short-iso", "-n", str(_int(q, "lines", 200, 1, 2000))]
    if q.get("since"):
        cmd += ["--since", q["since"][:40]]
    out = await _sh(*cmd)
    if q.get("grep"):
        out = "\n".join(line for line in out.splitlines() if q["grep"] in line)
    return {"unit": unit, "log": out[-60000:]}


async def r_tables(db, q):
    rows = await db.fetch("SELECT relname AS table, n_live_tup AS rows FROM pg_stat_user_tables ORDER BY relname")
    return {"tables": _rows(rows)}


ROUTES = {"health": r_health, "deals": r_deals, "deal": r_deal, "outbox": r_outbox, "events": r_events, "rooms": r_rooms,
          "chain": r_chain, "settings": r_settings, "logs": r_logs, "tables": r_tables}


def new_token() -> tuple[str, dict]:
    import secrets
    tok = "esc_" + secrets.token_urlsafe(32)
    return tok, {"sha256": hashlib.sha256(tok.encode()).hexdigest(), "at": int(time.time())}


__all__ = ["router", "new_token", "redact", "json"]
