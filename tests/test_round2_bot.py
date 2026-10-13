"""2차 점검 봇 화면(U) 재현 테스트 — 가짜 텔레그램(test_bot_flow.Env)으로."""
from conftest import ADDR, ADMIN, A, B, PAY
from escrow import core
from escrow.money import MICRO
from test_bot_flow import Env


async def _to_room_notice(env, db):
    for who in (A, B):
        await env.start(who)
        await env.press(who, "t:skip")
    await db.execute("INSERT INTO rooms(chat_id, title, state) VALUES(-500, '방', 'free')")
    await env.press(A, "menu:new")
    await env.text(A, "@bobby")
    await env.press(B, env.last_button(B, "d:acc"))
    for who in (A, B):
        async with db.tx() as c:
            assert await core.room_joined(c, env.app.bot_data["ctx"], -500, who)
    await env.flush()
    return await db.fetchrow("SELECT * FROM deals ORDER BY id DESC LIMIT 1")


async def _to_funded(env, db):
    d = await _to_room_notice(env, db)
    for who in (A, B):
        await env.press(who, env.last_button(-500, "d:ntc"))
    await env.press(A, env.last_button(-500, "d:dep"))
    await env.press(B, env.last_button(-500, "d:dep"))
    await env.text(A, "1000")
    await env.text(B, "500")
    ctx = env.app.bot_data["ctx"]
    for who in (A, B):
        s = await db.fetchrow("SELECT * FROM deposit_slots WHERE tg_id=$1 AND status='waiting'", who)
        async with db.tx() as c:
            await core.record_incoming(c, ctx, f"tx{who}", 0, "TSender", PAY, s["amount_micro"], core.now())
    await env.flush()
    for _ in range(2):
        for data in [b for b in env.bot.buttons(ADMIN) if b.startswith("a:ok")]:
            await env.press(ADMIN, data)
    return d


async def test_u1_bot_kicked_from_room_moves_deal_to_dm(users):
    db = users
    env = Env(db)
    d = await _to_room_notice(env, db)
    env.bot.blocked.add(-500)                       # 봇이 방에서 쫓겨남 → 방에 보내면 Forbidden
    await env.press(A, env.last_button(-500, "d:ntc"))
    d2 = await db.fetchrow("SELECT * FROM deals WHERE id=$1", d["id"])
    assert d2["room_id"] is None, "고장 난 방에 계속 붙어 있음"
    assert (await db.fetchval("SELECT state FROM rooms WHERE chat_id=-500")) == "broken"
    for who in (A, B):
        assert any("1:1 에서 이어갈게요" in t for t in env.bot.texts(who))
        assert any(b.startswith("d:ntc") for b in env.bot.buttons(who)), "1:1 로 지금 단계 버튼이 안 감"


async def test_u2_room_link_can_be_requested_again(users):
    db = users
    env = Env(db)
    d = await _to_room_notice(env, db)
    await env.press(A, "menu:active")
    assert f"d:link:{d['id']}" in env.bot.buttons(A)
    before = len([c for c in env.bot.calls if c[0] == "link"])
    await env.press(A, f"d:link:{d['id']}")
    assert len([c for c in env.bot.calls if c[0] == "link"]) == before + 1
    assert "링크를 새로" in env.bot.texts(A)[-1]


async def test_u3_amount_can_be_reentered(users):
    db = users
    env = Env(db)
    d = await _to_room_notice(env, db)
    for who in (A, B):
        await env.press(who, env.last_button(-500, "d:ntc"))
    await env.press(A, env.last_button(-500, "d:dep"))
    await env.press(B, env.last_button(-500, "d:nodep"))
    await env.text(A, "10000")                       # 오타
    await env.press(A, env.last_button(A, "d:amt"))
    await env.text(A, "1000")
    s = await db.fetch("SELECT status, base_micro FROM deposit_slots WHERE deal_id=$1 ORDER BY id", d["id"])
    assert [(r["status"], r["base_micro"]) for r in s] == [("cancelled", 10000 * MICRO), ("waiting", 1000 * MICRO)]


async def test_u4_custom_split_asks_presser_own_share(users):
    """상대(B)가 '직접 입력'을 누르고 자기 몫 1000 을 적으면 → B 가 1000 받는 안 (예전엔 신청자 몫으로 들어감)."""
    db = users
    env = Env(db)
    d = await _to_funded(env, db)
    for who in (A, B):
        await env.press(who, env.last_button(-500, "d:fin"))
        await env.press(who, env.last_button(who, "d:finy"))
    await env.press(B, env.last_button(-500, "d:sp:custom"))
    assert "본인" in env.bot.texts(B)[-1]
    await env.text(B, "1000")
    sp = await db.fetchrow("SELECT plan FROM splits WHERE deal_id=$1 AND kind='final' ORDER BY id DESC LIMIT 1", d["id"])
    assert int(sp["plan"][str(B)]) == 1000 * MICRO


async def test_u5_old_username_is_released(users):
    db = users
    async with db.tx() as c:
        await core.upsert_user(c, 777, "bobby", "새 주인")      # B 가 버린 아이디를 다른 사람이 가져감
    assert await db.fetchval("SELECT username FROM users WHERE tg_id=$1", B) is None
    assert await db.fetchval("SELECT tg_id FROM users WHERE lower(username)='bobby'") == 777


async def test_u7_partner_who_never_opened_dm_gets_link(users):
    db = users
    env = Env(db)
    await env.start(A)
    await env.press(A, "t:skip")
    await env.press(A, "menu:new")
    await env.text(A, "@bobby")                      # B 는 DB 엔 있지만 봇과 1:1 을 연 적 없음
    assert "start=join_" in env.bot.texts(A)[-1]
    assert any(b.startswith("d:cx:") for b in env.bot.buttons(A)), "요청 취소 버튼 없음"


async def test_u12_banned_user_input_is_dropped(users):
    db = users
    env = Env(db)
    await env.start(A)
    await env.press(A, "t:skip")
    await env.press(A, "menu:new")
    await db.execute("UPDATE users SET banned=TRUE WHERE tg_id=$1", A)
    await env.text(A, "@bobby")
    assert await db.fetchval("SELECT count(*) FROM deals") == 0
    assert "제한" in env.bot.texts(A)[-1] and not await db.fetchval("SELECT 1 FROM inputs WHERE tg_id=$1", A)


async def test_u13_split_buttons_show_plain_names(users):
    rows = core.kb_split(1, "@a&amp;b", "Tom &lt;x&gt;")
    labels = [b[0] for row in rows for b in row]
    assert "전액 @a&b 에게" in labels and "전액 Tom <x> 에게" in labels


async def test_u15_group_upgrade_keeps_room(users):
    db = users
    await db.execute("INSERT INTO rooms(chat_id, title, state) VALUES(-11, '방', 'free')")
    async with db.tx() as c:
        await core.room_migrated(c, -11, -10011)
    assert await db.fetchval("SELECT title FROM rooms WHERE chat_id=-10011") == "방"
    assert not await db.fetchval("SELECT 1 FROM rooms WHERE chat_id=-11")


async def test_d6_invite_shows_numeric_id_and_history(users):
    db = users
    env = Env(db)
    for who in (A, B):
        await env.start(who)
        await env.press(who, "t:skip")
    await env.press(A, "menu:new")
    await env.text(A, "@bobby")
    inv = [t for t in env.bot.texts(B) if "거래 요청" in t][-1]
    assert str(A) in inv and "끝낸 거래 0건" in inv
    assert ADDR  # (import 유지)
