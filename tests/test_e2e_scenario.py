"""1 USDT 실전 흐름 모의: 봇 화면(가짜 텔레그램) + 감시(가짜 TronGrid, 실제 tx 모양) + 관리 페이지 API 를 실제 순서대로.
실행해서 흐름 보기: pytest -s tests/test_e2e_scenario.py"""
import time

import httpx

from conftest import A, ADMIN, B
from escrow import core, tron, watcher
from escrow.admin import app as adminapp
from escrow.config import Config
from escrow.money import MICRO, fmt
from test_bot_flow import Env
from test_watch_admin import FakeTron, info

PAY = "TR7sTzwA9nVfDiSyge8Ez7GJ6jFng7tVC1"          # 실제 받는 주소 (체크섬 맞음)
A_WALLET = "TLa2f6VPqDgRE67v1736s7bJ8Ray5wYjU7"      # 손님 A 지갑 (가상)
B_WALLET = tron.USDT_CONTRACT                        # B 가 받을 주소 (형식만 맞으면 됨)
SECRET = "z" * 40


def say(*a):
    print("  ▸", *a)


class World:
    def __init__(self, db):
        self.db = db
        self.env = Env(db)
        self.cfg = Config(admin_ids=frozenset({ADMIN}), pay_address=PAY, bot_name="딜스크로", support="@jwjw0101",
                          bot_username="dealscrow_safe_bot", admin_secret=SECRET)
        self.env.cfg = self.cfg
        self.env.app.bot_data.update(cfg=self.cfg, ctx=core.Ctx(self.cfg))
        self.ctx = self.env.app.bot_data["ctx"]
        self.chain = FakeTron()
        self.done, self.failed = set(), set()
        self.admin = httpx.AsyncClient(transport=httpx.ASGITransport(app=adminapp.create_app(self.cfg, db)),
                                       base_url="https://admin.test", headers={"X-Requested-With": "escrow"})
        self.admin.cookies.set(adminapp.COOKIE, adminapp.make_session(SECRET, ADMIN))

    def wallet_send(self, tx, frm, to, amount):
        """지갑에서 실제로 보낸 것처럼: 블록에 tx 가 생기고 확정 조회가 됨."""
        ms = int(time.time() * 1000)
        self.chain.cands.append((tx, ms))
        self.chain.infos[tx] = info(tx, frm, to, amount, ts_ms=ms)

    async def watch(self):
        n = await watcher.scan_once(self.db, self.ctx, self.chain, self.failed, self.done)
        await self.env.flush()
        return n

    async def approve_all(self, kind):
        rows = (await self.admin.get("/api/approvals")).json()
        for a in [r for r in rows if r["kind"] == kind]:
            r = await self.admin.post(f"/api/approvals/{a['id']}/approve", json={"note": "모의"})
            assert r.status_code == 200, r.text
        await self.env.flush()
        return len([r for r in rows if r["kind"] == kind])


async def test_one_usdt_deal_end_to_end(users):
    db = users
    w = World(db)
    env, bot = w.env, w.env.bot
    print("\n[1 USDT 모의 거래]")
    r = await w.admin.put("/api/settings", json={"min_micro": 1 * MICRO, "fee_micro": MICRO // 10, "refund_fee_micro": 0})
    assert r.status_code == 200, r.text
    say("관리 페이지: 최소 예치 1 USDT · 수수료 0.1 USDT 로 설정")

    for who in (A, B):
        await env.start(who)
        await env.press(who, "t:skip")
    await db.execute("INSERT INTO rooms(chat_id, title, state) VALUES(-500, '거래방1', 'free')")
    await env.press(A, "menu:new")
    await env.text(A, "@bobby")
    inv = [t for t in bot.texts(B) if "거래 요청" in t][-1]
    assert str(A) in inv
    say("A 가 @bobby 에게 거래 요청 → B 화면에 숫자 ID·거래 기록 표시")
    await env.press(B, env.last_button(B, "d:acc"))
    assert any("t.me/+room-500" in t for t in bot.texts(A))
    for who in (A, B):
        async with db.tx() as c:
            assert await core.room_joined(c, w.ctx, -500, who)
    await env.flush()
    assert "공식 봇은 @dealscrow_safe_bot" in bot.texts(-500)[-1]
    say("B 수락 → 거래방 링크 → 둘 다 입장 → 공지 (공식 봇 안내 포함)")
    for who in (A, B):
        await env.press(who, env.last_button(-500, "d:ntc"))
    await env.press(A, env.last_button(-500, "d:dep"))
    await env.press(B, env.last_button(-500, "d:nodep"))
    await env.text(A, "1")
    guide = [t for t in bot.texts(A) if "입금 안내" in t][-1]
    slot = await db.fetchrow("SELECT * FROM deposit_slots WHERE tg_id=$1 AND status='waiting'", A)
    exact = slot["amount_micro"]
    assert PAY in guide and core.fmt(exact) in guide and 1 * MICRO < exact < 1 * MICRO + MICRO // 10
    assert not any(PAY in t for t in bot.texts(-500))
    say(f"A 예치 1 USDT 선택 → 1:1 입금 안내: {fmt(exact)} USDT → {PAY[:8]}… (방엔 주소 안 감)")

    w.wallet_send("a" * 64, A_WALLET, PAY, exact)
    assert await w.watch() == 1
    assert (await db.fetchval("SELECT status FROM deposit_slots WHERE id=$1", slot["id"])) == "received"
    say("A 지갑에서 송금 → 감시가 확정 블록에서 찾아 '받음' → 관리자 텔레그램에 확인 요청")
    assert await w.approve_all("deposit") == 1
    assert await w.approve_all("start") == 1
    assert (await db.fetchval("SELECT state FROM deals WHERE id=$1", slot["deal_id"])) == "FUNDED"
    say("관리 페이지에서 입금 확인 승인 → 거래 시작 승인 → 거래 중")

    for who in (A, B):
        await env.press(who, env.last_button(-500, "d:fin"))
        await env.press(who, env.last_button(who, "d:finy"))
    await env.press(A, env.last_button(-500, "d:sp:b"))
    await env.press(B, env.last_button(B, "d:spok"))
    say("둘 다 [거래완료] → A 가 '전액 B 에게' → B 동의")
    await env.text(B, B_WALLET)
    await env.text(B, B_WALLET)
    assert await w.approve_all("final") == 1
    todo = (await w.admin.get("/api/payouts?status=approved")).json()
    assert len(todo) == 1
    p = todo[0]
    assert p["address"] == B_WALLET and p["amount_exact"] == core.fmt(exact - MICRO // 10).replace(",", "")
    say(f"B 받을 주소 두 번 입력 → 관리자 승인 → '보낼 것': {p['amount_exact']} USDT (수수료 {p['fee']}) → {B_WALLET[:8]}…")

    amount = await db.fetchval("SELECT amount_micro FROM payouts WHERE id=$1", p["id"])
    w.wallet_send("b" * 64, PAY, B_WALLET, amount)
    assert await w.watch() == 1
    assert (await db.fetchval("SELECT status FROM payouts WHERE id=$1", p["id"])) == "sent"
    say("오너가 지갑에서 그대로 송금 → 감시가 출금을 찾아 자동 연결 ('보냄')")
    assert await w.approve_all("payout_done") == 1
    d = await db.fetchrow("SELECT * FROM deals WHERE id=$1", slot["deal_id"])
    assert d["state"] == "DONE"
    assert any("영수증" in t and "tronscan" in t for t in bot.texts(B))
    assert {c[0] for c in bot.calls} >= {"revoke", "ban", "unban", "delete"}
    assert (await db.fetchval("SELECT state FROM rooms WHERE chat_id=-500")) == "free"
    say("송금 완료 확인 → 거래 끝 → 두 사람에게 영수증 → 거래방 정리(링크 폐기·내보내기·글 삭제) → 방 다시 비어 있음")

    s = (await w.admin.get("/api/stats")).json()
    assert s["ledger_micro"] == MICRO // 10 and s["fees_micro"] == MICRO // 10
    say(f"장부: 지갑에 남아야 할 돈 {s['ledger']} USDT = 번 수수료 {s['fees']} (끝자리 {fmt(exact - MICRO)} 는 B 에게 같이 감)")


async def test_one_usdt_wrong_amount_then_admin_links_it(users):
    """손님이 끝자리를 빼고 1.00 만 보냄 → 자동 확인 안 됨 → 관리자가 관리 페이지에서 그 거래에 연결."""
    db = users
    w = World(db)
    env = w.env
    print("\n[끝자리 틀린 입금]")
    await w.admin.put("/api/settings", json={"min_micro": 1 * MICRO, "fee_micro": MICRO // 10, "refund_fee_micro": 0})
    for who in (A, B):
        await env.start(who)
        await env.press(who, "t:skip")
    await env.press(A, "menu:new")
    await env.text(A, "@bobby")
    await env.press(B, env.last_button(B, "d:acc"))
    for who in (A, B):
        await env.press(who, env.last_button(who, "d:ntc"))
    await env.press(A, env.last_button(A, "d:dep"))
    await env.press(B, env.last_button(B, "d:nodep"))
    await env.text(A, "1")
    slot = await db.fetchrow("SELECT * FROM deposit_slots WHERE tg_id=$1 AND status='waiting'", A)
    w.wallet_send("c" * 64, A_WALLET, PAY, 1 * MICRO)
    await w.watch()
    dep = (await w.admin.get("/api/deposits?status=unmatched")).json()
    assert len(dep) == 1 and any("확인 필요한 입금" in t for t in env.bot.texts(ADMIN))
    say("1.00 USDT 도착 → 맞는 거래 없음 → 관리자 알림 + '입금 확인' 목록")
    slots = (await w.admin.get("/api/slots/open")).json()
    assert [s["id"] for s in slots] == [slot["id"]]
    r = await w.admin.post(f"/api/deposits/{'c' * 64}/0/assign", json={"slot_id": slot["id"]})
    assert r.status_code == 200, r.text
    assert (await db.fetchval("SELECT amount_micro FROM deposit_slots WHERE id=$1", slot["id"])) == 1 * MICRO
    assert await w.approve_all("start") == 1
    assert (await db.fetchval("SELECT state FROM deals WHERE id=$1", slot["deal_id"])) == "FUNDED"
    say("관리자가 그 거래에 연결 → 받은 금액(1.00) 그대로 인정 → 시작 승인 → 거래 중")


async def test_one_usdt_cancel_refund(users):
    """예치 뒤 둘 다 취소 → 환불 → 오너 송금 감지 → 취소로 끝 (수수료 0)."""
    db = users
    w = World(db)
    env = w.env
    print("\n[취소 → 환불]")
    await w.admin.put("/api/settings", json={"min_micro": 1 * MICRO, "fee_micro": MICRO // 10, "refund_fee_micro": 0})
    for who in (A, B):
        await env.start(who)
        await env.press(who, "t:skip")
    await env.press(A, "menu:new")
    await env.text(A, "@bobby")
    await env.press(B, env.last_button(B, "d:acc"))
    for who in (A, B):
        await env.press(who, env.last_button(who, "d:ntc"))
    await env.press(A, env.last_button(A, "d:dep"))
    await env.press(B, env.last_button(B, "d:nodep"))
    await env.text(A, "1")
    slot = await db.fetchrow("SELECT * FROM deposit_slots WHERE tg_id=$1 AND status='waiting'", A)
    w.wallet_send("d" * 64, A_WALLET, PAY, slot["amount_micro"])
    await w.watch()
    await w.approve_all("deposit")
    await w.approve_all("start")
    await env.press(A, env.last_button(A, "d:cx"))
    await env.press(B, env.last_button(B, "d:cxok"))
    assert (await db.fetchval("SELECT state FROM deals WHERE id=$1", slot["deal_id"])) == "REFUND"
    say("A 취소 요청 → B 동의 → 환불 대기 → A 에게 받을 주소 물음")
    await env.text(A, A_WALLET)
    await env.text(A, A_WALLET)
    await w.approve_all("refund")
    [p] = (await w.admin.get("/api/payouts?status=approved")).json()
    assert p["amount_exact"] == core.fmt(slot["amount_micro"]).replace(",", "") and p["fee"] == "0"
    w.wallet_send("e" * 64, PAY, A_WALLET, slot["amount_micro"])
    await w.watch()
    await w.approve_all("payout_done")
    assert (await db.fetchval("SELECT state FROM deals WHERE id=$1", slot["deal_id"])) == "CANCELLED"
    say(f"환불 {p['amount_exact']} USDT 승인 → 오너 송금 감지 → 취소로 끝 (수수료 0)")
