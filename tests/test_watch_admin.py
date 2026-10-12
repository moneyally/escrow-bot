"""트론 해석 · 감시 · 관리 페이지 API."""
import hashlib
import time

import httpx
import pytest

from conftest import ADDR, ADMIN, A, B, PAY
from escrow import core, tron, watcher
from escrow.admin import app as adminapp
from escrow.config import Config
from escrow.money import MICRO

PAY_OK = "TXYZopYRdj2D9XRtbG411XZZ3kM5VkAeBf"   # 감시 테스트용 (체크섬 상관없이 문자열 비교만)


def info(tx_id, frm, to, amount, ok=True, contract=tron.USDT_CONTRACT, ts_ms=None):
    def pad(a):
        return "0" * 24 + tron.to_hex(a)[2:]
    return {"id": tx_id, "blockTimeStamp": ts_ms or int(time.time() * 1000), "receipt": {"result": "SUCCESS" if ok else "REVERT"},
            "log": [{"address": tron.to_hex(contract)[2:], "topics": [tron.TRANSFER_TOPIC, pad(frm), pad(to)], "data": f"{amount:064x}"}]}


SENDER = "TLa2f6VPqDgRE67v1736s7bJ8Ray5wYjU7"


def test_address_checks():
    assert tron.is_address(tron.USDT_CONTRACT) and tron.is_address(SENDER)
    assert not tron.is_address("T" + "1" * 33) and not tron.is_address(tron.USDT_CONTRACT.lower())
    assert tron.from_hex(tron.to_hex(SENDER)) == SENDER


def test_transfer_decode_only_success_and_usdt():
    good = tron.transfers_from_info(info("aa", SENDER, tron.USDT_CONTRACT, 5 * MICRO))
    assert [(t.from_addr, t.to_addr, t.amount) for t in good] == [(SENDER, tron.USDT_CONTRACT, 5 * MICRO)]
    assert tron.transfers_from_info(info("bb", SENDER, tron.USDT_CONTRACT, 5, ok=False)) == []
    assert tron.transfers_from_info(info("cc", SENDER, tron.USDT_CONTRACT, 5, contract=SENDER)) == [], "가짜 토큰"
    assert tron.transfers_from_info({**info("dd", SENDER, tron.USDT_CONTRACT, 5), "result": "FAILED"}) == []


class FakeTron:
    def __init__(self):
        self.cands, self.infos, self.black = [], {}, set()

    async def candidate_txids(self, address, since_ms, limit_pages=5):
        return [c for c in self.cands if c[1] >= since_ms]

    async def solid_info(self, tx_id):
        return self.infos.get(tx_id)

    async def is_blacklisted(self, a):
        return a in self.black


async def test_watcher_matches_deposit_and_waits_for_solid(users, ctx):
    db = users
    pay = tron.USDT_CONTRACT          # 감시는 문자열 비교라 아무 진짜 주소나 우리 주소로
    wctx = core.Ctx(Config(admin_ids=frozenset({ADMIN}), pay_address=pay))
    from test_core import to_choose
    d = await to_choose(db, wctx)
    async with db.tx() as c:
        await core.choose(c, wctx, d["id"], A, True)
    async with db.tx() as c:
        await core.choose(c, wctx, d["id"], B, False)
    async with db.tx() as c:
        s = await core.open_slot(c, wctx, d["id"], A, 100 * MICRO)
    ft = FakeTron()
    now_ms = int(time.time() * 1000)
    ft.cands = [("t1", now_ms), ("t2", now_ms + 1)]
    ft.infos["t1"] = info("t1", SENDER, pay, s["amount_micro"], ts_ms=now_ms)     # t2 는 아직 확정 전
    failed = set()
    assert await watcher.scan_once(db, wctx, ft, failed) == 1
    assert (await db.fetchrow("SELECT status FROM deposit_slots WHERE id=$1", s["id"]))["status"] == "received"
    cur1 = await watcher.get_cursor(db)
    ft.infos["t2"] = info("t2", SENDER, pay, 7 * MICRO, ts_ms=now_ms + 1)
    assert await watcher.scan_once(db, wctx, ft, failed) == 1
    assert await watcher.scan_once(db, wctx, ft, failed) == 0, "같은 tx 두 번 안 셈"
    assert await watcher.get_cursor(db) >= cur1
    assert (await db.fetchrow("SELECT status FROM chain_tx WHERE tx_id='t2'"))["status"] == "unmatched"


async def test_watcher_flags_blacklisted_sender(users):
    db = users
    pay = tron.USDT_CONTRACT
    wctx = core.Ctx(Config(admin_ids=frozenset({ADMIN}), pay_address=pay))
    ft = FakeTron()
    ft.black.add(SENDER)
    ft.cands = [("t9", int(time.time() * 1000))]
    ft.infos["t9"] = info("t9", SENDER, pay, 3 * MICRO)
    await watcher.scan_once(db, wctx, ft, set())
    assert (await db.fetchrow("SELECT status FROM chain_tx WHERE tx_id='t9'"))["status"] == "flagged"


# ── 관리 페이지 ──
SECRET = "s" * 40


def make(db):
    cfg = Config(admin_ids=frozenset({ADMIN}), pay_address=PAY, admin_secret=SECRET)
    return adminapp.create_app(cfg, db)


async def client(db, logged_in=True, who=ADMIN):
    app = make(db)
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://admin.test")
    if logged_in:
        c.cookies.set(adminapp.COOKIE, adminapp.make_session(SECRET, who))
    return c


def test_session_signing():
    s = adminapp.make_session(SECRET, 5)
    assert adminapp.read_session(SECRET, s) == 5
    assert adminapp.read_session("x" * 40, s) is None
    tg, exp, sig = s.split(".")
    assert adminapp.read_session(SECRET, f"6.{exp}.{sig}") is None, "ID 바꾸면 서명 깨짐"
    assert adminapp.read_session(SECRET, s, now=time.time() + 13 * 3600) is None, "12시간 지나면 만료"


async def test_admin_requires_login_admin_id_and_csrf_header(users):
    db = users
    async with await client(db, logged_in=False) as c:
        assert (await c.get("/api/stats")).status_code == 401
    async with await client(db, who=A) as c:
        assert (await c.get("/api/stats")).status_code == 401, "관리자 아닌 사람 쿠키"
    async with await client(db) as c:
        assert (await c.get("/api/stats")).status_code == 200
        r = await c.put("/api/settings", json={"fee_micro": 1})
        assert r.status_code == 403, "X-Requested-With 없으면 거절"


async def test_login_link_is_one_time(users):
    db = users
    token = "tok123"
    await db.execute("INSERT INTO admin_logins(token_hash, tg_id, expires_at) VALUES($1,$2, now() + interval '5 minutes')",
                     hashlib.sha256(token.encode()).hexdigest(), ADMIN)
    async with await client(db, logged_in=False) as c:
        r = await c.get(f"/login?t={token}")
        assert r.status_code == 303 and adminapp.COOKIE in r.headers.get("set-cookie", "")
        assert "HttpOnly" in r.headers["set-cookie"] and "samesite=strict" in r.headers["set-cookie"].lower()
        r2 = await c.get(f"/login?t={token}")
        assert r2.status_code == 401, "두 번째는 안 됨"


async def test_admin_approves_and_marks_payout(users, ctx):
    db = users
    from test_core import finish_split, give_addresses, to_funded
    d, slots = await to_funded(db, ctx, 100 * MICRO, 0)
    await finish_split(db, ctx, d, "b")
    await give_addresses(db, ctx, d)
    H = {"X-Requested-With": "escrow"}
    async with await client(db) as c:
        ap = (await c.get("/api/approvals")).json()
        assert [a["kind"] for a in ap] == ["final"] and ADDR in ap[0]["detail"]
        assert (await c.post(f"/api/approvals/{ap[0]['id']}/approve", json={}, headers=H)).status_code == 200
        assert (await c.post(f"/api/approvals/{ap[0]['id']}/approve", json={}, headers=H)).status_code == 400, "두 번 승인 안 됨"
        [p] = (await c.get("/api/payouts")).json()
        assert p["address"] == ADDR and p["amount"] == "80.0001"
        bad = await c.post(f"/api/payouts/{p['id']}/done", json={"tx_id": "zz"}, headers=H)
        assert bad.status_code == 400
        ok = await c.post(f"/api/payouts/{p['id']}/done", json={"tx_id": "b" * 64}, headers=H)
        assert ok.status_code == 200
        info_ = (await c.get(f"/api/deals/{d['code']}")).json()
        assert info_["deal"]["state"] == "DONE"
        s = (await c.get("/api/stats")).json()
        assert s["fees"] == "20" and s["ledger_micro"] == 20 * MICRO


async def test_admin_settings_validation(users):
    db = users
    H = {"X-Requested-With": "escrow"}
    async with await client(db) as c:
        assert (await c.put("/api/settings", json={"mode.deposit": "auto"}, headers=H)).status_code == 200
        assert (await c.put("/api/settings", json={"mode.deposit": "yolo"}, headers=H)).status_code == 400
        assert (await c.put("/api/settings", json={"nope": 1}, headers=H)).status_code == 400
        assert (await c.put("/api/settings", json={"fee_micro": -5}, headers=H)).status_code == 400
        v = (await c.get("/api/settings")).json()["values"]
        assert v["mode.deposit"] == "auto" and v["fee_micro"] == 20 * MICRO


async def test_admin_dispute_resolve(users, ctx):
    db = users
    from test_core import to_funded
    d, slots = await to_funded(db, ctx, 100 * MICRO, 0)
    async with db.tx() as c:
        await core.open_dispute(c, ctx, d["id"], A)
    H = {"X-Requested-With": "escrow"}
    total = slots[A]["amount_micro"]
    async with await client(db) as c:
        r = await c.post(f"/api/deals/{d['code']}/resolve", json={"plan": {str(A): "1", str(B): "1"}}, headers=H)
        assert r.status_code == 400
        r = await c.post(f"/api/deals/{d['code']}/resolve",
                         json={"plan": {str(A): "50", str(B): str((total - 50 * MICRO) / MICRO)}, "fee": "0"}, headers=H)
        assert r.status_code == 200, r.text
    pos = await db.fetch("SELECT * FROM payouts WHERE deal_id=$1", d["id"])
    assert sorted(p["gross_micro"] for p in pos) == sorted([50 * MICRO, total - 50 * MICRO]) and all(p["fee_micro"] == 0 for p in pos)
