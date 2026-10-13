"""거래 규칙 (escrow/core.py) — 진짜 PostgreSQL 로 처음부터 끝까지."""
import asyncio
from datetime import timedelta

import pytest

from conftest import ADDR, ADMIN, A, B, C, PAY, ts
from escrow import core
from escrow.money import MICRO

U = MICRO


async def outbox(db, chat=None):
    rows = await db.fetch("SELECT * FROM outbox ORDER BY id")
    return [r for r in rows if chat is None or r["chat_id"] == chat]


async def approvals(db, kind=None):
    rows = await db.fetch("SELECT * FROM approvals WHERE status='pending' ORDER BY id")
    return [r for r in rows if kind is None or r["kind"] == kind]


async def to_choose(db, ctx, room=False):
    if room:
        async with db.tx() as c:
            await c.execute("INSERT INTO rooms(chat_id, title, state) VALUES(-1001,'방1','free')")
    async with db.tx() as c:
        d = await core.create_deal(c, ctx, A, B, None)
    async with db.tx() as c:
        d = await core.answer_invite(c, ctx, d["id"], B, True)
    if room:
        assert d["state"] == "ROOM" and d["room_id"] == -1001
        async with db.tx() as c:
            assert await core.room_joined(c, ctx, -1001, A)
            assert not await core.room_joined(c, ctx, -1001, C), "당사자 아니면 거절"
            assert await core.room_joined(c, ctx, -1001, B)
    for who in (A, B):
        async with db.tx() as c:
            d = await core.ack_notice(c, ctx, d["id"], who)
    assert d["state"] == "CHOOSE"
    return d


async def to_funded(db, ctx, a_amt=1000 * U, b_amt=0, room=False):
    d = await to_choose(db, ctx, room)
    async with db.tx() as c:
        await core.choose(c, ctx, d["id"], A, a_amt > 0)
    async with db.tx() as c:
        moved = await core.choose(c, ctx, d["id"], B, b_amt > 0)
    assert moved
    slots = {}
    for who, amt in ((A, a_amt), (B, b_amt)):
        if amt:
            async with db.tx() as c:
                slots[who] = await core.open_slot(c, ctx, d["id"], who, amt)
    for who, s in slots.items():
        async with db.tx() as c:
            r = await core.record_incoming(c, ctx, f"tx{who}{s['id']}", 0, "TFrom", PAY, s["amount_micro"], ts())
        assert r == "matched"
    for ap in await approvals(db, "deposit"):
        async with db.tx() as c:
            await core.decide(c, ctx, ap["id"], ADMIN, True)
    [ap] = await approvals(db, "start")
    async with db.tx() as c:
        await core.decide(c, ctx, ap["id"], ADMIN, True)
    d = await db.fetchrow("SELECT * FROM deals WHERE id=$1", d["id"])
    assert d["state"] == "FUNDED"
    return d, slots


async def finish_split(db, ctx, d, how="each", custom=None):
    for who in (A, B):
        async with db.tx() as c:
            await core.press_complete(c, ctx, d["id"], who, True)
    async with db.tx() as c:
        sp = await core.propose_split(c, ctx, d["id"], A, how, custom)
    async with db.tx() as c:
        await core.answer_split(c, ctx, d["id"], sp["id"], B, True)
    return sp


async def give_addresses(db, ctx, d):
    for po in await db.fetch("SELECT * FROM payouts WHERE deal_id=$1 AND status='need_address'", d["id"]):
        async with db.tx() as c:
            await core.set_address(c, ctx, po["id"], po["tg_id"], ADDR)


async def test_full_manual_flow_with_room(users, ctx):
    db = users
    d, slots = await to_funded(db, ctx, 1000 * U, 500 * U, room=True)
    # 끝자리 금액: 맡긴 금액 + 0.0001 단위
    assert slots[A]["amount_micro"] == 1000 * U + 100 and slots[B]["amount_micro"] == 500 * U + 100
    # 입금 안내는 1:1 로만 (방에는 주소가 안 감)
    assert all(PAY not in r["text"] for r in await outbox(db, -1001))
    assert any(PAY in r["text"] for r in await outbox(db, A))
    await finish_split(db, ctx, d, "each")
    await give_addresses(db, ctx, d)
    [ap] = await approvals(db, "final")
    async with db.tx() as c:
        await core.decide(c, ctx, ap["id"], ADMIN, True)
    pos = await db.fetch("SELECT * FROM payouts WHERE deal_id=$1 ORDER BY tg_id", d["id"])
    assert [p["status"] for p in pos] == ["approved", "approved"]
    # 수수료 20 을 받는 비율로: A 1000.0001 / B 500.0001 → 합계 20
    assert sum(p["fee_micro"] for p in pos) == 20 * U
    assert sum(p["gross_micro"] for p in pos) == slots[A]["amount_micro"] + slots[B]["amount_micro"]
    # 오너가 보낸 출금이 감지되면 sent → 관리자 완료 확인 → 끝
    for p in pos:
        async with db.tx() as c:
            assert await core.record_outgoing(c, ctx, f"out{p['id']}", 0, PAY, p["address"], p["amount_micro"], ts()) == "matched"
    for ap in await approvals(db, "payout_done"):
        async with db.tx() as c:
            await core.decide(c, ctx, ap["id"], ADMIN, True)
    d = await db.fetchrow("SELECT * FROM deals WHERE id=$1", d["id"])
    assert d["state"] == "DONE" and d["room_id"] is None
    assert [r["kind"] for r in await db.fetch("SELECT kind FROM outbox WHERE kind<>'msg' ORDER BY id")] == ["room_open", "room_close"]


async def test_auto_modes_need_no_admin(users, ctx):
    db = users
    async with db.tx() as c:
        for m in core.MODES:
            await core.set_setting(c, f"mode.{m}", "auto", ADMIN)
    d = await to_choose(db, ctx)
    async with db.tx() as c:
        await core.choose(c, ctx, d["id"], A, True)
    async with db.tx() as c:
        await core.choose(c, ctx, d["id"], B, False)
    async with db.tx() as c:
        s = await core.open_slot(c, ctx, d["id"], A, 100 * U)
    async with db.tx() as c:
        await core.record_incoming(c, ctx, "t1", 0, "TFrom", PAY, s["amount_micro"], ts())
    d = await db.fetchrow("SELECT * FROM deals WHERE id=$1", d["id"])
    assert d["state"] == "FUNDED" and not await approvals(db)
    await finish_split(db, ctx, d, "b")
    await give_addresses(db, ctx, d)
    [po] = await db.fetch("SELECT * FROM payouts WHERE deal_id=$1", d["id"])
    assert po["tg_id"] == B and po["status"] == "approved" and po["amount_micro"] == s["amount_micro"] - 20 * U
    async with db.tx() as c:
        await core.record_outgoing(c, ctx, "o1", 0, PAY, ADDR, po["amount_micro"], ts())
    assert (await db.fetchrow("SELECT state FROM deals WHERE id=$1", d["id"]))["state"] == "DONE"


async def test_auto_max_keeps_big_amounts_manual(users, ctx):
    db = users
    async with db.tx() as c:
        await core.set_setting(c, "mode.deposit", "auto", ADMIN)
        await core.set_setting(c, "auto_max_micro", 500 * U, ADMIN)
    d = await to_choose(db, ctx)
    async with db.tx() as c:
        await core.choose(c, ctx, d["id"], A, True)
    async with db.tx() as c:
        await core.choose(c, ctx, d["id"], B, False)
    async with db.tx() as c:
        s = await core.open_slot(c, ctx, d["id"], A, 1000 * U)
    async with db.tx() as c:
        await core.record_incoming(c, ctx, "t1", 0, "TFrom", PAY, s["amount_micro"], ts())
    assert len(await approvals(db, "deposit")) == 1, "500 넘으면 자동이어도 관리자 확인"


async def test_unique_amounts_never_collide(users, ctx):
    db = users
    deals = []
    for _ in range(3):
        d = await to_choose(db, ctx)
        async with db.tx() as c:
            await core.choose(c, ctx, d["id"], A, True)
        async with db.tx() as c:
            await core.choose(c, ctx, d["id"], B, False)
        deals.append(d)
    got = await asyncio.gather(*[_slot(db, ctx, d["id"]) for d in deals])
    amounts = [s["amount_micro"] for s in got]
    assert len(set(amounts)) == 3 and all(a > 100 * U for a in amounts)


async def _slot(db, ctx, deal_id):
    async with db.tx() as c:
        return await core.open_slot(c, ctx, deal_id, A, 100 * U)


async def test_wrong_amount_and_blacklist_go_to_admin(users, ctx):
    db = users
    d = await to_choose(db, ctx)
    async with db.tx() as c:
        await core.choose(c, ctx, d["id"], A, True)
    async with db.tx() as c:
        await core.choose(c, ctx, d["id"], B, False)
    async with db.tx() as c:
        s = await core.open_slot(c, ctx, d["id"], A, 100 * U)
    async with db.tx() as c:
        assert await core.record_incoming(c, ctx, "t1", 0, "TFrom", PAY, 100 * U, ts()) == "unmatched"
        assert await core.record_incoming(c, ctx, "t2", 0, "TBad", PAY, s["amount_micro"], ts(), blacklisted=True) == "flagged"
        assert await core.record_incoming(c, ctx, "t1", 0, "TFrom", PAY, 100 * U, ts()) == "dup"
    assert (await db.fetchrow("SELECT status FROM deposit_slots WHERE id=$1", s["id"]))["status"] == "waiting"
    # 관리자가 금액 다른 입금을 그 자리에 연결 → 실제 받은 금액으로
    async with db.tx() as c:
        await core.assign_deposit(c, ctx, "t1", 0, s["id"], ADMIN)
    s2 = await db.fetchrow("SELECT * FROM deposit_slots WHERE id=$1", s["id"])
    assert s2["status"] == "confirmed" and s2["amount_micro"] == 100 * U


async def test_partial_limits_and_flow(users, ctx):
    db = users
    d, slots = await to_funded(db, ctx, 1000 * U, 0)
    total = slots[A]["amount_micro"]
    async with db.tx() as c:
        with pytest.raises(core.Refused):
            await core.partial(c, ctx, d["id"], A, total - 10 * U)     # 수수료 20 을 못 남김
    async with db.tx() as c:
        with pytest.raises(core.Refused):
            await core.partial(c, ctx, d["id"], B, 1 * U)               # B 는 맡긴 게 없음
    async with db.tx() as c:
        po = await core.partial(c, ctx, d["id"], A, 300 * U)
    assert po["tg_id"] == B and po["fee_micro"] == 0
    async with db.tx() as c:
        with pytest.raises(core.Refused):
            await core.press_complete(c, ctx, d["id"], A, True)         # 부분 지급 진행 중엔 완료 못 누름
    async with db.tx() as c:
        await core.set_address(c, ctx, po["id"], B, ADDR)
    [ap] = await approvals(db, "partial")
    async with db.tx() as c:
        await core.decide(c, ctx, ap["id"], ADMIN, True)
    async with db.tx() as c:
        await core.mark_payout_sent(c, ctx, po["id"], "a" * 64, ADMIN)
    assert (await db.fetchrow("SELECT state FROM deals WHERE id=$1", d["id"]))["state"] == "FUNDED"
    bal = await db.pool.acquire()
    try:
        assert (await core.balances(bal, d["id"]))[A] == total - 300 * U
    finally:
        await db.pool.release(bal)
    await finish_split(db, ctx, d, "a")
    pos = await db.fetch("SELECT * FROM payouts WHERE deal_id=$1 AND kind='final'", d["id"])
    assert [(p["tg_id"], p["gross_micro"]) for p in pos] == [(A, total - 300 * U)]


async def test_split_needs_other_party_and_custom(users, ctx):
    db = users
    d, slots = await to_funded(db, ctx, 1000 * U, 1000 * U)
    for who in (A, B):
        async with db.tx() as c:
            await core.press_complete(c, ctx, d["id"], who, True)
    async with db.tx() as c:
        sp = await core.propose_split(c, ctx, d["id"], A, "custom", 1500 * U)
    async with db.tx() as c:
        with pytest.raises(core.Refused):
            await core.answer_split(c, ctx, d["id"], sp["id"], A, True)     # 제안자는 동의 못 함
    async with db.tx() as c:
        await core.answer_split(c, ctx, d["id"], sp["id"], B, False)
    async with db.tx() as c:
        with pytest.raises(core.Refused):
            await core.propose_split(c, ctx, d["id"], B, "custom", 99999 * U)
    async with db.tx() as c:
        sp = await core.propose_split(c, ctx, d["id"], B, "custom", 1500 * U)
    async with db.tx() as c:
        await core.answer_split(c, ctx, d["id"], sp["id"], A, True)
    pos = {p["tg_id"]: p for p in await db.fetch("SELECT * FROM payouts WHERE deal_id=$1", d["id"])}
    total = slots[A]["amount_micro"] + slots[B]["amount_micro"]
    assert pos[A]["gross_micro"] == 1500 * U and pos[B]["gross_micro"] == total - 1500 * U
    assert pos[A]["fee_micro"] + pos[B]["fee_micro"] == 20 * U


async def test_double_click_complete_is_once(users, ctx):
    db = users
    d, _ = await to_funded(db, ctx)

    async def press(who):
        async with db.tx() as c:
            return await core.press_complete(c, ctx, d["id"], who, True)
    res = await asyncio.gather(press(A), press(B), press(A), press(B), return_exceptions=True)
    moved = [r for r in res if isinstance(r, tuple) and r[1]]
    assert len(moved) == 1, res
    assert (await db.fetchrow("SELECT state FROM deals WHERE id=$1", d["id"]))["state"] == "SPLIT"


async def test_cancel_before_money_is_immediate(users, ctx):
    db = users
    d = await to_choose(db, ctx, room=True)
    async with db.tx() as c:
        await core.request_cancel(c, ctx, d["id"], B)
    d = await db.fetchrow("SELECT * FROM deals WHERE id=$1", d["id"])
    assert d["state"] == "CANCELLED" and d["room_id"] is None


async def test_cancel_after_money_needs_both_then_refund(users, ctx):
    db = users
    d, slots = await to_funded(db, ctx, 1000 * U, 200 * U)
    async with db.tx() as c:
        await core.request_cancel(c, ctx, d["id"], A)
    assert (await db.fetchrow("SELECT state FROM deals WHERE id=$1", d["id"]))["state"] == "FUNDED"
    async with db.tx() as c:
        await core.answer_cancel(c, ctx, d["id"], B, True)
    d = await db.fetchrow("SELECT * FROM deals WHERE id=$1", d["id"])
    assert d["state"] == "REFUND"
    pos = {p["tg_id"]: p for p in await db.fetch("SELECT * FROM payouts WHERE deal_id=$1", d["id"])}
    assert pos[A]["gross_micro"] == slots[A]["amount_micro"]
    assert sum(p["fee_micro"] for p in pos.values()) == 15 * U, "환불 수수료 15 USDT (예치 비율대로 나눔)"
    assert abs(pos[A]["fee_micro"] - 12500000) <= 1 and abs(pos[B]["fee_micro"] - 2500000) <= 1      # 1000:200 비율
    assert any("수수료 15 USDT 를 빼고" in r["text"] for r in await outbox(db, A))
    await give_addresses(db, ctx, d)
    [ap] = await approvals(db, "refund")
    async with db.tx() as c:
        await core.decide(c, ctx, ap["id"], ADMIN, True)
    for p in await db.fetch("SELECT * FROM payouts WHERE deal_id=$1", d["id"]):
        async with db.tx() as c:
            await core.mark_payout_sent(c, ctx, p["id"], f"{p['id']:064x}", ADMIN)
    assert (await db.fetchrow("SELECT state FROM deals WHERE id=$1", d["id"]))["state"] == "CANCELLED"


async def test_expire_without_and_with_money(users, ctx):
    db = users
    d1 = await to_choose(db, ctx)
    d2, slots = await to_funded(db, ctx, 100 * U, 0)
    async with db.tx() as c:
        # d2 는 입금 단계로 되돌려 시간 초과 흉내 (돈은 들어와 있음)
        await c.execute("UPDATE deals SET state='DEPOSIT', deadline=now() - interval '1 minute' WHERE id=$1", d2["id"])
        await c.execute("UPDATE deals SET deadline=now() - interval '1 minute' WHERE id=$1", d1["id"])
        n = await core.expire_due(c, ctx)
    assert n == 2
    assert (await db.fetchrow("SELECT state FROM deals WHERE id=$1", d1["id"]))["state"] == "CANCELLED"
    assert (await db.fetchrow("SELECT state FROM deals WHERE id=$1", d2["id"]))["state"] == "REFUND"


async def test_dispute_locks_buttons_and_admin_decides(users, ctx):
    db = users
    d, slots = await to_funded(db, ctx, 1000 * U, 0)
    async with db.tx() as c:
        await core.open_dispute(c, ctx, d["id"], B)
    async with db.tx() as c:
        with pytest.raises(core.Refused, match="관리자 확인 중"):
            await core.press_complete(c, ctx, d["id"], A, True)
    total = slots[A]["amount_micro"]
    async with db.tx() as c:
        with pytest.raises(core.Refused):
            await core.resolve_dispute(c, ctx, d["id"], ADMIN, {A: 1, B: 1})
    async with db.tx() as c:
        await core.resolve_dispute(c, ctx, d["id"], ADMIN, {A: 100 * U, B: total - 100 * U})
    await give_addresses(db, ctx, d)
    pos = await db.fetch("SELECT * FROM payouts WHERE deal_id=$1", d["id"])
    assert all(p["status"] == "approved" for p in pos), "관리자가 정한 분배는 주소만 받으면 바로 승인"
    assert not await approvals(db)


async def test_payout_never_exceeds_deposit(users, ctx):
    db = users
    d, slots = await to_funded(db, ctx, 100 * U, 0)
    async with db.tx() as c:
        with pytest.raises(core.Refused, match="예치금보다"):
            await core._make_payouts(c, ctx, d, None, {A: 101 * U}, "final", 0)


async def test_not_party_and_admin_only(users, ctx):
    db = users
    d, _ = await to_funded(db, ctx)
    async with db.tx() as c:
        with pytest.raises(core.Refused, match="당사자"):
            await core.press_complete(c, ctx, d["id"], C, True)
    async with db.tx() as c:
        with pytest.raises(core.Refused, match="관리자만"):
            await core.resolve_dispute(c, ctx, d["id"], A, None)


async def test_events_are_append_only(users, ctx):
    db = users
    await to_choose(db, ctx)
    with pytest.raises(Exception, match="고치거나 지울 수 없음"):
        await db.execute("DELETE FROM events")
    with pytest.raises(Exception, match="고치거나 지울 수 없음"):
        await db.execute("UPDATE events SET kind='x'")


async def test_partner_by_deeplink(users, ctx):
    db = users
    async with db.tx() as c:
        d = await core.create_deal(c, ctx, A, None, "bobby")
    async with db.tx() as c:
        with pytest.raises(core.Refused):
            await core.claim_invite(c, ctx, d["code"], A)
    async with db.tx() as c:
        await core.claim_invite(c, ctx, d["code"].lower(), B)
    async with db.tx() as c:
        with pytest.raises(core.Refused):
            await core.claim_invite(c, ctx, d["code"], C)          # 이미 B 가 잡음
    async with db.tx() as c:
        d = await core.answer_invite(c, ctx, d["id"], B, True)
    assert d["state"] == "NOTICE", "빈 방이 없으면 1:1 로 바로 공지"


async def test_stub_address_blocks_deposits(users):
    from escrow.config import Config
    stub = core.Ctx(Config(admin_ids=frozenset({ADMIN})))
    db = users
    d = await to_choose(db, stub)
    async with db.tx() as c:
        await core.choose(c, stub, d["id"], A, True)
    async with db.tx() as c:
        await core.choose(c, stub, d["id"], B, False)
    async with db.tx() as c:
        with pytest.raises(core.Refused):
            await core.open_slot(c, stub, d["id"], A, 100 * U)
