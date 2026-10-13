"""2차 점검(docs: 계획 C·U·O 표) 재현 테스트. 고친 줄을 되돌리면 각 테스트가 실패해야 함."""
from datetime import timedelta

from conftest import A, ADMIN, B, PAY, ts
from escrow import core
from test_core import U, to_choose


async def _deposit_stage(db, ctx):
    """A 만 맡기는 거래를 DEPOSIT 까지."""
    d = await to_choose(db, ctx)
    async with db.tx() as c:
        await core.choose(c, ctx, d["id"], A, True)
    async with db.tx() as c:
        await core.choose(c, ctx, d["id"], B, False)
    return d


async def _slot(db, ctx, deal_id, base=100 * U):
    async with db.tx() as c:
        return await core.open_slot(c, ctx, deal_id, A, base)


async def _age(db, slot_id, minutes):
    """자리를 minutes 분 전에 만든 것처럼 (안내 시간이 지난 상황 재현)."""
    await db.execute("UPDATE deposit_slots SET created_at=created_at - $2::interval, expires_at=expires_at - $2::interval, "
                     "updated_at=updated_at - $2::interval WHERE id=$1", slot_id, timedelta(minutes=minutes))


async def test_c1_ended_slot_amount_is_not_reused(users, ctx):
    """A 거래 자리가 만료된 직후 B 거래가 같은 끝자리 금액을 받으면, A 의 늦은 돈이 B 거래로 잡힌다 → 24시간 재사용 금지."""
    db = users
    d1 = await _deposit_stage(db, ctx)
    s1 = await _slot(db, ctx, d1["id"])
    await _age(db, s1["id"], 45)                       # 30분 안내 + 10분 여유 지남
    async with db.tx() as c:
        await core.expire_due(c, ctx)
    assert (await db.fetchval("SELECT status FROM deposit_slots WHERE id=$1", s1["id"])) == "expired"
    d2 = await _deposit_stage(db, ctx)
    s2 = await _slot(db, ctx, d2["id"])
    assert s2["amount_micro"] != s1["amount_micro"], "끝난 자리 금액을 바로 다시 줌"
    # A 가 예전 안내 금액을 이제야 보냄 → d2 로 잡히면 안 됨
    async with db.tx() as c:
        r = await core.record_incoming(c, ctx, "late1", 0, "TFromA", PAY, s1["amount_micro"], ts())
    assert r == "unmatched"
    assert (await db.fetchval("SELECT status FROM deposit_slots WHERE id=$1", s2["id"])) == "waiting"


async def test_c1_payment_before_slot_existed_is_not_matched(users, ctx):
    """자리가 생기기 전 시각의 입금(다른 사람이 우연히 같은 금액)은 그 자리로 안 잡힘."""
    db = users
    d = await _deposit_stage(db, ctx)
    s = await _slot(db, ctx, d["id"])
    async with db.tx() as c:
        r = await core.record_incoming(c, ctx, "early1", 0, "TOther", PAY, s["amount_micro"], ts() - timedelta(minutes=20))
    assert r == "unmatched"


async def test_c3_watcher_lag_still_matches_expired_slot(users, ctx):
    """감시가 늦어 자리가 먼저 만료돼도, 안내 시간 안에 보낸 돈이면 그 거래로 인정."""
    db = users
    d = await _deposit_stage(db, ctx)
    s = await _slot(db, ctx, d["id"])
    await _age(db, s["id"], 45)
    async with db.tx() as c:
        await core.expire_due(c, ctx)
    sent_at = ts() - timedelta(minutes=40)              # 안내 시간 안 (만든 지 5분 뒤)
    async with db.tx() as c:
        r = await core.record_incoming(c, ctx, "lag1", 0, "TFromA", PAY, s["amount_micro"], sent_at)
    assert r == "matched"
    assert (await db.fetchval("SELECT status FROM deposit_slots WHERE id=$1", s["id"])) == "received"


async def test_c5_assign_after_watcher_matched_is_refused(users, ctx):
    """관리자가 '확인 필요' 입금을 자리에 연결하려는 사이 감시가 그 자리를 먼저 잡았으면 → 연결 거부 (돈 한 건이 장부에서 사라지지 않게)."""
    db = users
    d = await _deposit_stage(db, ctx)
    s = await _slot(db, ctx, d["id"])
    async with db.tx() as c:
        assert await core.record_incoming(c, ctx, "odd1", 0, "TFromA", PAY, 99 * U, ts()) == "unmatched"
    async with db.tx() as c:
        assert await core.record_incoming(c, ctx, "ok1", 0, "TFromA", PAY, s["amount_micro"], ts()) == "matched"
    try:
        async with db.tx() as c:
            await core.assign_deposit(c, ctx, "odd1", 0, s["id"], ADMIN)
        raise AssertionError("이미 잡힌 자리에 또 연결됨")
    except core.Refused:
        pass
    assert (await db.fetchval("SELECT status FROM chain_tx WHERE tx_id='odd1'")) == "unmatched"


from test_core import approvals, finish_split, give_addresses, to_funded   # noqa: E402
from conftest import ADDR   # noqa: E402


async def _approve_all(db, ctx, kind):
    for ap in await approvals(db, kind):
        async with db.tx() as c:
            await core.decide(c, ctx, ap["id"], ADMIN, True)


async def _send_all(db, ctx, deal_id, tx="out"):
    for i, p in enumerate(await db.fetch("SELECT * FROM payouts WHERE deal_id=$1 AND status='approved' ORDER BY id", deal_id)):
        async with db.tx() as c:
            assert await core.record_outgoing(c, ctx, f"{tx}{p['id']}", 0, PAY, p["address"], p["amount_micro"], ts()) == "matched"
    await _approve_all(db, ctx, "payout_done")


async def test_c2_admin_can_cancel_approved_payout_and_reask_address(users, ctx):
    db = users
    d, _ = await to_funded(db, ctx, 1000 * U, 500 * U)
    await finish_split(db, ctx, d, "each")
    await give_addresses(db, ctx, d)
    await _approve_all(db, ctx, "final")
    po = await db.fetchrow("SELECT * FROM payouts WHERE deal_id=$1 AND tg_id=$2 AND status='approved'", d["id"], A)
    async with db.tx() as c:
        new = await core.cancel_payout(c, ctx, po["id"], ADMIN, "주소 오타")
    assert (await db.fetchval("SELECT status FROM payouts WHERE id=$1", po["id"])) == "cancelled"
    assert new["status"] == "need_address" and new["amount_micro"] == po["amount_micro"] and new["debits"] == po["debits"]
    # 장부: 취소 안 된 지급의 합 = 맡긴 돈 그대로
    gross = await db.fetchval("SELECT sum(gross_micro) FROM payouts WHERE deal_id=$1 AND status<>'cancelled'", d["id"])
    assert gross == await db.fetchval("SELECT sum(amount_micro) FROM deposit_slots WHERE deal_id=$1 AND status='confirmed'", d["id"])
    async with db.tx() as c:
        await core.set_address(c, ctx, new["id"], A, ADDR)
    await _approve_all(db, ctx, "final")
    await _send_all(db, ctx, d["id"])
    assert (await db.fetchval("SELECT state FROM deals WHERE id=$1", d["id"])) == "DONE"


async def test_c6_batch_transfer_matches_both_payouts(users, ctx):
    """지갑에서 한 번에 두 건 보냄 (한 tx 에 Transfer 2개) → 둘 다 연결."""
    db = users
    d, _ = await to_funded(db, ctx, 1000 * U, 500 * U)
    await finish_split(db, ctx, d, "each")
    await give_addresses(db, ctx, d)
    await _approve_all(db, ctx, "final")
    p1, p2 = await db.fetch("SELECT * FROM payouts WHERE deal_id=$1 AND status='approved' ORDER BY id", d["id"])
    async with db.tx() as c:
        assert await core.record_outgoing(c, ctx, "batch", 0, PAY, p1["address"], p1["amount_micro"], ts()) == "matched"
    async with db.tx() as c:
        assert await core.record_outgoing(c, ctx, "batch", 1, PAY, p2["address"], p2["amount_micro"], ts()) == "matched"
    assert [r["status"] for r in await db.fetch("SELECT status FROM payouts WHERE id = ANY($1) ORDER BY id", [p1["id"], p2["id"]])] == ["sent", "sent"]


async def test_c4_partial_then_cancel_still_pays_fee(users, ctx):
    db = users
    d, _ = await to_funded(db, ctx, 1000 * U, 0)
    async with db.tx() as c:
        po = await core.partial(c, ctx, d["id"], A, 980 * U)
    async with db.tx() as c:
        await core.set_address(c, ctx, po["id"], B, ADDR)
    await _approve_all(db, ctx, "partial")
    await _send_all(db, ctx, d["id"], "p")
    for who in (A, B):
        async with db.tx() as c:
            await core.request_cancel(c, ctx, d["id"], who)
    fee = await db.fetchval("SELECT sum(fee_micro) FROM payouts WHERE deal_id=$1 AND kind='refund'", d["id"])
    assert fee == 20 * U, "부분 지급 뒤 취소하면 수수료 0 이 됨"


async def test_c8_late_deposit_after_dispute_decision_is_refunded(users, ctx):
    """분쟁 결정(B 자리 취소) 뒤 B 가 늦게 보낸 돈을 관리자가 연결 → 정산 끝날 때 그 돈은 B 에게 환불 (DONE 에 돈이 남지 않게)."""
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
        await core.record_incoming(c, ctx, "ina", 0, "TA", PAY, sa["amount_micro"], ts())
    await _approve_all(db, ctx, "deposit")
    async with db.tx() as c:
        await core.open_dispute(c, ctx, d["id"], A)
    async with db.tx() as c:
        await core.resolve_dispute(c, ctx, d["id"], ADMIN, {A: sa["amount_micro"], B: 0})
    async with db.tx() as c:
        assert await core.record_incoming(c, ctx, "inb", 0, "TB", PAY, sb["amount_micro"], ts()) == "unmatched"
    async with db.tx() as c:
        await core.assign_deposit(c, ctx, "inb", 0, sb["id"], ADMIN)
    await give_addresses(db, ctx, d)
    await _approve_all(db, ctx, "final")
    await _send_all(db, ctx, d["id"])
    d2 = await db.fetchrow("SELECT * FROM deals WHERE id=$1", d["id"])
    assert d2["state"] == "REFUND"
    assert await db.fetchval("SELECT count(*) FROM payouts WHERE deal_id=$1 AND kind='refund' AND tg_id=$2", d["id"], B) == 1


async def test_c10_dispute_without_money_ends_cancelled(users, ctx):
    db = users
    d = await to_choose(db, ctx)
    async with db.tx() as c:
        await core.open_dispute(c, ctx, d["id"], A)
    async with db.tx() as c:
        await core.resolve_dispute(c, ctx, d["id"], ADMIN, {A: 0, B: 0})
    assert (await db.fetchval("SELECT state FROM deals WHERE id=$1", d["id"])) == "CANCELLED"


async def test_c13_open_slots_per_person_are_limited(users, ctx):
    db = users
    for i in range(core.MAX_OPEN_SLOTS + 1):
        d = await _deposit_stage(db, ctx)
        if i < core.MAX_OPEN_SLOTS:
            await _slot(db, ctx, d["id"])
        else:
            try:
                await _slot(db, ctx, d["id"])
                raise AssertionError("자리 수 제한 없음")
            except core.Refused:
                pass


async def test_c14_room_never_sees_decimal_amount(users, ctx):
    db = users
    d, slots = await to_funded(db, ctx, 1000 * U, 0, room=True)
    exact = core.fmt(slots[A]["amount_micro"])
    room_texts = [r["text"] or "" for r in await db.fetch("SELECT text FROM outbox WHERE chat_id=-1001")]
    deposit_msgs = [t for t in room_texts if "예치 완료" not in t]      # 시작 안내의 합계는 실제 맡긴 돈 그대로 보여 줌
    assert deposit_msgs and not any(exact in t for t in deposit_msgs)


def test_o3_bad_pay_address_blocks_deposits():
    from escrow.config import Config
    bad = Config(pay_address="TR7sTzwA9nVfDiSyge8Ez7GJ6jFng7tVC2")      # 마지막 글자 오타 → 체크섬 틀림
    good = Config(pay_address="TR7sTzwA9nVfDiSyge8Ez7GJ6jFng7tVC1")
    assert not bad.can_take_deposits and "PAY_ADDRESS(형식 틀림)" in bad.stub_fields
    assert good.can_take_deposits


async def test_o4_failed_tx_is_retried_before_cursor_moves(users):
    """처리 오류 tx 는 커서를 그 앞에 세워 두고 다시 시도 → 고쳐지면 자동 처리 (예전엔 커서가 넘어가 영영 빠짐)."""
    import time as _t
    from conftest import ADMIN as _ADMIN
    from escrow import tron, watcher
    from escrow.config import Config
    from test_watch_admin import FakeTron, SENDER, info
    db = users
    pay = tron.USDT_CONTRACT
    wctx = core.Ctx(Config(admin_ids=frozenset({_ADMIN}), pay_address=pay))
    ft = FakeTron()
    now_ms = int(_t.time() * 1000)
    ft.cands = [("t1", now_ms)]
    ft.infos["t1"] = info("t1", SENDER, pay, 9 * U, ts_ms=now_ms)
    orig = core.record_incoming
    calls = {"n": 0}

    async def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("잠깐 DB 오류")
        return await orig(*a, **k)
    core.record_incoming = flaky
    failed, done, tries = set(), set(), {}
    try:
        await watcher.set_cursor(db, now_ms - 1000)
        before = await watcher.get_cursor(db)
        await watcher.scan_once(db, wctx, ft, failed, done, tries)
        assert await watcher.get_cursor(db) == before, "실패한 tx 를 두고 커서가 넘어감"
        await watcher.scan_once(db, wctx, ft, failed, done, tries)
    finally:
        core.record_incoming = orig
    assert await db.fetchval("SELECT count(*) FROM chain_tx WHERE tx_id='t1'") == 1 and not failed


async def test_o11_settings_are_strict(users):
    db = users
    for key, bad in (("new_deals", "false"), ("stage_minutes", 0), ("fee_micro", "20"), ("min_micro", 0)):
        try:
            async with db.tx() as c:
                await core.set_setting(c, key, bad, ADMIN)
            raise AssertionError(f"{key}={bad!r} 가 통과됨")
        except core.Refused:
            pass
    async with db.tx() as c:
        await core.set_setting(c, "new_deals", False, ADMIN)
        assert (await core.settings(c))["new_deals"] is False


async def test_d7_check_and_d9_receipt(users, ctx):
    db = users
    d, _ = await to_funded(db, ctx, 1000 * U, 0)
    async with db.tx() as c:
        assert "진짜 거래" in await core.check_deal(c, d["code"].lower(), A)
        assert "@alice" in await core.check_deal(c, d["code"], A)
        assert "@alice" not in await core.check_deal(c, d["code"], 999)
        assert "없어요" in await core.check_deal(c, "ZZZZZZ", A)
    await finish_split(db, ctx, d, "b")
    await give_addresses(db, ctx, d)
    await _approve_all(db, ctx, "final")
    await _send_all(db, ctx, d["id"])
    rec = [r["text"] for r in await db.fetch("SELECT text FROM outbox WHERE chat_id=$1 AND text LIKE '%영수증%'", B)]
    assert rec and "받은 돈" in rec[0] and "tronscan" in rec[0]
