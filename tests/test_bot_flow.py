"""봇 화면 (escrow/bot.py) — 가짜 텔레그램으로 버튼·글자 입력을 실제 순서대로 눌러 끝까지."""
import time
from types import SimpleNamespace

from telegram.error import Forbidden

from conftest import ADDR, ADMIN, A, B, C, PAY
from escrow import bot as B_
from escrow import core
from escrow.config import Config
from escrow.money import MICRO


class FakeBot:
    def __init__(self):
        self.sent = []          # (chat_id, text, markup)
        self.calls = []         # (method, args)
        self.blocked = set()
        self._mid = 0

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None, disable_web_page_preview=None, **kw):
        if chat_id in self.blocked:
            raise Forbidden("bot was blocked by the user")
        self._mid += 1
        self.sent.append((chat_id, text, reply_markup))
        return SimpleNamespace(message_id=self._mid)

    async def send_photo(self, *a, **k):
        return await self.send_message(a[0], k.get("caption", ""), reply_markup=k.get("reply_markup"))

    async def create_chat_invite_link(self, chat_id, **kw):
        self.calls.append(("link", chat_id, kw))
        return SimpleNamespace(invite_link=f"https://t.me/+room{chat_id}")

    async def revoke_chat_invite_link(self, chat_id, link):
        self.calls.append(("revoke", chat_id, link))

    async def ban_chat_member(self, chat_id, uid):
        self.calls.append(("ban", chat_id, uid))

    async def unban_chat_member(self, chat_id, uid, only_if_banned=False):
        self.calls.append(("unban", chat_id, uid, only_if_banned))

    async def delete_messages(self, chat_id, ids):
        self.calls.append(("delete", chat_id, list(ids)))

    async def leave_chat(self, chat_id):
        self.calls.append(("leave", chat_id))

    async def get_chat(self, chat_id):
        return SimpleNamespace(has_visible_history=False)

    def texts(self, chat):
        return [t for c, t, _ in self.sent if c == chat]

    def buttons(self, chat):
        out = []
        for c, _, m in self.sent:
            if c == chat and m:
                out += [b.callback_data for row in m.inline_keyboard for b in row if b.callback_data]
        return out


class Env:
    def __init__(self, db):
        self.cfg = Config(admin_ids=frozenset({ADMIN}), pay_address=PAY, bot_name="테스트에스크로", support="@help",
                          bot_username="testescrowbot")
        self.bot = FakeBot()
        self.db = db
        self.app = SimpleNamespace(bot_data={"db": db, "ctx": core.Ctx(self.cfg), "cfg": self.cfg, "bot_username": "testescrowbot",
                                             "tron": SimpleNamespace(is_blacklisted=self._bl)})
        self.user_data = {}
        self.black = set()

    async def _bl(self, a):
        return a in self.black

    def context(self, uid, args=None):
        return SimpleNamespace(application=self.app, bot=self.bot, user_data=self.user_data.setdefault(uid, {}), args=args or [])

    def user(self, uid):
        return SimpleNamespace(id=uid, is_bot=False, username={A: "alice", B: "bobby", C: "carol", ADMIN: "admin"}[uid],
                               full_name=f"U{uid}")

    async def flush(self):
        for _ in range(5):
            await B_.outbox_job(self.context(0))

    async def start(self, uid, args=None):
        u = self.user(uid)
        upd = SimpleNamespace(effective_user=u, effective_chat=SimpleNamespace(type="private", id=uid),
                              effective_message=SimpleNamespace(), callback_query=None)
        await B_.track_user(upd, self.context(uid))
        await B_.cmd_start(upd, self.context(uid, args))
        await self.flush()

    async def text(self, uid, text):
        u = self.user(uid)
        upd = SimpleNamespace(effective_user=u, effective_chat=SimpleNamespace(type="private", id=uid),
                              effective_message=SimpleNamespace(text=text))
        await B_.on_private_text(upd, self.context(uid))
        await self.flush()

    async def press(self, uid, data):
        answers = []

        async def answer(text=None, show_alert=False):
            answers.append(text)

        async def noop(*a, **k):
            return None
        q = SimpleNamespace(data=data, from_user=self.user(uid), answer=answer, edit_message_text=noop,
                            edit_message_reply_markup=noop, message=SimpleNamespace(photo=None, text_html="x", reply_text=noop))
        upd = SimpleNamespace(callback_query=q, effective_user=q.from_user)
        await B_.track_user(upd, self.context(uid))
        await B_.on_callback(upd, self.context(uid))
        await self.flush()
        return answers

    def last_button(self, chat, prefix):
        return [b for b in self.bot.buttons(chat) if b.startswith(prefix)][-1]


async def test_full_deal_through_buttons(users):
    db = users
    env = Env(db)
    await env.start(A)
    assert "에스크로가 뭐냐면" in env.bot.texts(A)[-1], "처음 오면 튜토리얼"
    await env.press(A, "t:skip")
    await env.start(B)
    await env.press(B, "t:skip")
    await db.execute("INSERT INTO rooms(chat_id, title, state) VALUES(-500, '방', 'free')")
    await env.press(A, "menu:new")
    await env.text(A, "@nobody_here")                  # 봇을 안 켠 상대 → 링크 안내
    assert "start=join_" in env.bot.texts(A)[-1]
    await env.press(A, "menu:new")
    await env.text(A, "@BOBBY")                          # 대소문자 상관없이
    assert "거래 요청" in env.bot.texts(B)[-1]
    await env.press(C, env.last_button(B, "d:acc"))    # 남이 누르면 거절
    await env.press(B, env.last_button(B, "d:acc"))
    assert ("link", -500) == env.bot.calls[0][:2] and env.bot.calls[0][2]["creates_join_request"]
    assert any("t.me/+room-500" in t for t in env.bot.texts(A))
    for who in (A, B):
        async with db.tx() as c:
            assert await core.room_joined(c, env.app.bot_data["ctx"], -500, who)
    await env.flush()
    assert "공지" in env.bot.texts(-500)[-1]
    for who in (A, B):
        await env.press(who, env.last_button(-500, "d:ntc"))
    await env.press(A, env.last_button(-500, "d:dep"))
    await env.press(B, env.last_button(-500, "d:dep"))
    await env.text(A, "1,000")
    await env.text(B, "10")                            # 최소 50 미만
    assert "최소" in env.bot.texts(B)[-1]
    await env.text(B, "500")
    guide_a = [t for t in env.bot.texts(A) if "입금 안내" in t][-1]
    assert PAY in guide_a and "1000.0001" in guide_a
    assert not any(PAY in t for t in env.bot.texts(-500)), "주소는 방에 안 감"
    ctx = env.app.bot_data["ctx"]
    for who, amt in ((A, 1000 * MICRO + 100), (B, 500 * MICRO + 100)):
        async with db.tx() as c:
            await core.record_incoming(c, ctx, f"tx{who}", 0, "TSender", PAY, amt, core.now())
    await env.flush()
    # 관리자: 텔레그램 승인 버튼
    for data in [b for b in env.bot.buttons(ADMIN) if b.startswith("a:ok")]:
        await env.press(ADMIN, data)
    for data in [b for b in env.bot.buttons(ADMIN) if b.startswith("a:ok")]:
        await env.press(ADMIN, data)
    assert "거래 시작" in env.bot.texts(-500)[-1]
    # 부분 지급 → 상대 주소 → 승인 → 감시가 출금 발견
    await env.press(A, env.last_button(-500, "d:part"))
    await env.text(A, "100")
    await env.text(B, "nope")
    assert "34자" in env.bot.texts(B)[-1]
    await env.text(B, ADDR)
    await env.text(B, ADDR)
    for data in [b for b in env.bot.buttons(ADMIN) if b.startswith("a:ok")]:
        await env.press(ADMIN, data)
    async with db.tx() as c:
        assert await core.record_outgoing(c, ctx, "out1", 0, PAY, ADDR, 100 * MICRO, core.now()) == "matched"
    await env.flush()
    for data in [b for b in env.bot.buttons(ADMIN) if b.startswith("a:ok")]:
        await env.press(ADMIN, data)
    assert any("송금 완료" in t for t in env.bot.texts(B))
    # 완료 → 정산 직접 입력 → 동의 → 주소 → 승인 → 송금
    for who in (A, B):
        await env.press(who, env.last_button(-500, "d:fin"))
        await env.press(who, env.last_button(who, "d:finy"))
    await env.press(A, env.last_button(-500, "d:sp:custom"))
    await env.text(A, "700")
    await env.press(B, env.last_button(B, "d:spok"))
    for who in (A, B):
        await env.text(who, ADDR)
        await env.text(who, ADDR)
    for data in [b for b in env.bot.buttons(ADMIN) if b.startswith("a:ok")]:
        await env.press(ADMIN, data)
    pos = await db.fetch("SELECT * FROM payouts WHERE kind='final' ORDER BY tg_id")
    assert sum(p["gross_micro"] for p in pos) == 1500 * MICRO + 200 - 100 * MICRO
    assert pos[0]["gross_micro"] == 700 * MICRO
    for i, p in enumerate(pos):
        async with db.tx() as c:
            await core.record_outgoing(c, ctx, f"fo{i}", 0, PAY, ADDR, p["amount_micro"], core.now())
    await env.flush()
    for data in [b for b in env.bot.buttons(ADMIN) if b.startswith("a:ok")]:
        await env.press(ADMIN, data)
    d = await db.fetchrow("SELECT * FROM deals WHERE partner=$1", B)
    assert d["state"] == "DONE"
    assert (await db.fetchrow("SELECT state FROM deals WHERE partner_hint='nobody_here'"))["state"] == "INVITED"
    kinds = [c[0] for c in env.bot.calls]
    assert {"revoke", "ban", "unban", "delete"} <= set(kinds), kinds
    assert (await db.fetchrow("SELECT state FROM rooms WHERE chat_id=-500"))["state"] == "free"


async def test_mock_trade_walkthrough(users):
    env = Env(users)
    await env.start(A)
    await env.press(A, "t:1")
    await env.press(A, "t:2")
    await env.press(A, "t:end")
    for data in ("m:start", "m:room", "m:ntc", "m:dep", "m:amt:1000", "m:paid", "m:fin", "m:finy", "m:sp"):
        await env.press(A, data)
    txt = "\n".join(env.bot.texts(A))
    assert "연습" in txt and "실제로 보내지 마세요" in txt and "연습 끝" in txt
    assert not await users.fetchval("SELECT count(*) FROM deals"), "연습은 DB 거래 안 만듦"
    assert await users.fetchval("SELECT tutorial_done FROM users WHERE tg_id=$1", A)


async def test_room_registration_only_by_admin(users):
    env = Env(users)
    from telegram import ChatMemberAdministrator, User

    def upd(by, can=True):
        admin_member = ChatMemberAdministrator(
            user=User(1, "bot", True), can_be_edited=False, is_anonymous=False, can_manage_chat=True, can_delete_messages=can,
            can_manage_video_chats=False, can_restrict_members=can, can_promote_members=False, can_change_info=False,
            can_invite_users=can, can_post_stories=False, can_edit_stories=False, can_delete_stories=False)
        cm = SimpleNamespace(chat=SimpleNamespace(id=-900, type="supergroup", title="거래방9"), new_chat_member=admin_member,
                             from_user=SimpleNamespace(id=by))
        return SimpleNamespace(my_chat_member=cm)
    await B_.on_my_member(upd(A), env.context(0))
    assert ("leave", -900) in env.bot.calls and not await users.fetchval("SELECT count(*) FROM rooms")
    await B_.on_my_member(upd(ADMIN, can=False), env.context(0))
    assert (await users.fetchrow("SELECT state FROM rooms"))["state"] == "broken"
    await B_.on_my_member(upd(ADMIN), env.context(0))
    assert (await users.fetchrow("SELECT state FROM rooms"))["state"] == "free"


async def test_outbox_survives_blocked_user_and_keeps_order(users):
    env = Env(users)
    async with users.tx() as c:
        await core.say(c, A, "첫째")
        await core.say(c, B, "막힌 사람")
        await core.say(c, A, "둘째")
    env.bot.blocked.add(B)
    await env.flush()
    assert env.bot.texts(A) == ["첫째", "둘째"]
    row = await users.fetchrow("SELECT * FROM outbox WHERE chat_id=$1", B)
    assert row["sent_at"] and "막힘" in row["error"], "막힌 사람 때문에 줄이 멈추지 않음"


async def test_banned_user_cannot_start_deal(users):
    env = Env(users)
    await env.start(A)
    await users.execute("UPDATE users SET banned=TRUE WHERE tg_id=$1", A)
    ans = await env.press(A, "menu:new")
    assert ans and "제한" in ans[-1]


async def test_address_blacklist_and_mismatch(users, ctx):
    env = Env(users)
    from test_core import finish_split, to_funded
    d, _ = await to_funded(users, env.app.bot_data["ctx"], 100 * MICRO, 0)
    await finish_split(users, env.app.bot_data["ctx"], d, "b")
    await env.flush()
    env.black.add(ADDR)
    await env.text(B, ADDR)
    assert "동결" in env.bot.texts(B)[-1]
    env.black.clear()
    await env.text(B, ADDR)
    await env.text(B, "TLa2f6VPqDgRE67v1736s7bJ8Ray5wYjU7")
    assert "달라요" in env.bot.texts(B)[-1]
    await env.text(B, ADDR)
    await env.text(B, ADDR)
    assert (await users.fetchrow("SELECT status FROM payouts"))["status"] == "review"
