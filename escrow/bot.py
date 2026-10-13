"""텔레그램 봇: python -m escrow.bot

- 화면·버튼·1:1 글자 입력 → core 함수 (돈 규칙은 전부 core).
- 보낼 메시지는 core 가 outbox 에 넣고, 여기 outbox_job 이 1초마다 보냄 (거래방 열기·정리도 같은 줄에서).
- 거래방: 관리자가 만든 그룹에 봇을 관리자로 넣으면 풀에 등록 (넣은 사람이 ADMIN_IDS 가 아니면 나감).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone

from telegram import (Chat, ChatMemberAdministrator, InlineKeyboardButton, InlineKeyboardMarkup, Update)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import (Application, CallbackQueryHandler, ChatJoinRequestHandler, ChatMemberHandler, CommandHandler,
                          ContextTypes, MessageHandler, TypeHandler, filters)

from . import config as cfgmod
from . import core, helper, mock
from . import texts as T
from .db import DB
from .money import MICRO, fmt, parse_usdt
from .tron import TronGrid, is_address

log = logging.getLogger("escrow.bot")
_USERNAME = re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{3,31})$")


# ── 공통 ─────────────────────────────────────────────
def markup(rows) -> InlineKeyboardMarkup | None:
    if not rows:
        return None
    out = []
    for row in rows:
        btns = []
        for label, data in row:
            if data.startswith("url:"):
                btns.append(InlineKeyboardButton(label, url=data[4:]))
            else:
                btns.append(InlineKeyboardButton(label, callback_data=data[:64]))
        out.append(btns)
    return InlineKeyboardMarkup(out)


def svc(context: ContextTypes.DEFAULT_TYPE):
    bd = context.application.bot_data
    return bd["db"], bd["ctx"], bd["cfg"]


def support_url(cfg) -> str | None:
    s = cfg.support.strip()
    if s.startswith("@") and len(s) > 2:
        return f"https://t.me/{s[1:]}"
    if s.startswith("https://t.me/"):
        return s
    return None


def main_kb(cfg):
    rows = [[("거래 신청", "menu:new"), ("환불 대기", "menu:refund")],
            [("진행 중인 거래", "menu:active"), ("끝난 거래", "menu:done")],
            [("이용 방법", "menu:tut")]]
    url = support_url(cfg)
    if url:
        rows[-1].append(("고객센터", "url:" + url))
    return rows


async def send(context, chat_id: int, text: str, rows=None, direct: bool = False):
    bot = context if direct else context.bot
    try:
        return await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=markup(rows),
                                      disable_web_page_preview=True)
    except Forbidden:
        log.info("1:1 막힘 %s", chat_id)
    except TelegramError as e:
        log.warning("보내기 실패 %s: %r", chat_id, e)
    return None


async def show_main(context, chat_id: int):
    _, _, cfg = svc(context)
    photo = context.application.bot_data.get("menu_photo")
    if photo:
        try:
            await context.bot.send_photo(chat_id, photo, caption=T.main_menu(cfg.bot_name, cfg.support),
                                         parse_mode=ParseMode.HTML, reply_markup=markup(main_kb(cfg)))
            return
        except TelegramError as e:
            log.warning("메뉴 사진 실패: %r", e)
    await send(context, chat_id, T.main_menu(cfg.bot_name, cfg.support), main_kb(cfg))


async def track_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """모든 업데이트 앞에서: 보낸 사람 기록 (아이디가 바뀌어도 최신으로)."""
    u = update.effective_user
    if not u or u.is_bot:
        return
    db, _, _ = svc(context)
    chat = getattr(update, "effective_chat", None)
    async with db.tx() as c:
        await core.upsert_user(c, u.id, u.username, u.full_name or "", dm=bool(chat and chat.type == Chat.PRIVATE))


async def current_fee(db) -> int:
    async with db.tx() as c:
        return int((await core.settings(c))["fee_micro"])


async def is_banned(db, tg_id: int) -> bool:
    return bool(await db.fetchval("SELECT banned FROM users WHERE tg_id=$1", tg_id))


# ── /start · 튜토리얼 ─────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != Chat.PRIVATE:
        return
    db, ctx, cfg = svc(context)
    uid = update.effective_user.id
    if await is_banned(db, uid):
        await send(context, uid, T.BANNED.format(support=T.esc(cfg.support)))
        return
    await db.execute("DELETE FROM inputs WHERE tg_id=$1 AND kind <> 'address'", uid)
    arg = (context.args[0] if context.args else "").strip()
    if arg.startswith("join_"):
        try:
            async with db.tx() as c:
                await core.claim_invite(c, ctx, arg[5:], uid)
        except core.Refused as e:
            await send(context, uid, str(e))
            await show_main(context, uid)
        return
    done = await db.fetchval("SELECT tutorial_done FROM users WHERE tg_id=$1", uid)
    if not done:
        await send(context, uid, T.tutorial(0, await current_fee(db), cfg.support), tut_kb(0))
    else:
        await show_main(context, uid)
    await remind_addresses(context, uid)


async def remind_addresses(context, uid: int):
    """받을 주소를 아직 안 준 지급이 있으면 다시 알림 (입력 대기가 지워졌어도 버튼으로 다시)."""
    db, _, _ = svc(context)
    async with db.tx() as c:
        rows = await core.pending_addresses(c, uid)
    for po in rows:
        await send(context, uid, f"받을 주소가 아직 없어요. (거래 <code>{po['code']}</code>, {fmt(po['amount_micro'])} USDT)",
                   core.kb_address(po["id"]))


def tut_kb(i: int):
    nav = []
    if i > 0:
        nav.append(("이전", f"t:{i - 1}"))
    if i < len(T.TUTORIAL) - 1:
        nav.append(("다음", f"t:{i + 1}"))
        return [nav, [("건너뛰기", "t:skip")]]
    nav.append(("다 봤어요", "t:end"))
    return [nav]


async def on_tutorial(update: Update, context: ContextTypes.DEFAULT_TYPE, arg: str):
    q = update.callback_query
    db, _, cfg = svc(context)
    uid = q.from_user.id
    if arg in ("skip", "end"):
        await db.execute("UPDATE users SET tutorial_done=TRUE WHERE tg_id=$1", uid)
        await q.answer()
        if arg == "end":
            await edit(q, T.TUTORIAL_END, [[("연습 거래 해보기", "m:start")], [("바로 거래 신청", "menu:new"), ("메뉴로", "menu:home")]])
        else:
            await show_main(context, uid)
        return
    i = max(0, min(int(arg), len(T.TUTORIAL) - 1))
    async with db.tx() as c:
        fee = int((await core.settings(c))["fee_micro"])
    await q.answer()
    await edit(q, T.tutorial(i, fee, cfg.support), tut_kb(i))


async def edit(q, text: str, rows=None):
    try:
        if q.message and getattr(q.message, "photo", None):
            await q.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup(rows), disable_web_page_preview=True)
        else:
            await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup(rows), disable_web_page_preview=True)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            await send(q.get_bot(), q.from_user.id, text, rows, direct=True)


async def drop_buttons(q, note: str | None = None):
    try:
        if note and q.message and getattr(q.message, "text_html", None):
            await q.edit_message_text(q.message.text_html + f"\n\n<i>{T.esc(note)}</i>", parse_mode=ParseMode.HTML,
                                      disable_web_page_preview=True)
        else:
            await q.edit_message_reply_markup(None)
    except TelegramError:
        pass


# ── 연습 거래 ─────────────────────────────────────────
async def on_mock(update: Update, context: ContextTypes.DEFAULT_TYPE, arg: str):
    q = update.callback_query
    db, _, cfg = svc(context)
    uid = q.from_user.id
    st = context.user_data.setdefault("mock", {"mine": 0, "theirs": 1000})
    async with db.tx() as c:
        s = await core.settings(c)
    fee, minimum = int(s["fee_micro"]), int(s["min_micro"])

    async def out(pair, delay: float = 0):
        if delay:
            await asyncio.sleep(delay)
        text, rows = pair
        await send(context, uid, text, rows)

    if arg == "quit":
        context.user_data.pop("mock", None)
        await q.answer("연습을 그만뒀어요.")
        await drop_buttons(q)
        await show_main(context, uid)
        return
    if arg == "nfull":
        await q.answer()
        await send(context, uid, mock.ON_NOTICE_FULL)
        return
    if arg in ("part", "sos"):
        await q.answer(mock.PARTIAL_INFO if arg == "part" else mock.SOS_INFO, show_alert=True)
        return
    await q.answer()
    await drop_buttons(q)
    if arg == "start":
        context.user_data["mock"] = {"mine": 0, "theirs": 1000}
        await out(mock.start())
    elif arg == "room":
        await out(mock.notice(fee, minimum, cfg.support))
    elif arg == "ntc":
        await out(mock.notice_mine())
        await out((mock.partner_line("공지 확인했어요"), None), 1.5)
        await out(mock.choose(), 0.5)
    elif arg == "dep":
        await out(mock.ask_amount())
    elif arg == "nodep":
        st["mine"] = 0
        await out((f"(연습) 예치 안 함을 골랐어요. {mock.PARTNER} 님 입금을 기다려요.", None))
        await out(mock.checking(), 1)
        await out(mock.funded(0, st["theirs"]), 2)
    elif arg.startswith("amt:"):
        st["mine"] = int(arg[4:])
        await out(mock.deposit_guide(st["mine"]))
    elif arg == "paid":
        await out(mock.checking())
        await out(mock.funded(st["mine"], st["theirs"]), 2)
    elif arg == "fin":
        await out(mock.complete_confirm())
    elif arg == "finn":
        await out(mock.funded(st["mine"], st["theirs"]))
    elif arg == "finy":
        await out(mock.complete_wait())
        await out((mock.partner_line("완료 눌렀어요"), None), 1.5)
        await out(mock.split(st["mine"], st["theirs"], fee), 0.5)
    elif arg == "sp":
        await out(mock.finished(), 1)
        context.user_data.pop("mock", None)
        await db.execute("UPDATE users SET tutorial_done=TRUE WHERE tg_id=$1", uid)
        await show_main(context, uid)


# ── 메뉴 ─────────────────────────────────────────────
async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, arg: str):
    q = update.callback_query
    db, ctx, cfg = svc(context)
    uid = q.from_user.id
    if arg == "home":
        await q.answer()
        await show_main(context, uid)
        return
    if arg == "tut":
        await q.answer()
        await send(context, uid, T.tutorial(0, await current_fee(db), cfg.support), tut_kb(0))
        return
    if arg == "new":
        if await is_banned(db, uid):
            await q.answer(T.BANNED.format(support=cfg.support)[:190], show_alert=True)
            return
        async with db.tx() as c:
            if not (await core.settings(c))["new_deals"]:
                await q.answer(T.DEPOSITS_OFF, show_alert=True)
                return
            await core.set_input(c, uid, "partner", None, None, 10)
        await q.answer()
        await send(context, uid, T.ASK_PARTNER, [[("아이디 없는 상대 (링크로 초대)", "menu:link")], [("메뉴로", "menu:home")]])
        await remind_addresses(context, uid)
        return
    if arg == "link":
        if await is_banned(db, uid):
            await q.answer(T.BANNED.format(support=cfg.support)[:190], show_alert=True)
            return
        try:
            async with db.tx() as c:
                d = await core.create_deal(c, ctx, uid, None, None, open_link=True)
                await c.execute("DELETE FROM inputs WHERE tg_id=$1 AND kind='partner'", uid)
        except core.Refused as e:
            await q.answer(str(e)[:190], show_alert=True)
            return
        await q.answer()
        bot_user = context.application.bot_data.get("bot_username") or cfg.bot_username
        await send(context, uid, T.open_link_made(d["code"], f"https://t.me/{bot_user}?start=join_{d['code']}"),
                   [[("요청 취소", f"d:cx:{d['id']}")]])
        return
    kind = {"active": "active", "done": "done", "refund": "refund"}.get(arg)
    if not kind:
        await q.answer()
        return
    async with db.tx() as c:
        rows = await core.user_deals(c, uid, kind)
        lines = []
        for d in rows:
            other = await core.label(c, d["partner"] if d["creator"] == uid else d["creator"])
            total = await core.held(c, d["id"], core.MONEY_HELD)
            lines.append(T.deal_line(d["code"], T.STATE_LABEL.get(d["state"], d["state"]), other, total))
    title = {"active": "진행 중인 거래", "done": "끝난 거래", "refund": "환불 대기"}[kind]
    empty = {"active": "진행 중인 거래가 없어요.", "done": "끝난 거래가 없어요.", "refund": "환불 대기 중인 거래가 없어요."}[kind]
    btns = []
    if kind == "active":
        for d in rows[:8]:
            row = [(f"{d['code']} 지금 단계 보기", f"d:scr:{d['id']}")]
            if d["room_id"]:
                row.append((f"{d['code']} 방 링크", f"d:link:{d['id']}"))
            btns.append(row)
    await q.answer()
    await send(context, uid, f"<b>{title}</b>\n\n" + ("\n".join(lines) if lines else empty), btns + [[("메뉴로", "menu:home")]])


# ── 거래 버튼 ─────────────────────────────────────────
async def on_deal(update: Update, context: ContextTypes.DEFAULT_TYPE, parts: list[str]):
    """거래 버튼. DB 일은 한 트랜잭션에서 끝내고, 텔레그램 응답(answer·버튼 지우기·1:1 안내)은 커밋 뒤에
    (텔레그램 호출이 실패해도 이미 한 일이 되돌아가지 않게 — 재시작 뒤 오래된 버튼 응답 실패 등)."""
    q = update.callback_query
    db, ctx, cfg = svc(context)
    uid = q.from_user.id
    act = parts[0] if parts else ""
    try:
        if act == "sp":                         # d:sp:<how>:<deal>
            how, deal_id = parts[1], int(parts[2])
        else:
            deal_id = int(parts[1])             # d:addr:<지급 id> 는 지급 id
    except (IndexError, ValueError):
        await q.answer(T.STALE)
        return
    if await is_banned(db, uid):
        await q.answer(T.BANNED.format(support=cfg.support)[:190], show_alert=True)
        return
    answer: str | None = None
    drop: tuple[bool, str | None] = (False, None)
    dms: list[tuple[str, list | None]] = []
    relink: tuple[int, str] | None = None
    try:
        async with db.tx() as c:
            if act in ("acc", "dec"):
                await core.answer_invite(c, ctx, deal_id, uid, act == "acc")
                answer = "수락했어요." if act == "acc" else "거절했어요."
                drop = (True, answer)
            elif act == "ntc":
                await core.ack_notice(c, ctx, deal_id, uid)
            elif act == "nfull":
                dms.append((T.NOTICE_FULL, None))
            elif act in ("dep", "nodep"):
                await core.choose(c, ctx, deal_id, uid, act == "dep")
            elif act == "part":
                d = await c.fetchrow("SELECT * FROM deals WHERE id=$1", deal_id)
                if not d:
                    raise core.Refused(T.NOTHING)
                core._party(d, uid)
                core._expect(d, "FUNDED")
                bal = await core.balances(c, deal_id)
                mine = bal.get(uid, 0)
                room_for = max(0, min(mine, sum(bal.values()) - d["fee_micro"]))
                if room_for <= 0:
                    raise core.Refused("먼저 보낼 수 있는 내 잔액이 없어요.")
                await core.set_input(c, uid, "partial", deal_id, None, 10)
                answer = "1:1 로 금액을 물어볼게요."
                dms.append((T.partial_ask(d["code"], await core.label(c, core._other(d, uid)), mine, room_for),
                            [[("그만두기", f"d:inx:{deal_id}")]]))
            elif act == "fin":
                d, _ = await core.press_complete(c, ctx, deal_id, uid, False)
                dms.append((T.complete_confirm(d["code"]), core.kb_complete_confirm(deal_id)))
            elif act == "finn":
                answer, drop = "취소했어요.", (True, None)
            elif act == "finy":
                await core.press_complete(c, ctx, deal_id, uid, True)
                drop = (True, None)
            elif act == "sp" and how == "custom":
                d = await c.fetchrow("SELECT * FROM deals WHERE id=$1", deal_id)
                if not d:
                    raise core.Refused(T.NOTHING)
                core._party(d, uid)
                core._expect(d, "SPLIT")
                total = sum((await core.balances(c, deal_id)).values())
                await core.set_input(c, uid, "custom", deal_id, None, 10)
                answer = "1:1 로 금액을 물어볼게요."
                dms.append((T.ask_custom(d["code"], await core.label(c, core._other(d, uid)), total), None))
            elif act == "sp":
                await core.propose_split(c, ctx, deal_id, uid, how)
            elif act in ("spok", "spno"):
                await core.answer_split(c, ctx, deal_id, int(parts[2]), uid, act == "spok")
                drop = (True, "동의했어요." if act == "spok" else "다시 정하기로 했어요.")
            elif act == "cx":
                await core.request_cancel(c, ctx, deal_id, uid)
            elif act in ("cxok", "cxno"):
                await core.answer_cancel(c, ctx, deal_id, uid, act == "cxok")
                drop = (True, None)
            elif act == "sos":
                await core.open_dispute(c, ctx, deal_id, uid)
            elif act == "addr":
                po = await core.reopen_address(c, deal_id, uid)
                dms.append((T.ask_address(po["code"], po["amount_micro"], 1), None))
            elif act == "rate":
                await core.rate(c, ctx, deal_id, uid, int(parts[2]))
                drop = (True, "★" * int(parts[2]) + " 고마워요.")
            elif act in ("pok", "pno"):         # 아이디 없는 상대: 링크로 들어온 사람 확인
                await core.confirm_claim(c, ctx, deal_id, uid, act == "pok")
                drop = (True, "확인했어요." if act == "pok" else "거절했어요. 링크로 다른 분이 들어올 수 있어요.")
            elif act == "inx":
                await c.execute("DELETE FROM inputs WHERE tg_id=$1 AND kind <> 'address'", uid)
                drop = (True, "그만뒀어요.")
            elif act == "amt":                  # U3: 예치 금액 다시 입력
                d = await c.fetchrow("SELECT * FROM deals WHERE id=$1", deal_id)
                if not d:
                    raise core.Refused(T.NOTHING)
                core._party(d, uid)
                core._expect(d, "DEPOSIT")
                if await c.fetchval("SELECT 1 FROM deposit_slots WHERE deal_id=$1 AND tg_id=$2 AND status IN ('received','confirmed')",
                                    deal_id, uid):
                    raise core.Refused("이미 입금이 들어와서 금액을 바꿀 수 없어요.")
                st = await core.settings(c)
                await core.set_input(c, uid, "amount", deal_id, None, 60)
                answer = "새 금액을 보내주세요."
                dms.append((T.ask_amount(d["code"], int(st["min_micro"])) + "\n\n앞서 안내한 금액은 더 이상 쓰지 마세요.", None))
            elif act == "scr":                  # 지금 단계 화면 다시 (버튼 포함)
                d = await c.fetchrow("SELECT * FROM deals WHERE id=$1", deal_id)
                if not d:
                    raise core.Refused(T.NOTHING)
                core._party(d, uid)
                sc = await core.screen(c, ctx, d)
                if not sc:
                    raise core.Refused(f"지금은 {T.STATE_LABEL.get(d['state'], d['state'])} 단계라 누를 버튼이 없어요.")
                dms.append(sc)
            elif act == "link":                 # U2: 거래방 링크 다시
                d = await c.fetchrow("SELECT * FROM deals WHERE id=$1", deal_id)
                if not d:
                    raise core.Refused(T.NOTHING)
                core._party(d, uid)
                if not d["room_id"] or d["state"] in core.CLOSED:
                    raise core.Refused("이 거래는 거래방이 없어요. 봇 1:1 에서 진행돼요.")
                relink = (d["room_id"], d["code"])
            else:
                answer = T.STALE
    except core.Refused as e:
        await _safe(q.answer(str(e)[:190], show_alert=True))
        return
    if relink:
        try:
            link = await context.bot.create_chat_invite_link(relink[0], name=relink[1], creates_join_request=True,
                                                             expire_date=datetime.now(timezone.utc) + timedelta(hours=2))
            dms.append((T.room_relink(relink[1], link.invite_link), None))
            answer = "1:1 로 링크를 보냈어요."
        except TelegramError as e:
            log.warning("방 링크 다시 만들기 실패: %r", e)
            async with db.tx() as c:
                await core.room_broken(c, ctx, relink[0], f"링크 다시 만들기 실패: {e}")
            answer = "거래방에 문제가 있어서 1:1 로 이어갈게요."
    await _safe(q.answer(answer))
    if drop[0]:
        await drop_buttons(q, drop[1])
    for text, rows in dms:
        await send(context, uid, text, rows)


async def _safe(coro):
    """텔레그램 응답 실패는 무시 (이미 DB 는 커밋됨)."""
    try:
        await coro
    except TelegramError as e:
        log.info("응답 실패(무시): %r", e)


# ── 관리자 버튼 ───────────────────────────────────────
async def on_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, parts: list[str]):
    q = update.callback_query
    db, ctx, cfg = svc(context)
    uid = q.from_user.id
    if uid not in ctx.admins:
        await q.answer("관리자만", show_alert=True)
        return
    try:
        aid = int(parts[1])
        async with db.tx() as c:
            msg = await core.decide(c, ctx, aid, uid, parts[0] == "ok")
        await q.answer(msg)
        await drop_buttons(q, f"{msg} ({q.from_user.full_name})")
    except core.Refused as e:
        await q.answer(str(e)[:190], show_alert=True)
        await drop_buttons(q, str(e))
    except (IndexError, ValueError):
        await q.answer(T.STALE)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    head, _, rest = data.partition(":")
    parts = rest.split(":") if rest else []
    if head == "t":
        await on_tutorial(update, context, rest)
    elif head == "m":
        await on_mock(update, context, rest)
    elif head == "menu":
        await on_menu(update, context, rest)
    elif head == "d":
        await on_deal(update, context, parts)
    elif head == "a":
        await on_admin(update, context, parts)
    else:
        await q.answer(T.STALE)


# ── 1:1 글자 입력 ─────────────────────────────────────
async def on_private_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """같은 사람의 입력은 하나씩 (빠르게 두 번 보내도 주소 확인 등이 꼬이지 않게)."""
    uid = update.effective_user.id
    locks = context.application.bot_data.setdefault("input_locks", {})
    lock = locks.setdefault(uid, asyncio.Lock())
    async with lock:
        await _private_text(update, context)
    if not lock.locked() and len(locks) > 5000:
        locks.clear()


async def _private_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db, ctx, cfg = svc(context)
    msg = update.effective_message
    uid = update.effective_user.id
    text = (msg.text or "").strip()
    if await is_banned(db, uid):
        await db.execute("DELETE FROM inputs WHERE tg_id=$1", uid)
        await send(context, uid, T.BANNED.format(support=T.esc(cfg.support)))
        return
    inp = await db.fetchrow("SELECT * FROM inputs WHERE tg_id=$1 AND expires_at > now()", uid)
    if not inp:
        async with db.tx() as c:
            code = await core.add_evidence(c, ctx, uid, "text", text, None)
        if code:
            await send(context, uid, T.evidence_saved(code))
            return
        await show_main(context, uid)
        await remind_addresses(context, uid)
        return
    if text in ("취소", "그만", "/cancel"):
        await db.execute("DELETE FROM inputs WHERE tg_id=$1", uid)
        await send(context, uid, T.ADDRESS_LATER if inp["kind"] == "address" else T.CANCELLED_INPUT)
        await show_main(context, uid)
        return
    try:
        if inp["kind"] == "partner":
            await _input_partner(context, uid, text)
        elif inp["kind"] == "amount":
            v = parse_usdt(text)
            async with db.tx() as c:
                if v is None:
                    raise core.Refused(T.amount_bad(int((await core.settings(c))["min_micro"])))
                await core.open_slot(c, ctx, inp["deal_id"], uid, v)
                await c.execute("DELETE FROM inputs WHERE tg_id=$1", uid)
        elif inp["kind"] == "partial":
            v = parse_usdt(text)
            if v is None:
                raise core.Refused("숫자만 보내주세요.")
            async with db.tx() as c:
                await core.partial(c, ctx, inp["deal_id"], uid, v)
                await c.execute("DELETE FROM inputs WHERE tg_id=$1", uid)
        elif inp["kind"] == "custom":
            v = parse_usdt(text) if text != "0" else 0
            if v is None:
                raise core.Refused("숫자만 보내주세요.")
            async with db.tx() as c:
                d = await c.fetchrow("SELECT * FROM deals WHERE id=$1", inp["deal_id"])
                total = sum((await core.balances(c, inp["deal_id"])).values())
                if not 0 <= v <= total:
                    raise core.Refused(f"0 ~ {fmt(total)} 사이로 보내주세요.")
                # 누른 사람 본인 몫을 받음 → core 는 '신청자 몫' 으로
                await core.propose_split(c, ctx, inp["deal_id"], uid, "custom", v if uid == d["creator"] else total - v)
                await c.execute("DELETE FROM inputs WHERE tg_id=$1", uid)
        elif inp["kind"] == "address":
            await _input_address(context, uid, inp, text)
    except core.Refused as e:
        await send(context, uid, str(e))


async def _input_partner(context, uid: int, text: str):
    db, ctx, cfg = svc(context)
    m = _USERNAME.match(text)
    if not m:
        raise core.Refused(T.PARTNER_BAD)
    name = m.group(1)
    async with db.tx() as c:
        partner = await c.fetchrow("SELECT * FROM users WHERE lower(username)=lower($1) ORDER BY updated_at DESC LIMIT 1", name)
        if partner and partner["tg_id"] == uid:
            raise core.Refused(T.PARTNER_SELF)
        if partner and partner["banned"]:
            raise core.Refused("이 상대와는 거래할 수 없어요.")
        d = await core.create_deal(c, ctx, uid, partner["tg_id"] if partner else None, name)
        await c.execute("DELETE FROM inputs WHERE tg_id=$1", uid)
    cancel = [[("요청 취소", f"d:cx:{d['id']}")]]
    if partner and partner["dm_ok"]:
        await send(context, uid, T.invite_sent("@" + T.esc(name), d["code"]), cancel)
    else:       # 봇과 1:1 을 연 적 없는 사람에겐 봇이 먼저 못 보냄 → 링크를 직접 전해 달라고
        bot_user = context.application.bot_data.get("bot_username") or cfg.bot_username
        await send(context, uid, T.partner_unknown(name, f"https://t.me/{bot_user}?start=join_{d['code']}"), cancel)


async def _input_address(context, uid: int, inp, text: str):
    db, ctx, cfg = svc(context)
    addr = text.strip()
    data = dict(inp["data"] or {})
    if not is_address(addr):
        raise core.Refused(T.ADDRESS_BAD)
    if addr == cfg.pay_address:
        raise core.Refused("그건 저희 입금 주소예요. 받으실 본인 주소를 보내주세요.")
    po = await db.fetchrow("SELECT * FROM payouts WHERE id=$1", inp["ref_id"])
    if not po or po["status"] != "need_address":
        await db.execute("DELETE FROM inputs WHERE tg_id=$1", uid)
        raise core.Refused(T.STALE)
    code = await db.fetchval("SELECT code FROM deals WHERE id=$1", po["deal_id"])
    first = data.get("first")
    if not first:
        tron: TronGrid = context.application.bot_data["tron"]
        if await tron.is_blacklisted(addr):
            raise core.Refused(T.ADDRESS_BLOCKED)
        known = [r["address"] for r in await db.fetch("SELECT address FROM addresses WHERE tg_id=$1", uid)]
        await db.execute("UPDATE inputs SET data=$2 WHERE tg_id=$1", uid, {"first": addr})
        if known and addr not in known:
            await send(context, uid, T.address_new_warning(addr))
        else:
            await send(context, uid, T.ask_address(code, po["amount_micro"], 2))
        return
    if addr != first:
        await db.execute("UPDATE inputs SET data='{}' WHERE tg_id=$1", uid)
        raise core.Refused(T.ADDRESS_MISMATCH)
    async with db.tx() as c:
        await core.set_address(c, ctx, po["id"], uid, addr)


def _media(msg) -> tuple[str, str, str | None]:
    """(kind, 글, file_id) — 사진은 제일 큰 것."""
    text = msg.text or msg.caption or ""
    if msg.photo:
        return "photo", text, msg.photo[-1].file_id
    if msg.document:
        return "document", text or (msg.document.file_name or ""), msg.document.file_id
    if msg.video:
        return "video", text, msg.video.file_id
    return ("text" if msg.text else "other"), text, None


async def on_room_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """거래방 글은 그 거래 기록으로 (분쟁 때 관리자가 관리 페이지에서 봄). 봇은 방 관리자라 모든 글을 받음."""
    msg = update.effective_message
    u = update.effective_user
    if not msg or not u or u.is_bot:
        return
    db, _, _ = svc(context)
    kind, text, fid = _media(msg)
    async with db.tx() as c:
        await core.log_room_message(c, msg.chat.id, u.id, kind, text, fid)


async def on_private_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """1:1 로 온 사진·파일: 분쟁 중이면 증거로 저장."""
    db, ctx, _ = svc(context)
    msg = update.effective_message
    uid = update.effective_user.id
    kind, text, fid = _media(msg)
    async with db.tx() as c:
        code = await core.add_evidence(c, ctx, uid, kind, text, fid)
    await send(context, uid, T.evidence_saved(code) if code else "사진·파일은 분쟁 중일 때만 증거로 받아요.")


# ── 거래방 ───────────────────────────────────────────
async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db, ctx, _ = svc(context)
    jr = update.chat_join_request
    try:
        async with db.tx() as c:
            ok = await core.room_joined(c, ctx, jr.chat.id, jr.from_user.id)
        if ok:
            await jr.approve()
        else:
            await jr.decline()
    except TelegramError as e:
        log.warning("가입 신청 처리 실패: %r", e)


async def on_my_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """봇이 그룹 관리자가 됨 → 풀 등록. 넣은 사람이 운영 관리자가 아니면 나감 (아무 그룹이나 거래방이 되면 안 됨)."""
    db, ctx, _ = svc(context)
    cm = update.my_chat_member
    chat = cm.chat
    if chat.type not in (Chat.GROUP, Chat.SUPERGROUP):
        return
    new = cm.new_chat_member
    if new.status in ("left", "kicked"):
        async with db.tx() as c:
            if await c.fetchval("SELECT 1 FROM rooms WHERE chat_id=$1", chat.id):
                await core.room_broken(c, ctx, chat.id, "봇이 방에서 나가짐")
        return
    hdata = await helper.load(db)
    if hdata and cm.from_user.id == hdata.get("user_id"):
        return
    if cm.from_user.id not in ctx.admins:
        try:
            await context.bot.leave_chat(chat.id)
        except TelegramError:
            pass
        return
    if not isinstance(new, ChatMemberAdministrator):
        async with db.tx() as c:
            await core.room_register(c, ctx, chat.id, chat.title or "", False, "봇을 관리자로 올려주세요.")
        return
    missing = [n for n, ok in (("사용자 초대", new.can_invite_users), ("사용자 차단", new.can_restrict_members),
                               ("메시지 삭제", new.can_delete_messages)) if not ok]
    note = ""
    ok = not missing and chat.type == Chat.SUPERGROUP
    if missing:
        note = "권한 필요: " + ", ".join(missing)
    elif chat.type != Chat.SUPERGROUP:
        note = "슈퍼그룹이어야 해요 (그룹 설정에서 '새 멤버에게 대화 기록 숨기기'를 켜면 슈퍼그룹이 돼요)."
    else:
        try:
            full = await context.bot.get_chat(chat.id)
            if getattr(full, "has_visible_history", False):
                ok, note = False, "'새 멤버에게 대화 기록 보이기'를 꺼주세요 (다음 거래 사람이 이전 대화를 보면 안 됨)."
        except TelegramError as e:
            ok, note = False, f"방 정보 조회 실패: {e}"
    async with db.tx() as c:
        await core.room_register(c, ctx, chat.id, chat.title or "", ok, note)


async def _room_open(context, row):
    db, ctx, _ = svc(context)
    deal = await db.fetchrow("SELECT * FROM deals WHERE id=$1", row["payload"]["deal_id"])
    chat_id = row["chat_id"]
    if not deal or deal["state"] != "ROOM" or deal["room_id"] != chat_id:
        return
    try:
        link = await context.bot.create_chat_invite_link(
            chat_id, name=deal["code"], creates_join_request=True,
            expire_date=datetime.now(timezone.utc) + timedelta(hours=2))
    except TelegramError as e:
        async with db.tx() as c:
            await core.room_failed(c, ctx, deal["id"], chat_id, f"초대 링크 실패: {e}")
        return
    await db.execute("UPDATE rooms SET invite_link=$2, updated_at=now() WHERE chat_id=$1", chat_id, link.invite_link)
    for p in (deal["creator"], deal["partner"]):
        await send(context, p, T.room_link(deal["code"], link.invite_link))


async def _room_create(context, row):
    """빈 방이 없을 때 도우미 계정으로 새 거래방. 실패하면 1:1 로."""
    db, ctx, cfg = svc(context)
    deal_id = row["payload"]["deal_id"]
    code = await db.fetchval("SELECT code FROM deals WHERE id=$1", deal_id)
    data = await helper.load(db)
    title = f"{cfg.bot_name} 거래방"
    try:
        if not data:
            raise RuntimeError("도우미 계정이 연결돼 있지 않아요")
        h: helper.Helper = context.application.bot_data.setdefault("helper", helper.Helper())
        bot_user = context.application.bot_data.get("bot_username") or cfg.bot_username
        others = sorted(a for a in ctx.admins if a != data.get("user_id"))     # 도우미가 곧 관리자 계정이면 이미 방장
        chat_id = await h.create_room(data, title, bot_user, others)
        data["rooms_made"] = int(data.get("rooms_made", 0)) + 1
        await helper.save(db, data)
    except Exception as e:   # noqa: BLE001 — 텔레그램 제한·로그인 풀림 등
        log.warning("방 만들기 실패 %s: %r", code, e)
        async with db.tx() as c:
            await core.room_create_failed(c, ctx, deal_id, f"{type(e).__name__}: {e}")
        return
    async with db.tx() as c:
        await core.room_created(c, ctx, deal_id, chat_id, title)


async def _room_close(context, row):
    db, ctx, _ = svc(context)
    chat_id = row["chat_id"]
    deal_id = row["payload"]["deal_id"]
    room = await db.fetchrow("SELECT * FROM rooms WHERE chat_id=$1", chat_id)
    problems = []
    if room and room["invite_link"]:
        try:
            await context.bot.revoke_chat_invite_link(chat_id, room["invite_link"])
        except TelegramError as e:
            problems.append(f"링크 폐기: {e}")
    for r in await db.fetch("SELECT tg_id FROM deal_parties WHERE deal_id=$1", deal_id):
        try:
            await context.bot.ban_chat_member(chat_id, r["tg_id"])
            await context.bot.unban_chat_member(chat_id, r["tg_id"], only_if_banned=True)
        except TelegramError as e:
            problems.append(f"내보내기 {r['tg_id']}: {e}")
    ids = [r["message_id"] for r in await db.fetch(
        "SELECT message_id FROM outbox WHERE chat_id=$1 AND deal_id=$2 AND message_id IS NOT NULL "
        "AND sent_at > now() - interval '47 hours' ORDER BY id", chat_id, deal_id)]
    for i in range(0, len(ids), 100):
        try:
            await context.bot.delete_messages(chat_id, ids[i:i + 100])
        except TelegramError as e:
            problems.append(f"메시지 지우기: {e}")
    async with db.tx() as c:
        if problems:
            await core.room_broken(c, ctx, chat_id, "; ".join(problems)[:200])
        else:
            await core.room_freed(c, chat_id)


# ── 보내기 줄 (outbox) ────────────────────────────────
async def outbox_job(context: ContextTypes.DEFAULT_TYPE):
    db, _, _ = svc(context)
    rows = await db.fetch("SELECT * FROM outbox WHERE sent_at IS NULL AND attempts < 5 ORDER BY id LIMIT 30")
    blocked: set[int] = set()
    for row in rows:
        chat = row["chat_id"]
        if chat in blocked:
            continue                     # 같은 채팅은 순서 지키기
        try:
            message_id = None
            if row["kind"] == "msg":
                m = await context.bot.send_message(chat, row["text"], parse_mode=ParseMode.HTML,
                                                   reply_markup=markup(row["buttons"]), disable_web_page_preview=True)
                message_id = m.message_id
            elif row["kind"] == "room_open":
                await _room_open(context, row)
            elif row["kind"] == "room_close":
                await _room_close(context, row)
            elif row["kind"] == "room_create":
                await _room_create(context, row)
            await db.execute("UPDATE outbox SET sent_at=now(), attempts=attempts+1, message_id=$2 WHERE id=$1", row["id"], message_id)
        except RetryAfter as e:
            blocked.add(chat)
            await asyncio.sleep(min(float(getattr(e, "retry_after", 3) or 3), 10))
        except Forbidden as e:
            await db.execute("UPDATE outbox SET sent_at=now(), attempts=attempts+1, error=$2 WHERE id=$1", row["id"], f"막힘: {e}")
            if chat < 0:
                async with db.tx() as c:
                    if await c.fetchval("SELECT 1 FROM rooms WHERE chat_id=$1 AND state <> 'broken'", chat):
                        await core.room_broken(c, svc(context)[1], chat, f"방에 못 보냄: {e}")
        except (BadRequest, TelegramError) as e:
            blocked.add(chat)
            await db.execute("UPDATE outbox SET attempts=attempts+1, error=$2 WHERE id=$1", row["id"], str(e)[:300])
        except Exception as e:   # noqa: BLE001
            log.exception("outbox %s", row["id"])
            blocked.add(chat)
            await db.execute("UPDATE outbox SET attempts=attempts+1, error=$2 WHERE id=$1", row["id"], repr(e)[:300])


async def expire_job(context: ContextTypes.DEFAULT_TYPE):
    db, ctx, _ = svc(context)
    async with db.tx() as c:
        n = await core.expire_due(c, ctx)
    if n:
        log.info("시간 초과 정리 %d건", n)


async def monitor_job(context: ContextTypes.DEFAULT_TYPE):
    """1분마다: 감시가 5분 넘게 멈췄으면 관리자에게 🚨 (한 시간에 한 번). 하루 한 번 장부 vs 지갑 보고."""
    db, ctx, cfg = svc(context)
    if not cfg.can_take_deposits:
        return
    now = int(datetime.now(timezone.utc).timestamp())
    kv = {r["key"]: r["value"] for r in await db.fetch(
        "SELECT key, value FROM kv WHERE key IN ('watch_alive','watch_alarm_at','daily_report_at','wallet_balance')")}
    alive = int(kv.get("watch_alive") or 0)
    if now - alive > 300 and now - int(kv.get("watch_alarm_at") or 0) > 3600:
        async with db.tx() as c:
            await core.say_admins(c, ctx, "🚨 <b>입금 감시가 멈췄어요</b> (5분 넘게 소식 없음). 입금·송금이 자동 확인되지 않아요.\n"
                                          "서버의 escrow-watcher 를 확인해주세요." + ("" if alive else "\n(아직 한 번도 안 돌았어요)"))
            await c.execute("INSERT INTO kv(key, value) VALUES('watch_alarm_at',$1) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", now)
    if now - int(kv.get("daily_report_at") or 0) > 24 * 3600:
        async with db.tx() as c:
            wb = kv.get("wallet_balance") or {}
            s_ = await core.stats(c, wb.get("micro"))
            diff = "" if wb.get("micro") is None else f" · 차이 {fmt(int(wb['micro']) - s_['ledger_micro'])}"
            await core.say_admins(c, ctx, f"📋 <b>하루 보고</b>\n진행 중 {s_['active']} · 오늘 새 거래 {s_['today']} · 분쟁 {s_['disputes']}\n"
                                          f"장부 {fmt(s_['ledger_micro'])} USDT · 지갑 "
                                          f"{fmt(int(wb['micro'])) + ' USDT' if wb.get('micro') is not None else '조회 전'}{diff}\n"
                                          f"확인 대기 {s_['pending_approvals']} · 보낼 것 {s_['payouts_to_send']} · 확인 필요 입금 {s_['unmatched_in']}")
            await c.execute("INSERT INTO kv(key, value) VALUES('daily_report_at',$1) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", now)


# ── 관리자 명령 ───────────────────────────────────────
async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db, ctx, cfg = svc(context)
    uid = update.effective_user.id
    if update.effective_chat.type != Chat.PRIVATE or uid not in ctx.admins:
        return
    async with db.tx() as c:
        s = await core.stats(c)
        pend = await c.fetch("SELECT a.*, d.code FROM approvals a LEFT JOIN deals d ON d.id=a.deal_id "
                             "WHERE a.status IN ('pending','held') ORDER BY a.id LIMIT 10")
    lines = [f"진행 중 {s['active']} · 오늘 {s['today']} · 분쟁 {s['disputes']}",
             f"장부상 보관 {fmt(s['ledger_micro'])} USDT · 번 수수료 {fmt(s['fees_micro'])}",
             f"확인 대기 {s['pending_approvals']} (보류 {s['held_approvals']}) · 보낼 것 {s['payouts_to_send']} · 확인 필요 입금 {s['unmatched_in']}",
             f"빈 거래방 {s['rooms_free']}"]
    if cfg.stub_fields:
        lines.append("⚠️ 아직 설정 안 됨: " + ", ".join(cfg.stub_fields))
    rows = [[("관리 페이지", "url:" + cfg.admin_base_url)]] if cfg.admin_base_url.startswith("https://") else None
    await send(context, uid, "<b>관리</b>\n" + "\n".join(lines), rows)
    for a in pend:
        await send(context, uid, f"{'(보류) ' if a['status'] == 'held' else ''}{T.ADM_KIND.get(a['kind'], a['kind'])} · <code>{a['code'] or '-'}</code>",
                   core.kb_approval(a["id"]))


async def cmd_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/login — 관리 페이지 1회용 링크 (5분, 한 번만). 링크는 전달·저장 못 하게 protect_content."""
    db, ctx, cfg = svc(context)
    uid = update.effective_user.id
    if update.effective_chat.type != Chat.PRIVATE or uid not in ctx.admins:
        return
    if not cfg.admin_base_url.startswith("https://"):
        await send(context, uid, "ADMIN_BASE_URL(https://…)이 아직 설정 안 됐어요.")
        return
    token = secrets.token_urlsafe(32)
    await db.execute("INSERT INTO admin_logins(token_hash, tg_id, expires_at) VALUES($1,$2, now() + interval '5 minutes')",
                     hashlib.sha256(token.encode()).hexdigest(), uid)
    await db.execute("DELETE FROM admin_logins WHERE expires_at < now() - interval '1 day'")
    await context.bot.send_message(uid, "관리 페이지 로그인 링크예요. 5분 안에 한 번만 쓸 수 있어요.",
                                   reply_markup=markup([[("관리 페이지 열기", f"url:{cfg.admin_base_url}/login?t={token}")]]),
                                   protect_content=True)


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/check 거래코드 — 이 봇에서 만든 진짜 거래인지."""
    db, _, _ = svc(context)
    uid = update.effective_user.id
    if not context.args:
        await update.effective_message.reply_text("거래코드를 같이 보내주세요. 예: /check AB12CD")
        return
    async with db.tx() as c:
        text = await core.check_deal(c, context.args[0][:12], uid)
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/cancel — 입력 기다리는 것 그만두기."""
    if update.effective_chat.type != Chat.PRIVATE:
        return
    db, _, _ = svc(context)
    uid = update.effective_user.id
    kind = await db.fetchval("DELETE FROM inputs WHERE tg_id=$1 RETURNING kind", uid)
    await send(context, uid, T.ADDRESS_LATER if kind == "address" else T.CANCELLED_INPUT)


async def on_migrate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message
    if m and m.migrate_to_chat_id:
        db, _, _ = svc(context)
        async with db.tx() as c:
            await core.room_migrated(c, m.chat.id, m.migrate_to_chat_id)


async def cmd_diag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/diag — 원격 점검 창구 토큰 새로 받기 (관리자만, 이전 토큰은 바로 무효). 클로드 환경 비밀값 ESCROW_DIAG_TOKEN 에 넣는 용."""
    db, ctx, _ = svc(context)
    uid = update.effective_user.id
    if update.effective_chat.type != Chat.PRIVATE or uid not in ctx.admins:
        return
    from .admin.diag import new_token
    tok, rec = new_token()
    async with db.tx() as c:
        await c.execute("INSERT INTO kv(key, value) VALUES('diag_token',$1) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", rec)
        await core.event(c, None, uid, "diag_token", {})
    await context.bot.send_message(uid, "🔌 원격 점검 토큰 (읽기 전용, 이전 토큰은 이제 안 돼요)\n\n"
                                        f"<code>{tok}</code>\n\n클로드 환경 설정의 비밀값 ESCROW_DIAG_TOKEN 에 넣어 주세요. "
                                        "채팅에는 붙이지 마세요.", parse_mode=ParseMode.HTML, protect_content=True)


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/id — 내 텔레그램 숫자 ID (관리자 등록용)."""
    if update.effective_chat.type == Chat.PRIVATE:
        await update.effective_message.reply_text(f"내 텔레그램 ID: <code>{update.effective_user.id}</code>", parse_mode=ParseMode.HTML)


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    log.error("처리 중 오류", exc_info=context.error)


# ── 시작 ─────────────────────────────────────────────
async def post_init(app: Application):
    cfg = app.bot_data["cfg"]
    db = await DB.connect(cfg.database_url)
    await db.migrate()
    app.bot_data["db"] = db
    app.bot_data["tron"] = TronGrid(cfg.trongrid_url, cfg.trongrid_key)
    me = await app.bot.get_me()
    app.bot_data["bot_username"] = cfg.bot_username or me.username
    if cfg.stub_fields:
        log.warning("아직 STUB: %s — .env 를 채워야 운영 가능", ", ".join(cfg.stub_fields))
    app.job_queue.run_repeating(outbox_job, interval=1.0, first=1)
    app.job_queue.run_repeating(expire_job, interval=30, first=10)
    app.job_queue.run_repeating(monitor_job, interval=60, first=120)
    log.info("시작 @%s", me.username)


async def post_shutdown(app: Application):
    if "db" in app.bot_data:
        await app.bot_data["db"].close()
    if "tron" in app.bot_data:
        await app.bot_data["tron"].close()
    if "helper" in app.bot_data:
        await app.bot_data["helper"].close()


def build(cfg) -> Application:
    app = (Application.builder().token(cfg.bot_token).concurrent_updates(True)
           .post_init(post_init).post_shutdown(post_shutdown).build())
    app.bot_data["cfg"] = cfg
    app.bot_data["ctx"] = core.Ctx(cfg)
    import os
    if os.environ.get("MENU_PHOTO"):
        app.bot_data["menu_photo"] = os.environ["MENU_PHOTO"]
    app.add_handler(TypeHandler(Update, track_user), group=-1)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("login", cmd_login))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("diag", cmd_diag))
    app.add_handler(MessageHandler(filters.StatusUpdate.MIGRATE, on_migrate))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(ChatJoinRequestHandler(on_join_request))
    app.add_handler(ChatMemberHandler(on_my_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, on_private_text))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & (filters.PHOTO | filters.Document.ALL | filters.VIDEO), on_private_media))
    app.add_handler(MessageHandler(filters.ChatType.SUPERGROUP & ~filters.StatusUpdate.ALL, on_room_message))
    app.add_error_handler(on_error)
    return app


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = cfgmod.load()
    if not cfg.bot_token:
        raise SystemExit("BOT_TOKEN 이 없어요 (.env)")
    build(cfg).run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
