"""버그 리뷰(2026-10-12)에서 나온 것들 — 예전엔 거래가 영원히 멈추거나 돈이 거래 밖으로 떨어졌던 경로. 고친 줄을 되돌리면 실패해야 함."""
import time

import pytest

from conftest import ADDR, ADMIN, A, B, C, PAY, ts
from escrow import core, watcher
from escrow.config import Config
from test_core import U, approvals, finish_split, give_addresses, to_choose, to_funded

HASH = "ab" * 32


async def state(db, d):
    return await db.fetchval("SELECT state FROM deals WHERE id=$1", d["id"])


async def test_dispute_voids_approvals_and_reopen_finishes(users, ctx):
    """#1: 승인 대기 중 분쟁 → 예전 [승인] 버튼은 안 먹힘 → 분쟁 풀면 승인 다시 걸리고 끝까지 감."""
    db = users
    d, _ = await to_funded(db, ctx, 1000 * U)
    await finish_split(db, ctx, d, "each")
    await give_addresses(db, ctx, d)
    [ap] = await approvals(db, "final")
    async with db.tx() as c:
        await core.open_dispute(c, ctx, d["id"], B)
    assert not await approvals(db), "분쟁 열면 확인 대기는 무효"
    assert await db.fetchval("SELECT status FROM approvals WHERE id=$1", ap["id"]) == "void"
    async with db.tx() as c:
        with pytest.raises(core.Refused):
            await core.decide(c, ctx, ap["id"], ADMIN, True)
    async with db.tx() as c:
        await core.resolve_dispute(c, ctx, d["id"], ADMIN, None)
    [ap2] = await approvals(db, "final")
    async with db.tx() as c:
        await core.decide(c, ctx, ap2["id"], ADMIN, True)
    for p in await db.fetch("SELECT * FROM payouts WHERE deal_id=$1 AND status='approved'", d["id"]):
        async with db.tx() as c:
            await core.mark_payout_sent(c, ctx, p["id"], f"{p['id']:064x}", ADMIN)
    assert await state(db, d) == "DONE"


async def test_payout_sent_during_dispute_then_reopen_closes(users, ctx):
    """#1-B: 환불 승인 뒤 분쟁 → 그 사이 송금 → 분쟁 풀면 닫힘."""
    db = users
    d, _ = await to_funded(db, ctx, 1000 * U)
    async with db.tx() as c:
        await core.request_cancel(c, ctx, d["id"], A)
    async with db.tx() as c:
        await core.answer_cancel(c, ctx, d["id"], B, True)
    await give_addresses(db, ctx, d)
    [ap] = await approvals(db, "refund")
    async with db.tx() as c:
        await core.decide(c, ctx, ap["id"], ADMIN, True)
    async with db.tx() as c:
        await core.open_dispute(c, ctx, d["id"], A)
    [p] = await db.fetch("SELECT * FROM payouts WHERE deal_id=$1 AND status='approved'", d["id"])
    async with db.tx() as c:
        await core.mark_payout_sent(c, ctx, p["id"], HASH, ADMIN)
    async with db.tx() as c:
        await core.resolve_dispute(c, ctx, d["id"], ADMIN, None)
    assert await state(db, d) == "CANCELLED"


async def test_dispute_with_sent_payout_can_be_decided(users, ctx):
    """#1: 일부 지급이 이미 나간 뒤 분쟁 → 남은 돈 기준으로 분배 결정 가능 (예전엔 '맡긴 돈보다 많이' 로 거절)."""
    db = users
    d, slots = await to_funded(db, ctx, 1000 * U)
    async with db.tx() as c:
        po = await core.partial(c, ctx, d["id"], A, 300 * U)
    async with db.tx() as c:
        await core.set_address(c, ctx, po["id"], B, ADDR)
    [ap] = await approvals(db, "partial")
    async with db.tx() as c:
        await core.decide(c, ctx, ap["id"], ADMIN, True)
    async with db.tx() as c:
        await core.mark_payout_sent(c, ctx, po["id"], HASH, ADMIN)
    async with db.tx() as c:
        await core.open_dispute(c, ctx, d["id"], A)
    rest = slots[A]["amount_micro"] - 300 * U
    async with db.tx() as c:
        await core.resolve_dispute(c, ctx, d["id"], ADMIN, {A: rest, B: 0})
    assert [p["gross_micro"] for p in await db.fetch("SELECT * FROM payouts WHERE deal_id=$1 AND kind='final'", d["id"])] == [rest]


async def test_reopen_after_deposit_dispute_restores_deadline_and_approval(users, ctx):
    """#2: 입금 대기 중 분쟁 → 그 사이 입금 → 이어가기 = 마감 시각 다시 + 입금 확인 요청 다시."""
    db = users
    d = await to_choose(db, ctx)
    async with db.tx() as c:
        await core.choose(c, ctx, d["id"], A, True)
    async with db.tx() as c:
        await core.choose(c, ctx, d["id"], B, False)
    async with db.tx() as c:
        s = await core.open_slot(c, ctx, d["id"], A, 100 * U)
    async with db.tx() as c:
        await core.open_dispute(c, ctx, d["id"], B)
    async with db.tx() as c:
        assert await core.record_incoming(c, ctx, "txA", 0, "TFrom", PAY, s["amount_micro"], ts()) == "matched"
    async with db.tx() as c:
        await core.resolve_dispute(c, ctx, d["id"], ADMIN, None)
    row = await db.fetchrow("SELECT * FROM deals WHERE id=$1", d["id"])
    assert row["state"] == "DEPOSIT" and row["deadline"] is not None
    [ap] = await approvals(db, "deposit")
    async with db.tx() as c:
        await core.decide(c, ctx, ap["id"], ADMIN, True)
    assert await state(db, d) == "START_REVIEW"


async def test_address_prompt_can_be_reopened(users, ctx):
    """#3: 주소 입력 대기가 지워져도 [받을 주소 입력] 버튼으로 다시."""
    db = users
    d, _ = await to_funded(db, ctx, 100 * U)
    await finish_split(db, ctx, d, "b")
    await db.execute("DELETE FROM inputs")
    async with db.tx() as c:
        [po] = await core.pending_addresses(c, B)
        await core.reopen_address(c, po["id"], B)
        with pytest.raises(core.Refused):
            await core.reopen_address(c, po["id"], A)        # 남의 지급
    assert await db.fetchval("SELECT ref_id FROM inputs WHERE tg_id=$1", B) == po["id"]
    msgs = await db.fetch("SELECT buttons FROM outbox WHERE chat_id=$1 AND text LIKE '%받을 주소%'", B)
    assert any(f"d:addr:{po['id']}" in str(m["buttons"]) for m in msgs)


async def test_late_deposit_after_refund_is_refunded(users, ctx):
    """#4: A 환불 끝난 뒤 B 의 늦은 입금 → 관리자 연결 → B 에게만 환불 (예전엔 '맡긴 돈보다 많이' 로 거절)."""
    db = users
    d = await to_choose(db, ctx)
    for who in (A, B):
        async with db.tx() as c:
            await core.choose(c, ctx, d["id"], who, True)
    async with db.tx() as c:
        sa = await core.open_slot(c, ctx, d["id"], A, 100 * U)
    async with db.tx() as c:
        sb = await core.open_slot(c, ctx, d["id"], B, 200 * U)
    async with db.tx() as c:
        await core.record_incoming(c, ctx, "txA", 0, "TFrom", PAY, sa["amount_micro"], ts())
    [ap] = await approvals(db, "deposit")
    async with db.tx() as c:
        await core.decide(c, ctx, ap["id"], ADMIN, True)
    await db.execute("UPDATE deposit_slots SET expires_at=now()-interval '1 hour' WHERE id=$1", sb["id"])
    await db.execute("UPDATE deals SET deadline=now()-interval '1 minute' WHERE id=$1", d["id"])
    async with db.tx() as c:
        assert await core.expire_due(c, ctx) == 1
    await give_addresses(db, ctx, d)
    [ap] = await approvals(db, "refund")
    async with db.tx() as c:
        await core.decide(c, ctx, ap["id"], ADMIN, True)
    [p] = await db.fetch("SELECT * FROM payouts WHERE deal_id=$1 AND status='approved'", d["id"])
    async with db.tx() as c:
        await core.mark_payout_sent(c, ctx, p["id"], HASH, ADMIN)
    assert await state(db, d) == "CANCELLED"
    async with db.tx() as c:
        assert await core.record_incoming(c, ctx, "txB", 0, "TFrom", PAY, sb["amount_micro"], ts()) == "unmatched"
    async with db.tx() as c:
        await core.assign_deposit(c, ctx, "txB", 0, sb["id"], ADMIN)
    assert await state(db, d) == "REFUND"
    new = await db.fetch("SELECT * FROM payouts WHERE deal_id=$1 AND status='need_address'", d["id"])
    assert [(p["tg_id"], p["gross_micro"]) for p in new] == [(B, sb["amount_micro"])]


async def test_one_sided_cancel_keeps_inflight_deposit(users, ctx):
    """#5: A 가 보내는 중에 B 혼자 취소 → 안내 시간 안에 들어온 A 돈은 이 거래 환불로 잡힘."""
    db = users
    d = await to_choose(db, ctx)
    async with db.tx() as c:
        await core.choose(c, ctx, d["id"], A, True)
    async with db.tx() as c:
        await core.choose(c, ctx, d["id"], B, False)
    async with db.tx() as c:
        s = await core.open_slot(c, ctx, d["id"], A, 100 * U)
    async with db.tx() as c:
        await core.request_cancel(c, ctx, d["id"], B)
    assert await state(db, d) == "CANCELLED"
    async with db.tx() as c:
        assert await core.record_incoming(c, ctx, "txA", 0, "TFrom", PAY, s["amount_micro"], ts()) == "matched"
    assert await state(db, d) == "REFUND"
    [p] = await db.fetch("SELECT * FROM payouts WHERE deal_id=$1", d["id"])
    assert p["tg_id"] == A and p["gross_micro"] == s["amount_micro"]


async def test_fee_eats_remaining_does_not_crash(users, ctx):
    """#6: 남은 돈 = 수수료 → 정산 동의가 터지지 않고 그대로 끝."""
    db = users
    d, _ = await to_funded(db, ctx, 50 * U)
    rem = 50 * U + 100 - 20 * U
    async with db.tx() as c:
        po = await core.partial(c, ctx, d["id"], A, rem)
    async with db.tx() as c:
        await core.set_address(c, ctx, po["id"], B, ADDR)
    [ap] = await approvals(db, "partial")
    async with db.tx() as c:
        await core.decide(c, ctx, ap["id"], ADMIN, True)
    async with db.tx() as c:
        await core.mark_payout_sent(c, ctx, po["id"], HASH, ADMIN)
    await finish_split(db, ctx, d, "each")
    assert await state(db, d) == "DONE"
    fee_row = await db.fetchrow("SELECT * FROM payouts WHERE deal_id=$1 AND kind='final'", d["id"])
    assert fee_row["amount_micro"] == 0 and fee_row["fee_micro"] == 20 * U and fee_row["status"] == "confirmed"


async def test_dispute_plan_cancels_waiting_slots(users, ctx):
    """#7: 분배로 분쟁을 풀면 기다리던 입금 자리는 취소 (그 뒤 온 돈은 '맞는 거래 없음' → 관리자가 처리)."""
    db = users
    d = await to_choose(db, ctx)
    for who in (A, B):
        async with db.tx() as c:
            await core.choose(c, ctx, d["id"], who, True)
    async with db.tx() as c:
        sa = await core.open_slot(c, ctx, d["id"], A, 100 * U)
    async with db.tx() as c:
        sb = await core.open_slot(c, ctx, d["id"], B, 100 * U)
    async with db.tx() as c:
        await core.record_incoming(c, ctx, "txA", 0, "TFrom", PAY, sa["amount_micro"], ts())
    async with db.tx() as c:
        await core.open_dispute(c, ctx, d["id"], A)
    async with db.tx() as c:
        await core.resolve_dispute(c, ctx, d["id"], ADMIN, {A: sa["amount_micro"], B: 0})
    assert (await db.fetchrow("SELECT status FROM deposit_slots WHERE id=$1", sb["id"]))["status"] == "cancelled"


async def test_watcher_keeps_going_after_bad_tx(users):
    """#8: tx 하나가 오류여도 다음 tx 는 처리 + 관리자 알림."""
    db = users
    from test_watch_admin import FakeTron, info, SENDER
    from escrow import tron
    pay = tron.USDT_CONTRACT
    wctx = core.Ctx(Config(admin_ids=frozenset({ADMIN}), pay_address=pay))
    ft = FakeTron()
    now_ms = int(time.time() * 1000)
    ft.cands = [("bad", now_ms), ("good", now_ms + 1)]
    ft.infos["bad"] = {**info("bad", SENDER, pay, 5 * U), "log": [{"address": "zz", "topics": None}]}   # 깨진 로그
    ft.infos["good"] = info("good", SENDER, pay, 7 * U, ts_ms=now_ms + 1)
    orig = core.record_incoming

    async def boom(c, ctx_, tx_id, *a, **k):
        if tx_id == "good2":
            raise RuntimeError("x")
        return await orig(c, ctx_, tx_id, *a, **k)
    ft.cands.insert(1, ("good2", now_ms))
    ft.infos["good2"] = info("good2", SENDER, pay, 9 * U, ts_ms=now_ms)
    core.record_incoming = boom
    try:
        await watcher.scan_once(db, wctx, ft, set(), set())
    finally:
        core.record_incoming = orig
    assert await db.fetchval("SELECT count(*) FROM chain_tx WHERE tx_id='good'") == 1
    assert await db.fetchval("SELECT count(*) FROM outbox WHERE text LIKE '%good2%'") >= 1


async def test_manual_hash_then_watcher_sees_same_tx(users, ctx):
    """#8: 관리자가 해시로 먼저 완료 처리한 송금을 감시가 나중에 봐도 터지지 않음."""
    db = users
    d, _ = await to_funded(db, ctx, 100 * U)
    await finish_split(db, ctx, d, "b")
    await give_addresses(db, ctx, d)
    [ap] = await approvals(db, "final")
    async with db.tx() as c:
        await core.decide(c, ctx, ap["id"], ADMIN, True)
    [p] = await db.fetch("SELECT * FROM payouts WHERE deal_id=$1", d["id"])
    async with db.tx() as c:
        await core.mark_payout_sent(c, ctx, p["id"], HASH, ADMIN)
    async with db.tx() as c:
        assert await core.record_outgoing(c, ctx, HASH, 0, PAY, ADDR, p["amount_micro"], ts()) == "matched"


async def test_blacklist_lookup_failure_forces_manual(users, ctx):
    """#10: 동결 조회 실패(None) → 자동 모드여도 관리자 확인."""
    db = users
    async with db.tx() as c:
        await core.set_setting(c, "mode.deposit", "auto", ADMIN)
    d = await to_choose(db, ctx)
    async with db.tx() as c:
        await core.choose(c, ctx, d["id"], A, True)
    async with db.tx() as c:
        await core.choose(c, ctx, d["id"], B, False)
    async with db.tx() as c:
        s = await core.open_slot(c, ctx, d["id"], A, 100 * U)
    async with db.tx() as c:
        await core.record_incoming(c, ctx, "t1", 0, "TFrom", PAY, s["amount_micro"], ts(), None)
    assert len(await approvals(db, "deposit")) == 1


async def test_dust_is_recorded_silently(users, ctx):
    """#11: 1 USDT 미만 + 맞는 거래 없음 → 기록만, 관리자 알림 없음."""
    db = users
    async with db.tx() as c:
        assert await core.record_incoming(c, ctx, "dust", 0, "TFrom", PAY, 1, ts()) == "ignored"
        assert await core.record_outgoing(c, ctx, "dust2", 0, PAY, "TFake", 0, ts()) == "ignored"
    assert not await db.fetchval("SELECT count(*) FROM outbox")


async def test_invite_link_only_for_named_user(users, ctx):
    """#12: 링크(join_코드)는 신청 때 입력한 @아이디 본인만."""
    db = users
    async with db.tx() as c:
        d = await core.create_deal(c, ctx, A, None, "bobby")
    async with db.tx() as c:
        with pytest.raises(core.Refused, match="전용"):
            await core.claim_invite(c, ctx, d["code"], C)
    async with db.tx() as c:
        await core.claim_invite(c, ctx, d["code"], B)
    assert any("링크로 들어왔어요" in r["text"] for r in await db.fetch("SELECT text FROM outbox WHERE chat_id=$1", A))


async def test_assign_refuses_second_open_slot_cleanly(users, ctx):
    """#13: 같은 사람이 이미 열린 자리가 있으면 오류(500) 대신 이유와 함께 거절."""
    db = users
    d = await to_choose(db, ctx)
    async with db.tx() as c:
        await core.choose(c, ctx, d["id"], A, True)
    async with db.tx() as c:
        await core.choose(c, ctx, d["id"], B, False)
    async with db.tx() as c:
        s1 = await core.open_slot(c, ctx, d["id"], A, 100 * U)
    async with db.tx() as c:
        s2 = await core.open_slot(c, ctx, d["id"], A, 150 * U)       # 금액 바꿈 → s1 취소
    async with db.tx() as c:
        await core.record_incoming(c, ctx, "late", 0, "TFrom", PAY, 99 * U, ts())
    async with db.tx() as c:
        with pytest.raises(core.Refused, match="이미 다른 입금 자리"):
            await core.assign_deposit(c, ctx, "late", 0, s1["id"], ADMIN)
    assert s2["status"] == "waiting"
