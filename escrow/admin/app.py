"""관리 페이지 (FastAPI): uvicorn escrow.admin.app:app --host 127.0.0.1 --port 8090  (밖으로는 Caddy HTTPS 로만)

로그인: 관리자가 봇에 /login → 1회용 링크(5분) → 서명된 세션 쿠키(12시간, HttpOnly·Secure·SameSite=Strict).
매 요청마다 쿠키 서명 + 관리자 ID(ADMIN_IDS) 다시 확인. 돈이 움직이는 POST 는 core 함수(같은 잠금·같은 규칙·기록)로만.
CSRF: SameSite=Strict 쿠키 + POST 는 'X-Requested-With: escrow' 헤더 필수 (다른 사이트 폼으론 못 붙임).
"""
from __future__ import annotations

import hashlib
import hmac
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import config as cfgmod
from .. import core, helper
from ..db import DB
from ..money import fmt, fmt_exact, parse_usdt

STATIC = Path(__file__).with_name("static")       # 예전 단일 HTML (React 빌드가 없을 때만)
WEB = Path(__file__).with_name("web")             # admin-web(React) 빌드 결과 — npm run build
SESSION_HOURS = 12
COOKIE = "escrow_admin"


# 요청 본문 (FastAPI 가 타입을 읽을 수 있게 모듈 맨 위에)

class Note(BaseModel):
    note: str = ""

class Reason(BaseModel):
    reason: str = ""

class Resolve(BaseModel):
    reopen: bool = False
    plan: dict[str, str] = {}       # {"<tg_id>": "1000.5"}
    fee: str | None = None

class Assign(BaseModel):
    slot_id: int

class Mark(BaseModel):
    status: str
    note: str = ""

class Done(BaseModel):
    tx_id: str

class Ban(BaseModel):
    banned: bool

class HelperStart(BaseModel):
    api_id: int
    api_hash: str
    phone: str

class HelperCode(BaseModel):
    code: str
    password: str = ""


def _sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_session(secret: str, tg_id: int, now: float | None = None) -> str:
    exp = int((now or time.time()) + SESSION_HOURS * 3600)
    body = f"{tg_id}.{exp}"
    return f"{body}.{_sign(secret, body)}"


def read_session(secret: str, value: str | None, now: float | None = None) -> int | None:
    try:
        tg, exp, sig = (value or "").split(".")
        if not hmac.compare_digest(sig, _sign(secret, f"{tg}.{exp}")) or int(exp) < (now or time.time()):
            return None
        return int(tg)
    except ValueError:
        return None


def create_app(cfg=None, database: DB | None = None) -> FastAPI:
    cfg = cfg or cfgmod.load()
    if len(cfg.admin_secret) < 32:
        raise SystemExit("ADMIN_SECRET 를 32자 이상 무작위로 넣어주세요 (.env)")
    ctx = core.Ctx(cfg)
    state: dict = {"db": database}

    @asynccontextmanager
    async def lifespan(_app):
        if state["db"] is None:
            state["db"] = await DB.connect(cfg.database_url, max_size=4)
            await state["db"].migrate()
            yield
            await state["db"].close()
        else:
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def db() -> DB:
        return state["db"]

    async def admin(request: Request) -> int:
        tg = read_session(cfg.admin_secret, request.cookies.get(COOKIE))
        if tg is None or tg not in ctx.admins:
            raise HTTPException(401, "로그인 필요 (봇에 /login)")
        if request.method != "GET" and request.headers.get("x-requested-with") != "escrow":
            raise HTTPException(403, "잘못된 요청")
        return tg

    async def run(fn, *args):
        """core 호출 — Refused 는 400 + 이유."""
        try:
            async with db().tx() as c:
                return await fn(c, ctx, *args)
        except core.Refused as e:
            raise HTTPException(400, str(e))

    # ── 화면·로그인 ──
    @app.get("/", response_class=HTMLResponse)
    async def index():
        page = WEB / "index.html"
        return FileResponse(page if page.exists() else STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/login")
    async def login(t: str = ""):
        h = hashlib.sha256(t.encode()).hexdigest()
        async with db().tx() as c:
            row = await c.fetchrow("UPDATE admin_logins SET used_at=now() WHERE token_hash=$1 AND used_at IS NULL "
                                   "AND expires_at > now() RETURNING tg_id", h)
            if row:
                await core.event(c, None, row["tg_id"], "admin_login", {})
        if not row or row["tg_id"] not in ctx.admins:
            return HTMLResponse("<p>링크가 만료됐거나 이미 썼어요. 봇에 /login 을 다시 보내주세요.</p>", status_code=401)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(COOKIE, make_session(cfg.admin_secret, row["tg_id"]), max_age=SESSION_HOURS * 3600,
                        httponly=True, secure=True, samesite="strict")
        return resp

    @app.post("/logout")
    async def logout(_: int = Depends(admin)):
        resp = HTMLResponse("ok")
        resp.delete_cookie(COOKIE)
        return resp

    @app.get("/api/me")
    async def me(tg: int = Depends(admin)):
        return {"tg_id": tg, "bot_name": cfg.bot_name, "pay_address": cfg.pay_address, "stub": cfg.stub_fields}

    # ── 대시보드 ──
    @app.get("/api/stats")
    async def stats(_: int = Depends(admin)):
        async with db().tx() as c:
            wb = await c.fetchval("SELECT value FROM kv WHERE key='wallet_balance'")
            s = await core.stats(c, (wb or {}).get("micro"))
        s["wallet_at"] = (wb or {}).get("at")
        for k in ("ledger_micro", "fees_micro", "wallet_micro"):
            s[k.replace("_micro", "")] = fmt(s[k]) if s[k] is not None else None
        return s

    @app.get("/api/todo")
    async def todo(_: int = Depends(admin)):
        """대시보드 '오늘 할 일': 오래 멈춘 거래 · 감시 상태 · 단계별 자동 전환 근거."""
        stuck = await db().fetch(
            "SELECT code, state, updated_at FROM deals WHERE state = ANY($1) AND updated_at < now() - interval '6 hours' "
            "ORDER BY updated_at LIMIT 30", ["START_REVIEW", "SPLIT", "ADDRESS", "PAYOUT", "REFUND", "DISPUTE", "FUNDED"])
        alive = await db().fetchval("SELECT value FROM kv WHERE key='watch_alive'")
        hints = []
        for m in core.MODES:
            r = await db().fetchrow("SELECT count(*) AS n, count(*) FILTER (WHERE status='approved') AS ok FROM ("
                                    "SELECT status FROM approvals WHERE kind=$1 AND status IN ('approved','held') ORDER BY id DESC LIMIT 30) t", m)
            hints.append({"mode": m, "recent": r["n"], "approved": r["ok"]})
        return {"stuck": [{"code": r["code"], "state": r["state"], "since": r["updated_at"].isoformat()} for r in stuck],
                "watch_age": (int(time.time()) - int(alive)) if alive else None, "hints": hints}

    # ── 확인 대기 ──
    @app.get("/api/approvals")
    async def approvals(_: int = Depends(admin)):
        rows = await db().fetch("SELECT a.*, d.code, d.state FROM approvals a LEFT JOIN deals d ON d.id=a.deal_id "
                                "WHERE a.status IN ('pending','held') ORDER BY a.id")
        out = []
        for a in rows:
            detail = ""
            if a["kind"] == "deposit":
                s = await db().fetchrow("SELECT s.*, u.username FROM deposit_slots s JOIN users u ON u.tg_id=s.tg_id WHERE s.id=$1",
                                        a["ref_id"])
                if s:
                    detail = f"@{s['username'] or s['tg_id']} {fmt(s['amount_micro'])} USDT · tx {s['tx_id']}"
            elif a["kind"] in ("partial", "final", "refund"):
                ps = await db().fetch("SELECT p.*, u.username FROM payouts p JOIN users u ON u.tg_id=p.tg_id WHERE split_id=$1 "
                                      "AND status <> 'cancelled'", a["ref_id"])
                detail = " / ".join(f"@{p['username'] or p['tg_id']} {fmt(p['amount_micro'])} → {p['address']}" for p in ps)
            elif a["kind"] == "payout_done":
                p = await db().fetchrow("SELECT * FROM payouts WHERE id=$1", a["ref_id"])
                if p:
                    detail = f"{fmt(p['amount_micro'])} → {p['address']} · tx {p['tx_id']}"
            elif a["kind"] == "start":
                detail = f"예치 합계 {fmt(await db().fetchval('SELECT coalesce(sum(amount_micro),0) FROM deposit_slots WHERE deal_id=$1 AND status=$2', a['deal_id'], 'confirmed'))} USDT"
            out.append({"id": a["id"], "kind": a["kind"], "status": a["status"], "code": a["code"], "deal_state": a["state"],
                        "detail": detail, "created_at": a["created_at"].isoformat()})
        return out


    @app.post("/api/approvals/{aid}/approve")
    async def approve(aid: int, body: Note, tg: int = Depends(admin)):
        try:
            async with db().tx() as c:
                return {"result": await core.decide(c, ctx, aid, tg, True, body.note[:200])}
        except core.Refused as e:
            raise HTTPException(400, str(e))

    @app.post("/api/approvals/{aid}/hold")
    async def hold(aid: int, body: Note, tg: int = Depends(admin)):
        try:
            async with db().tx() as c:
                return {"result": await core.decide(c, ctx, aid, tg, False, body.note[:200])}
        except core.Refused as e:
            raise HTTPException(400, str(e))

    # ── 거래 ──
    @app.get("/api/deals")
    async def deals(state: str = "", q: str = "", _: int = Depends(admin)):
        sql = ("SELECT d.*, a.username au, b.username bu FROM deals d JOIN users a ON a.tg_id=d.creator "
               "LEFT JOIN users b ON b.tg_id=d.partner WHERE TRUE")
        args: list = []
        if state == "active":
            args.append(list(core.CLOSED))
            sql += f" AND d.state <> ALL(${len(args)})"
        elif state:
            args.append(state)
            sql += f" AND d.state = ${len(args)}"
        if q:
            args.append(q.lstrip("@").lower())
            sql += f" AND (lower(d.code)=${len(args)} OR lower(a.username)=${len(args)} OR lower(b.username)=${len(args)})"
        rows = await db().fetch(sql + " ORDER BY d.id DESC LIMIT 200", *args)
        return [{"code": r["code"], "state": r["state"], "creator": r["au"] or r["creator"], "partner": r["bu"] or r["partner"] or r["partner_hint"],
                 "created_at": r["created_at"].isoformat(), "updated_at": r["updated_at"].isoformat()} for r in rows]

    async def _deal(code: str):
        d = await db().fetchrow("SELECT * FROM deals WHERE code=$1", code.upper())
        if not d:
            raise HTTPException(404, "없는 거래")
        return d

    @app.get("/api/deals/{code}")
    async def deal(code: str, _: int = Depends(admin)):
        d = await _deal(code)
        con = await db().pool.acquire()
        try:
            bal = await core.balances(con, d["id"], core.MONEY_HELD)
            parties = [dict(r) for r in await con.fetch(
                "SELECT p.*, u.username, u.name FROM deal_parties p JOIN users u USING (tg_id) WHERE deal_id=$1 ORDER BY role", d["id"])]
        finally:
            await db().pool.release(con)
        conv = lambda rows: [{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in dict(r).items()} for r in rows]  # noqa: E731
        return {
            "deal": conv([d])[0],
            "parties": conv(parties),
            "balances": {str(k): fmt(v) for k, v in bal.items()},
            "slots": conv(await db().fetch("SELECT * FROM deposit_slots WHERE deal_id=$1 ORDER BY id", d["id"])),
            "payouts": conv(await db().fetch("SELECT * FROM payouts WHERE deal_id=$1 ORDER BY id", d["id"])),
            "splits": conv(await db().fetch("SELECT * FROM splits WHERE deal_id=$1 ORDER BY id", d["id"])),
            "events": conv(await db().fetch("SELECT * FROM events WHERE deal_id=$1 ORDER BY id", d["id"])),
            "messages": conv(await db().fetch("SELECT m.*, u.username FROM deal_messages m LEFT JOIN users u ON u.tg_id=m.tg_id "
                                              "WHERE deal_id=$1 ORDER BY m.id LIMIT 2000", d["id"])),
            "ratings": conv(await db().fetch("SELECT * FROM ratings WHERE deal_id=$1 ORDER BY id", d["id"])),
        }


    @app.post("/api/deals/{code}/cancel")
    async def cancel(code: str, body: Reason, tg: int = Depends(admin)):
        d = await _deal(code)
        await run(core.admin_cancel, d["id"], tg, body.reason[:100] or "관리자")
        return {"result": "처리했어요."}


    @app.post("/api/deals/{code}/resolve")
    async def resolve(code: str, body: Resolve, tg: int = Depends(admin)):
        d = await _deal(code)
        if body.reopen:
            await run(core.resolve_dispute, d["id"], tg, None)
            return {"result": "이전 단계로 돌렸어요."}
        plan = {}
        for k, v in body.plan.items():
            m = parse_usdt(v)
            if m is None or not k.lstrip("-").isdecimal():
                raise HTTPException(400, f"금액 형식: {k}={v}")
            plan[int(k)] = m
        fee = None
        if body.fee not in (None, ""):
            fee = parse_usdt(body.fee)
            if fee is None:
                raise HTTPException(400, "수수료 형식")
        await run(core.resolve_dispute, d["id"], tg, plan, fee)
        return {"result": "결정했어요. 받을 분들에게 주소를 물어봐요."}

    # ── 입금 ──
    @app.get("/api/deposits")
    async def deposits(status: str = "", _: int = Depends(admin)):
        if status == "unmatched":
            rows = await db().fetch("SELECT * FROM chain_tx WHERE direction='in' AND status IN ('unmatched','flagged') ORDER BY block_ts DESC")
        else:
            rows = await db().fetch("SELECT * FROM chain_tx ORDER BY block_ts DESC LIMIT 300")
        return [{**{k: v for k, v in dict(r).items() if k not in ("block_ts", "created_at")}, "amount": fmt(r["amount_micro"]),
                 "block_ts": r["block_ts"].isoformat()} for r in rows]

    @app.get("/api/slots/open")
    async def open_slots(_: int = Depends(admin)):
        rows = await db().fetch("SELECT s.*, d.code, u.username FROM deposit_slots s JOIN deals d ON d.id=s.deal_id "
                                "JOIN users u ON u.tg_id=s.tg_id WHERE s.status IN ('waiting','expired') "
                                "AND d.state NOT IN ('DONE','CANCELLED') ORDER BY s.id DESC LIMIT 100")
        return [{"id": r["id"], "code": r["code"], "user": r["username"] or r["tg_id"], "amount": fmt(r["amount_micro"]),
                 "status": r["status"]} for r in rows]


    @app.post("/api/deposits/{tx}/{idx}/assign")
    async def assign(tx: str, idx: int, body: Assign, tg: int = Depends(admin)):
        await run(core.assign_deposit, tx, idx, body.slot_id, tg)
        return {"result": "연결했어요."}


    @app.post("/api/deposits/{tx}/{idx}/mark")
    async def mark(tx: str, idx: int, body: Mark, tg: int = Depends(admin)):
        await run(core.mark_tx, tx, idx, body.status, body.note, tg)
        return {"result": "표시했어요."}

    # ── 지급 ──
    @app.get("/api/payouts")
    async def payouts(status: str = "approved", _: int = Depends(admin)):
        rows = await db().fetch("SELECT p.*, d.code, u.username FROM payouts p JOIN deals d ON d.id=p.deal_id JOIN users u ON u.tg_id=p.tg_id "
                                "WHERE ($1 = '' OR p.status=$1) ORDER BY p.id DESC LIMIT 300", status)
        return [{"id": r["id"], "code": r["code"], "user": r["username"] or r["tg_id"], "kind": r["kind"], "status": r["status"],
                 "address": r["address"], "amount": fmt(r["amount_micro"]), "amount_exact": fmt_exact(r["amount_micro"]),
                 "fee": fmt(r["fee_micro"]), "tx_id": r["tx_id"],
                 "same_twin": sum(1 for x in rows if x["status"] == "approved" and x["address"] == r["address"]
                                  and x["amount_micro"] == r["amount_micro"]) > 1} for r in rows]


    @app.post("/api/payouts/{pid}/done")
    async def payout_done(pid: int, body: Done, tg: int = Depends(admin)):
        await run(core.mark_payout_sent, pid, body.tx_id, tg)
        return {"result": "완료 처리했어요."}

    @app.post("/api/payouts/{pid}/cancel")
    async def payout_cancel(pid: int, body: Reason, tg: int = Depends(admin)):
        """아직 안 보낸 지급 취소 → 받는 사람에게 주소를 다시 물음 (주소가 틀렸을 때)."""
        await run(core.cancel_payout, pid, tg, body.reason[:100])
        return {"result": "취소했어요. 받는 분에게 주소를 다시 물어봤어요."}

    # ── 사용자 목록 · 매출 · 파일 ──
    @app.get("/api/users")
    async def users_list(q: str = "", sort: str = "recent", _: int = Depends(admin)):
        order = {"recent": "u.updated_at DESC", "deals": "done DESC, u.updated_at DESC", "volume": "volume DESC",
                 "disputes": "disputes DESC, u.updated_at DESC"}.get(sort, "u.updated_at DESC")
        rows = await db().fetch(f"""
            SELECT u.tg_id, u.username, u.name, u.banned, u.created_at, u.updated_at,
                   (SELECT count(*) FROM deals d WHERE (d.creator=u.tg_id OR d.partner=u.tg_id) AND d.state='DONE') AS done,
                   (SELECT count(*) FROM deals d WHERE (d.creator=u.tg_id OR d.partner=u.tg_id) AND d.state NOT IN ('DONE','CANCELLED')) AS active,
                   (SELECT count(DISTINCT e.deal_id) FROM events e JOIN deals d ON d.id=e.deal_id
                     WHERE (d.creator=u.tg_id OR d.partner=u.tg_id) AND e.kind='state' AND e.data->>'to'='DISPUTE') AS disputes,
                   (SELECT coalesce(sum(s.amount_micro),0) FROM deposit_slots s WHERE s.tg_id=u.tg_id AND s.status='confirmed') AS volume,
                   (SELECT avg(stars)::float FROM ratings r WHERE r.ratee=u.tg_id) AS rating,
                   (SELECT count(*) FROM ratings r WHERE r.ratee=u.tg_id) AS ratings
            FROM users u
            WHERE ($1 = '' OR u.tg_id::text = $1 OR lower(u.username) LIKE '%' || lower($1) || '%' OR lower(u.name) LIKE '%' || lower($1) || '%')
            ORDER BY {order} LIMIT 200""", q.strip().lstrip("@"))
        return [{**{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in dict(r).items()},
                 "volume": fmt(r["volume"]), "rating": round(r["rating"], 1) if r["rating"] else None} for r in rows]

    @app.get("/api/revenue")
    async def revenue(days: int = 30, _: int = Depends(admin)):
        """하루 단위 (서울 시간): 새 거래 · 끝난 거래 · 예치 금액 · 번 수수료."""
        days = max(7, min(days, 180))
        rows = await db().fetch("""
            WITH d AS (SELECT generate_series((now() AT TIME ZONE 'Asia/Seoul')::date - ($1::int - 1), (now() AT TIME ZONE 'Asia/Seoul')::date,
                                              interval '1 day')::date AS day)
            SELECT d.day,
              (SELECT count(*) FROM deals x WHERE (x.created_at AT TIME ZONE 'Asia/Seoul')::date = d.day) AS new_deals,
              (SELECT count(*) FROM deals x WHERE x.state='DONE' AND (x.closed_at AT TIME ZONE 'Asia/Seoul')::date = d.day) AS done,
              (SELECT coalesce(sum(s.amount_micro),0) FROM deposit_slots s WHERE s.status='confirmed'
                 AND (s.updated_at AT TIME ZONE 'Asia/Seoul')::date = d.day) AS volume,
              (SELECT coalesce(sum(p.fee_micro),0) FROM payouts p WHERE p.status='confirmed'
                 AND (p.updated_at AT TIME ZONE 'Asia/Seoul')::date = d.day) AS fees
            FROM d ORDER BY d.day""", days)
        return [{"day": r["day"].isoformat(), "new_deals": r["new_deals"], "done": r["done"],
                 "volume_micro": int(r["volume"]), "fees_micro": int(r["fees"])} for r in rows]

    @app.get("/api/file/{file_id}")
    async def tg_file(file_id: str, _: int = Depends(admin)):
        """분쟁 증거·거래방 사진을 관리 페이지에서 보기 (봇 토큰으로 텔레그램에서 받아 그대로 전달)."""
        if not cfg.bot_token or not file_id.replace("-", "").replace("_", "").isalnum():
            raise HTTPException(404, "없음")
        async with httpx.AsyncClient(timeout=30) as h:
            r = await h.get(f"https://api.telegram.org/bot{cfg.bot_token}/getFile", params={"file_id": file_id})
            path = (r.json().get("result") or {}).get("file_path")
            if not path:
                raise HTTPException(404, "텔레그램에서 파일을 못 찾았어요")
            f = await h.get(f"https://api.telegram.org/file/bot{cfg.bot_token}/{path}")
        ctype = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp", "mp4": "video/mp4",
                 "pdf": "application/pdf"}.get(path.rsplit(".", 1)[-1].lower(), "application/octet-stream")
        return Response(f.content, media_type=ctype, headers={"Cache-Control": "private, max-age=3600",
                                                              "Content-Disposition": "inline", "X-Content-Type-Options": "nosniff"})

    # ── 사용자 ──
    @app.get("/api/users/{key}")
    async def user(key: str, _: int = Depends(admin)):
        k = key.lstrip("@")
        u = await db().fetchrow("SELECT * FROM users WHERE tg_id::text=$1 OR lower(username)=lower($1) ORDER BY updated_at DESC LIMIT 1", k)
        if not u:
            raise HTTPException(404, "없는 사용자")
        ds = await db().fetch("SELECT code, state, created_at FROM deals WHERE creator=$1 OR partner=$1 ORDER BY id DESC LIMIT 50",
                              u["tg_id"])
        addrs = await db().fetch("SELECT address, last_used FROM addresses WHERE tg_id=$1 ORDER BY last_used DESC", u["tg_id"])
        return {"user": {k2: (v.isoformat() if hasattr(v, "isoformat") else v) for k2, v in dict(u).items()},
                "deals": [{"code": r["code"], "state": r["state"], "created_at": r["created_at"].isoformat()} for r in ds],
                "addresses": [r["address"] for r in addrs]}


    @app.post("/api/users/{tg_id}/ban")
    async def ban(tg_id: int, body: Ban, tg: int = Depends(admin)):
        async with db().tx() as c:
            r = await c.execute("UPDATE users SET banned=$2, updated_at=now() WHERE tg_id=$1", tg_id, body.banned)
            await core.event(c, None, tg, "ban" if body.banned else "unban", {"user": tg_id})
        if r.endswith(" 0"):
            raise HTTPException(404, "없는 사용자")
        return {"result": "차단했어요." if body.banned else "차단 풀었어요."}

    # ── 거래방 ──
    @app.get("/api/rooms")
    async def rooms(_: int = Depends(admin)):
        rows = await db().fetch("SELECT r.*, d.code FROM rooms r LEFT JOIN deals d ON d.id=r.deal_id ORDER BY r.state, r.chat_id")
        return [{"chat_id": r["chat_id"], "title": r["title"], "state": r["state"], "code": r["code"], "note": r["note"]} for r in rows]

    @app.post("/api/rooms/{chat_id}/reset")
    async def room_reset(chat_id: int, tg: int = Depends(admin)):
        async with db().tx() as c:
            r = await c.execute("UPDATE rooms SET state='free', deal_id=NULL, invite_link=NULL, note='', updated_at=now() "
                                "WHERE chat_id=$1 AND state='broken'", chat_id)
            await core.event(c, None, tg, "room_reset", {"chat": chat_id})
        if r.endswith(" 0"):
            raise HTTPException(400, "고장 표시된 방만 되돌릴 수 있어요 (방 상태를 먼저 직접 확인하세요).")
        return {"result": "다시 쓸 수 있게 했어요."}

    # ── 설정 ──
    @app.get("/api/settings")
    async def get_settings(_: int = Depends(admin)):
        async with db().tx() as c:
            s = await core.settings(c)
        return {"values": s, "defaults": core.DEFAULTS}

    @app.put("/api/settings")
    async def put_settings(body: dict, tg: int = Depends(admin)):
        try:
            async with db().tx() as c:
                for k, v in body.items():
                    await core.set_setting(c, k, v, tg)
        except (core.Refused, ValueError, TypeError) as e:
            raise HTTPException(400, str(e))
        return {"result": "저장했어요."}

    # ── 도우미 계정 (거래방 자동 생성) ──
    logins: dict[int, helper.Login] = {}

    @app.get("/api/helper")
    async def helper_get(_: int = Depends(admin)):
        return helper.public(await helper.load(db()))

    @app.post("/api/helper/start")
    async def helper_start(body: HelperStart, tg: int = Depends(admin)):
        phone = "+" + "".join(ch for ch in body.phone if ch.isdigit())
        if len(phone) < 9 or len(body.api_hash.strip()) != 32:
            raise HTTPException(400, "전화번호(국가번호 포함)와 api_hash(32자)를 확인해주세요.")
        lg = helper.Login(body.api_id, body.api_hash.strip(), phone)
        try:
            await lg.send_code()
        except Exception as e:   # noqa: BLE001
            raise HTTPException(400, f"코드 요청 실패: {type(e).__name__}")
        logins[tg] = lg
        await core.event(db(), None, tg, "helper_code_sent", {"phone": phone[:4] + "****" + phone[-2:]})
        return {"result": "텔레그램 앱으로 로그인 코드를 보냈어요. 그 코드를 입력하세요."}

    @app.post("/api/helper/code")
    async def helper_code(body: HelperCode, tg: int = Depends(admin)):
        lg = logins.get(tg)
        if not lg:
            raise HTTPException(400, "먼저 코드 받기를 해주세요.")
        try:
            data = await lg.finish(body.code.strip().replace(" ", ""), body.password)
        except helper.PasswordNeeded:
            raise HTTPException(400, "2단계 인증 비밀번호가 필요해요. 비밀번호 칸을 채워서 다시 눌러주세요.")
        except Exception as e:   # noqa: BLE001
            raise HTTPException(400, f"로그인 실패: {type(e).__name__}")
        logins.pop(tg, None)
        await helper.save(db(), data)
        await core.event(db(), None, tg, "helper_connected", {"user_id": data["user_id"]})
        return {"result": f"도우미 계정 연결됨: {data['name']}. 이제 빈 방이 없으면 자동으로 거래방을 만들어요."}

    @app.post("/api/helper/logout")
    async def helper_logout(tg: int = Depends(admin)):
        await db().execute("DELETE FROM kv WHERE key=$1", helper.KV_KEY)
        await core.event(db(), None, tg, "helper_removed", {})
        return {"result": "도우미 계정 연결을 끊었어요."}

    # ── 기록 ──
    @app.get("/api/audit")
    async def audit(deal: str = "", _: int = Depends(admin)):
        if deal:
            d = await _deal(deal)
            rows = await db().fetch("SELECT * FROM events WHERE deal_id=$1 ORDER BY id DESC LIMIT 500", d["id"])
        else:
            rows = await db().fetch("SELECT * FROM events ORDER BY id DESC LIMIT 500")
        return [{"id": r["id"], "deal_id": r["deal_id"], "actor": r["actor"], "kind": r["kind"], "data": r["data"],
                 "at": r["at"].isoformat()} for r in rows]

    if (WEB / "assets").is_dir():   # 파일 이름에 해시가 붙어 있어 오래 캐시해도 됨
        app.mount("/assets", StaticFiles(directory=WEB / "assets"), name="assets")
    return app


def _lazy():
    return create_app()


class _Lazy:
    """uvicorn escrow.admin.app:app — import 할 때가 아니라 처음 요청 받을 때 설정을 읽음 (테스트가 import 만 해도 안 터지게)."""
    _app = None

    async def __call__(self, scope, receive, send):
        if _Lazy._app is None:
            _Lazy._app = create_app()
        await _Lazy._app(scope, receive, send)


app = _Lazy()
