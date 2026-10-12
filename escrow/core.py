"""거래 규칙 (텔레그램·웹과 무관한 순수 로직 + DB).

- 모든 함수는 이미 열린 트랜잭션 연결 `c` 를 받는다. 거래를 바꾸는 함수는 맨 처음 `_lock(c, deal_id)` 로 그 줄을 잠근다
  (버튼 연타·두 사람 동시 클릭·관리자와 봇 동시 처리에도 한 번만).
- 텔레그램으로 보낼 것은 같은 트랜잭션에서 outbox 에 넣는다 → 커밋돼야만 나가고, 재시작해도 안 잃는다.
- 돈 규칙: 지급 합계 ≤ 맡긴 합계. 지급 줄을 만들 때마다 같은 트랜잭션에서 다시 센다.
"""
from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import asyncpg

from . import texts as T
from .config import Config
from .money import MICRO, fmt, split_fee

log = logging.getLogger(__name__)

ACTIVE = ("INVITED", "ROOM", "NOTICE", "CHOOSE", "DEPOSIT", "START_REVIEW", "FUNDED", "SPLIT", "ADDRESS", "PAYOUT",
          "DISPUTE", "REFUND")
CLOSED = ("DONE", "CANCELLED")
EXPIRING = ("INVITED", "ROOM", "NOTICE", "CHOOSE", "DEPOSIT")     # 30분 무응답이면 멈추는 단계 (돈 나가는 단계는 안 멈춤)
MONEY_HELD = ("received", "confirmed")

DEFAULTS = {
    "fee_micro": 20 * MICRO,
    "min_micro": 50 * MICRO,
    "refund_fee_micro": 0,
    "stage_minutes": 30,
    "slot_minutes": 30,
    "slot_grace_minutes": 10,     # 안내한 시간보다 조금 늦게 확정돼도 인정
    "auto_max_micro": 0,          # 자동 모드라도 이 금액을 넘으면 수동 (0 = 제한 없음)
    "new_deals": True,            # False 면 새 거래 안 받음
    "mode.deposit": "manual",
    "mode.start": "manual",
    "mode.partial": "manual",
    "mode.final": "manual",
    "mode.refund": "manual",
    "mode.payout_done": "manual",
}
MODES = ("deposit", "start", "partial", "final", "refund", "payout_done")
_CODE_ABC = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class Refused(Exception):
    """사용자에게 그대로 보여 줄 이유와 함께 거절."""


@dataclass
class Ctx:
    cfg: Config

    @property
    def admins(self) -> frozenset[int]:
        return self.cfg.admin_ids


def now() -> datetime:
    return datetime.now(timezone.utc)


# ── 공통 ─────────────────────────────────────────────
async def settings(c) -> dict:
    out = dict(DEFAULTS)
    for r in await c.fetch("SELECT key, value FROM settings"):
        if r["key"] in DEFAULTS:
            out[r["key"]] = r["value"]
    return out


async def set_setting(c, key: str, value, admin: int) -> None:
    if key not in DEFAULTS:
        raise Refused(f"없는 설정: {key}")
    d = DEFAULTS[key]
    if key.startswith("mode.") and value not in ("manual", "auto"):
        raise Refused("manual 또는 auto")
    if isinstance(d, bool):
        value = bool(value)
    elif isinstance(d, int):
        value = int(value)
        if value < 0:
            raise Refused("0 이상")
    await c.execute("INSERT INTO settings(key, value, updated_by) VALUES($1, $2, $3) "
                    "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_by=EXCLUDED.updated_by, updated_at=now()",
                    key, value, admin)
    await event(c, None, admin, "setting", {"key": key, "value": value})


def is_auto(st: dict, mode: str, amount: int = 0) -> bool:
    if st.get(f"mode.{mode}") != "auto":
        return False
    cap = int(st.get("auto_max_micro") or 0)
    return not cap or amount <= cap


async def event(c, deal_id, actor, kind: str, data: dict | None = None) -> None:
    await c.execute("INSERT INTO events(deal_id, actor, kind, data) VALUES($1,$2,$3,$4)",
                    deal_id, actor or 0, kind, data or {})


async def say(c, chat_id: int, text: str, buttons=None, deal_id: int | None = None) -> None:
    await c.execute("INSERT INTO outbox(kind, chat_id, text, buttons, deal_id) VALUES('msg',$1,$2,$3,$4)",
                    chat_id, text, buttons, deal_id)


async def task(c, kind: str, payload: dict, chat_id: int | None = None, deal_id: int | None = None) -> None:
    await c.execute("INSERT INTO outbox(kind, chat_id, payload, deal_id) VALUES($1,$2,$3,$4)", kind, chat_id, payload, deal_id)


async def say_admins(c, ctx: Ctx, text: str, buttons=None, deal_id=None) -> None:
    for a in sorted(ctx.admins):
        await say(c, a, text, buttons, deal_id)


async def say_deal(c, deal, text: str, buttons=None) -> None:
    """거래방이 있으면 방에, 없으면 두 사람 1:1 에."""
    if deal["room_id"]:
        await say(c, deal["room_id"], text, buttons, deal["id"])
    else:
        for p in (deal["creator"], deal["partner"]):
            if p:
                await say(c, p, text, buttons, deal["id"])


async def upsert_user(c, tg_id: int, username: str | None, name: str) -> asyncpg.Record:
    return await c.fetchrow(
        "INSERT INTO users(tg_id, username, name) VALUES($1,$2,$3) ON CONFLICT (tg_id) DO UPDATE "
        "SET username=EXCLUDED.username, name=EXCLUDED.name, updated_at=now() RETURNING *",
        tg_id, username, name[:64])


async def user(c, tg_id: int):
    return await c.fetchrow("SELECT * FROM users WHERE tg_id=$1", tg_id)


async def label(c, tg_id: int | None) -> str:
    if not tg_id:
        return "상대"
    u = await user(c, tg_id)
    return T.who(u["username"], u["name"], tg_id) if u else f"#{tg_id}"


async def _lock(c, deal_id: int):
    d = await c.fetchrow("SELECT * FROM deals WHERE id=$1 FOR UPDATE", deal_id)
    if not d:
        raise Refused(T.NOTHING)
    return d


async def _set_state(c, deal, state: str, actor: int, minutes: int | None = None, **extra):
    deadline = now() + timedelta(minutes=minutes) if minutes else None
    sets = ["state=$2", "deadline=$3", "updated_at=now()"]
    args = [deal["id"], state, deadline]
    for k, v in extra.items():
        args.append(v)
        sets.append(f"{k}=${len(args)}")
    if state in CLOSED:
        sets.append("closed_at=now()")
    row = await c.fetchrow(f"UPDATE deals SET {', '.join(sets)} WHERE id=$1 RETURNING *", *args)
    await event(c, deal["id"], actor, "state", {"from": deal["state"], "to": state, **{k: v for k, v in extra.items()
                                                                                       if isinstance(v, (str, int))}})
    return row


def _party(deal, tg_id: int) -> None:
    if tg_id not in (deal["creator"], deal["partner"]):
        raise Refused(T.NOT_PARTY)


def _expect(deal, *states: str) -> None:
    if deal["state"] == "DISPUTE":
        raise Refused(T.LOCKED)
    if deal["state"] not in states:
        raise Refused(T.STALE)


def _other(deal, tg_id: int) -> int:
    return deal["partner"] if tg_id == deal["creator"] else deal["creator"]


async def _new_code(c) -> str:
    for _ in range(20):
        code = "".join(secrets.choice(_CODE_ABC) for _ in range(6))
        if not await c.fetchval("SELECT 1 FROM deals WHERE code=$1", code):
            return code
    raise RuntimeError("거래코드 생성 실패")


# ── 버튼 ─────────────────────────────────────────────
def kb_invite(deal_id):
    return [[["수락", f"d:acc:{deal_id}"], ["거절", f"d:dec:{deal_id}"]]]


def kb_notice(deal_id):
    return [[["공지 확인", f"d:ntc:{deal_id}"]], [["규칙 전문", f"d:nfull:{deal_id}"]],
            [["관리자 호출", f"d:sos:{deal_id}"], ["거래 취소", f"d:cx:{deal_id}"]]]


def kb_choose(deal_id):
    return [[["맡기기", f"d:dep:{deal_id}"], ["안 맡기기", f"d:nodep:{deal_id}"]],
            [["관리자 호출", f"d:sos:{deal_id}"], ["거래 취소", f"d:cx:{deal_id}"]]]


def kb_funded(deal_id):
    return [[["부분 지급", f"d:part:{deal_id}"], ["거래완료", f"d:fin:{deal_id}"]],
            [["관리자 호출", f"d:sos:{deal_id}"], ["거래 취소", f"d:cx:{deal_id}"]]]


def kb_complete_confirm(deal_id):
    return [[["예", f"d:finy:{deal_id}"], ["아니요", f"d:finn:{deal_id}"]]]


def kb_split(deal_id, a_label: str, b_label: str):
    return [[["각자 맡긴 만큼 돌려받기", f"d:sp:each:{deal_id}"]],
            [[f"전액 {a_label} 에게"[:40], f"d:sp:a:{deal_id}"]],
            [[f"전액 {b_label} 에게"[:40], f"d:sp:b:{deal_id}"]],
            [["직접 입력", f"d:sp:custom:{deal_id}"]],
            [["관리자 호출", f"d:sos:{deal_id}"]]]


def kb_split_agree(deal_id, split_id):
    return [[["동의", f"d:spok:{deal_id}:{split_id}"], ["다시 정하기", f"d:spno:{deal_id}:{split_id}"]]]


def kb_cancel_agree(deal_id):
    return [[["취소 동의", f"d:cxok:{deal_id}"], ["계속 진행", f"d:cxno:{deal_id}"]]]


def kb_approval(approval_id):
    return [[["승인", f"a:ok:{approval_id}"], ["보류", f"a:hold:{approval_id}"]]]


# ── 거래 시작 ─────────────────────────────────────────
async def create_deal(c, ctx: Ctx, creator: int, partner: int | None, partner_hint: str | None):
    st = await settings(c)
    if not st["new_deals"]:
        raise Refused(T.DEPOSITS_OFF)
    u = await user(c, creator)
    if u and u["banned"]:
        raise Refused(T.BANNED.format(support=ctx.cfg.support))
    if partner == creator:
        raise Refused(T.PARTNER_SELF)
    code = await _new_code(c)
    deal = await c.fetchrow(
        "INSERT INTO deals(code, state, creator, partner, partner_hint, fee_micro, deadline) "
        "VALUES($1,'INVITED',$2,$3,$4,$5,$6) RETURNING *",
        code, creator, partner, partner_hint, int(st["fee_micro"]), now() + timedelta(minutes=int(st["stage_minutes"])))
    await c.execute("INSERT INTO deal_parties(deal_id, tg_id, role) VALUES($1,$2,'creator')", deal["id"], creator)
    await event(c, deal["id"], creator, "create", {"partner": partner, "hint": partner_hint})
    if partner:
        await _send_invite(c, deal)
    return deal


async def _send_invite(c, deal):
    await say(c, deal["partner"], T.invite(await label(c, deal["creator"]), deal["code"]), kb_invite(deal["id"]), deal["id"])


async def claim_invite(c, ctx: Ctx, code: str, tg_id: int):
    """딥링크(start=join_<코드>)로 들어온 상대: 그 거래의 partner 로 묶는다 (아직 비어 있을 때만)."""
    d = await c.fetchrow("SELECT * FROM deals WHERE code=$1 FOR UPDATE", code.upper())
    if not d or d["state"] != "INVITED":
        raise Refused(T.STALE)
    if d["creator"] == tg_id:
        raise Refused(T.PARTNER_SELF)
    if d["partner"] and d["partner"] != tg_id:
        raise Refused(T.NOT_PARTY)
    u = await user(c, tg_id)
    if u and u["banned"]:
        raise Refused(T.BANNED.format(support=ctx.cfg.support))
    if not d["partner"]:
        d = await c.fetchrow("UPDATE deals SET partner=$2, updated_at=now() WHERE id=$1 RETURNING *", d["id"], tg_id)
        await event(c, d["id"], tg_id, "claim", {})
    await _send_invite(c, d)
    return d


async def answer_invite(c, ctx: Ctx, deal_id: int, tg_id: int, accept: bool):
    d = await _lock(c, deal_id)
    if tg_id != d["partner"]:
        raise Refused(T.NOT_PARTY)
    _expect(d, "INVITED")
    if not accept:
        d = await _set_state(c, d, "CANCELLED", tg_id, close_reason="declined")
        await say(c, d["creator"], T.declined(d["code"]), deal_id=d["id"])
        return d
    await c.execute("INSERT INTO deal_parties(deal_id, tg_id, role) VALUES($1,$2,'partner') ON CONFLICT DO NOTHING",
                    d["id"], tg_id)
    st = await settings(c)
    room = await c.fetchrow("SELECT * FROM rooms WHERE state='free' ORDER BY updated_at LIMIT 1 FOR UPDATE SKIP LOCKED")
    if room:
        await c.execute("UPDATE rooms SET state='used', deal_id=$2, updated_at=now() WHERE chat_id=$1", room["chat_id"], d["id"])
        d = await _set_state(c, d, "ROOM", tg_id, int(st["stage_minutes"]), room_id=room["chat_id"])
        await task(c, "room_open", {"deal_id": d["id"]}, room["chat_id"], d["id"])
        free = await c.fetchval("SELECT count(*) FROM rooms WHERE state='free'")
        if free <= 5:
            await say_admins(c, ctx, f"빈 거래방이 {free}개 남았어요. 방을 더 만들어 봇을 관리자로 넣어주세요.")
    else:
        await say_admins(c, ctx, f"빈 거래방이 없어서 <code>{d['code']}</code> 는 1:1 로 진행해요.")
        for p in (d["creator"], d["partner"]):
            await say(c, p, T.no_room(d["code"]), deal_id=d["id"])
        d = await _enter_notice(c, ctx, d, tg_id)
    return d


async def room_joined(c, ctx: Ctx, chat_id: int, tg_id: int) -> bool:
    """거래방 가입 신청: 그 방 거래의 당사자면 True(승인) + 입장 표시. 둘 다 들어오면 공지 단계."""
    room = await c.fetchrow("SELECT * FROM rooms WHERE chat_id=$1", chat_id)
    if not room or room["state"] != "used" or not room["deal_id"]:
        return False
    d = await _lock(c, room["deal_id"])
    if tg_id not in (d["creator"], d["partner"]) or d["state"] in CLOSED:
        return False
    await c.execute("UPDATE deal_parties SET joined=TRUE WHERE deal_id=$1 AND tg_id=$2", d["id"], tg_id)
    if d["state"] == "ROOM":
        n = await c.fetchval("SELECT count(*) FROM deal_parties WHERE deal_id=$1 AND joined", d["id"])
        if n >= 2:
            await _enter_notice(c, ctx, d, tg_id)
    return True


async def _enter_notice(c, ctx: Ctx, d, actor):
    st = await settings(c)
    d = await _set_state(c, d, "NOTICE", actor, int(st["stage_minutes"]))
    await say_deal(c, d, T.notice(d["code"], await label(c, d["creator"]), await label(c, d["partner"]),
                                  d["fee_micro"], int(st["min_micro"]), ctx.cfg.support), kb_notice(d["id"]))
    return d


async def ack_notice(c, ctx: Ctx, deal_id: int, tg_id: int):
    d = await _lock(c, deal_id)
    _party(d, tg_id)
    _expect(d, "NOTICE")
    await c.execute("UPDATE deal_parties SET notice_ok=TRUE WHERE deal_id=$1 AND tg_id=$2", deal_id, tg_id)
    rows = await c.fetch("SELECT tg_id, notice_ok FROM deal_parties WHERE deal_id=$1 ORDER BY role", deal_id)
    if all(r["notice_ok"] for r in rows) and len(rows) == 2:
        st = await settings(c)
        d = await _set_state(c, d, "CHOOSE", tg_id, int(st["stage_minutes"]))
        await say_deal(c, d, T.choose(d["code"], [(await label(c, r["tg_id"]), None) for r in rows]), kb_choose(deal_id))
    else:
        await say_deal(c, d, T.notice_status(d["code"], [(await label(c, r["tg_id"]), r["notice_ok"]) for r in rows]))
    return d


async def set_input(c, tg_id: int, kind: str, deal_id: int | None, ref_id: int | None, minutes: int, data: dict | None = None):
    await c.execute("INSERT INTO inputs(tg_id, kind, deal_id, ref_id, data, expires_at) VALUES($1,$2,$3,$4,$5,$6) "
                    "ON CONFLICT (tg_id) DO UPDATE SET kind=$2, deal_id=$3, ref_id=$4, data=$5, expires_at=$6",
                    tg_id, kind, deal_id, ref_id, data or {}, now() + timedelta(minutes=minutes))


async def choose(c, ctx: Ctx, deal_id: int, tg_id: int, deposit: bool) -> bool:
    """맡기기/안 맡기기. 둘 다 고르면 입금 단계로 → 맡기는 사람에게 1:1 로 금액을 묻는다. 입금 단계로 넘어가면 True."""
    d = await _lock(c, deal_id)
    _party(d, tg_id)
    _expect(d, "CHOOSE")
    await c.execute("UPDATE deal_parties SET choice=$3 WHERE deal_id=$1 AND tg_id=$2",
                    deal_id, tg_id, "deposit" if deposit else "none")
    rows = await c.fetch("SELECT tg_id, choice FROM deal_parties WHERE deal_id=$1 ORDER BY role", deal_id)
    labels = [(await label(c, r["tg_id"]), r["choice"]) for r in rows]
    if all(r["choice"] for r in rows):
        if not any(r["choice"] == "deposit" for r in rows):
            await c.execute("UPDATE deal_parties SET choice=NULL WHERE deal_id=$1", deal_id)
            await say_deal(c, d, T.BOTH_NONE, kb_choose(deal_id))
            return False
        st = await settings(c)
        d = await _set_state(c, d, "DEPOSIT", tg_id, int(st["slot_minutes"]) + int(st["slot_grace_minutes"]))
        await say_deal(c, d, T.choose(d["code"], labels))
        for r in rows:
            if r["choice"] == "deposit":
                await say(c, r["tg_id"], T.ask_amount(d["code"], int(st["min_micro"])), deal_id=deal_id)
                await set_input(c, r["tg_id"], "amount", deal_id, None, 60)
        return True
    await say_deal(c, d, T.choose(d["code"], labels), kb_choose(deal_id))
    return False


# ── 입금 ─────────────────────────────────────────────
async def open_slot(c, ctx: Ctx, deal_id: int, tg_id: int, base: int):
    """맡길 금액을 받아 끝자리 금액 자리를 만든다 → (slot). 같은 금액이 기다리는 중이면 0.0001 씩 올림 (최대 100번)."""
    if not ctx.cfg.can_take_deposits:
        raise Refused(T.DEPOSITS_OFF)
    d = await _lock(c, deal_id)
    _party(d, tg_id)
    _expect(d, "DEPOSIT")
    st = await settings(c)
    if base < int(st["min_micro"]) or base > 10_000_000 * MICRO:
        raise Refused(T.amount_bad(int(st["min_micro"])))
    choice = await c.fetchval("SELECT choice FROM deal_parties WHERE deal_id=$1 AND tg_id=$2", deal_id, tg_id)
    if choice != "deposit":
        raise Refused(T.STALE)
    old = await c.fetchrow("SELECT * FROM deposit_slots WHERE deal_id=$1 AND tg_id=$2 AND status IN ('waiting','received','confirmed')",
                           deal_id, tg_id)
    if old and old["status"] != "waiting":
        raise Refused("이미 입금이 들어온 거래예요.")
    if old:
        await c.execute("UPDATE deposit_slots SET status='cancelled', updated_at=now() WHERE id=$1", old["id"])
    expires = now() + timedelta(minutes=int(st["slot_minutes"]))
    step = 100   # 0.0001 USDT
    for i in range(1, 101):
        amount = base + i * step
        try:
            async with c.transaction():      # SAVEPOINT: 겹치면 이 시도만 되돌리고 다음 금액
                slot = await c.fetchrow(
                    "INSERT INTO deposit_slots(deal_id, tg_id, base_micro, amount_micro, status, expires_at) "
                    "VALUES($1,$2,$3,$4,'waiting',$5) RETURNING *", deal_id, tg_id, base, amount, expires)
            break
        except asyncpg.UniqueViolationError:
            continue
    else:
        raise Refused("지금 같은 금액대 입금이 몰려 있어요. 금액을 조금 바꿔서 다시 보내주세요.")
    deadline = expires + timedelta(minutes=int(st["slot_grace_minutes"]))
    if not d["deadline"] or d["deadline"] < deadline:
        await c.execute("UPDATE deals SET deadline=$2 WHERE id=$1", deal_id, deadline)
    await event(c, deal_id, tg_id, "slot", {"amount": amount})
    await say(c, tg_id, T.deposit_guide(d["code"], ctx.cfg.pay_address, amount, int(st["slot_minutes"])), deal_id=deal_id)
    return slot


async def record_incoming(c, ctx: Ctx, tx_id: str, idx: int, frm: str, to: str, amount: int, block_ts: datetime,
                          blacklisted: bool | None = None) -> str:
    """확정 블록에서 본 입금 한 건. 반환: matched / unmatched / flagged / dup."""
    if await c.fetchval("SELECT 1 FROM chain_tx WHERE tx_id=$1 AND idx=$2", tx_id, idx):
        return "dup"
    st = await settings(c)
    grace = timedelta(minutes=int(st["slot_grace_minutes"]))
    slot = await c.fetchrow("SELECT * FROM deposit_slots WHERE amount_micro=$1 AND status='waiting'", amount)
    if slot:
        await _lock(c, slot["deal_id"])
        slot = await c.fetchrow("SELECT * FROM deposit_slots WHERE id=$1 AND status='waiting' FOR UPDATE", slot["id"])
    status, note, slot_id = "unmatched", "", None
    if blacklisted:
        status, note = "flagged", "보낸 주소가 테더 동결 목록"
    elif slot and block_ts <= slot["expires_at"] + grace:
        status, slot_id = "matched", slot["id"]
    elif slot:
        note = "안내 시간 지나서 들어옴"
    await c.execute("INSERT INTO chain_tx(tx_id, idx, direction, from_addr, to_addr, amount_micro, block_ts, status, slot_id, note) "
                    "VALUES($1,$2,'in',$3,$4,$5,$6,$7,$8,$9)", tx_id, idx, frm, to, amount, block_ts, status, slot_id, note)
    if status != "matched":
        await say_admins(c, ctx, f"⚠️ 확인 필요한 입금 {fmt(amount)} USDT\n보낸 곳 <code>{frm}</code>\n"
                                 f"tx <code>{tx_id}</code>\n{note or '맞는 거래 없음'}")
        return status
    await _slot_received(c, ctx, slot, tx_id, actor=0)
    return "matched"


async def _slot_received(c, ctx: Ctx, slot, tx_id: str, actor: int):
    d = await _lock(c, slot["deal_id"])
    await c.execute("UPDATE deposit_slots SET status='received', tx_id=$2, updated_at=now() WHERE id=$1", slot["id"], tx_id)
    await event(c, d["id"], actor, "deposit_in", {"slot": slot["id"], "amount": slot["amount_micro"], "tx": tx_id})
    st = await settings(c)
    who_ = await label(c, slot["tg_id"])
    if d["state"] != "DEPOSIT":
        # 취소·만료 처리 중에 늦게 들어온 돈 → 환불 대상에 들어감 (MONEY_HELD). 관리자에게 알림.
        await say_admins(c, ctx, f"<code>{d['code']}</code> 상태 {d['state']} 에서 입금 {fmt(slot['amount_micro'])} USDT 들어옴 — 확인해주세요.")
        if d["state"] in CLOSED:
            await _open_refund(c, ctx, d, 0, reopen=True)
        return
    if is_auto(st, "deposit", slot["amount_micro"]):
        await _slot_confirm(c, ctx, d, slot["id"], actor)
    else:
        await say_deal(c, d, T.deposit_seen(d["code"], who_, slot["amount_micro"], True))
        await _ask_approval(c, ctx, d, "deposit", slot["id"],
                            f"{who_} 입금 {fmt(slot['amount_micro'])} USDT\ntx <code>{tx_id}</code>")


async def _slot_confirm(c, ctx: Ctx, d, slot_id: int, actor: int):
    slot = await c.fetchrow("UPDATE deposit_slots SET status='confirmed', updated_at=now() WHERE id=$1 AND status='received' "
                            "RETURNING *", slot_id)
    if not slot:
        return
    await event(c, d["id"], actor, "deposit_ok", {"slot": slot_id})
    await say_deal(c, d, T.deposit_confirmed(d["code"], await label(c, slot["tg_id"]), slot["amount_micro"]))
    await _maybe_funded(c, ctx, d, actor)


async def _maybe_funded(c, ctx: Ctx, d, actor: int):
    if d["state"] != "DEPOSIT":
        return
    rows = await c.fetch("""SELECT p.tg_id, p.choice, s.status FROM deal_parties p
                            LEFT JOIN deposit_slots s ON s.deal_id=p.deal_id AND s.tg_id=p.tg_id
                                 AND s.status IN ('waiting','received','confirmed')
                            WHERE p.deal_id=$1""", d["id"])
    if not all(r["status"] == "confirmed" for r in rows if r["choice"] == "deposit"):
        return
    st = await settings(c)
    total = await held(c, d["id"])
    if is_auto(st, "start", total):
        await _start(c, ctx, d, actor)
    else:
        d = await _set_state(c, d, "START_REVIEW", actor)
        await say_deal(c, d, T.START_WAIT)
        await _ask_approval(c, ctx, d, "start", d["id"], f"양쪽 예치 끝, 합계 {fmt(total)} USDT")


async def _start(c, ctx: Ctx, d, actor: int):
    d = await _set_state(c, d, "FUNDED", actor)
    bal = await balances(c, d["id"])
    rows = [(await label(c, p), v) for p, v in bal.items()]
    await say_deal(c, d, T.funded(d["code"], rows, sum(bal.values())), kb_funded(d["id"]))


async def held(c, deal_id: int, statuses=("confirmed",)) -> int:
    return int(await c.fetchval("SELECT coalesce(sum(amount_micro),0) FROM deposit_slots WHERE deal_id=$1 AND status = ANY($2)",
                                deal_id, list(statuses)))


async def balances(c, deal_id: int, statuses=("confirmed",)) -> dict[int, int]:
    """사람별 남은 몫 = 맡긴 돈 − 그 사람 몫에서 나간 부분 지급. 두 당사자 모두 키로 (0 이어도)."""
    parties = [r["tg_id"] for r in await c.fetch("SELECT tg_id FROM deal_parties WHERE deal_id=$1 ORDER BY role", deal_id)]
    out = {p: 0 for p in parties}
    for r in await c.fetch("SELECT tg_id, sum(amount_micro) s FROM deposit_slots WHERE deal_id=$1 AND status = ANY($2) GROUP BY tg_id",
                           deal_id, list(statuses)):
        out[r["tg_id"]] = out.get(r["tg_id"], 0) + int(r["s"])
    for r in await c.fetch("SELECT payer, sum(gross_micro) s FROM payouts WHERE deal_id=$1 AND kind='partial' "
                           "AND status <> 'cancelled' GROUP BY payer", deal_id):
        out[r["payer"]] = out.get(r["payer"], 0) - int(r["s"])
    return out


async def paid_out(c, deal_id: int) -> int:
    return int(await c.fetchval("SELECT coalesce(sum(gross_micro),0) FROM payouts WHERE deal_id=$1 AND status <> 'cancelled'",
                                deal_id))


# ── 관리자 확인 ───────────────────────────────────────
async def _ask_approval(c, ctx: Ctx, d, kind: str, ref_id: int, body: str):
    row = await c.fetchrow("INSERT INTO approvals(deal_id, kind, ref_id) VALUES($1,$2,$3) "
                           "ON CONFLICT (kind, ref_id) WHERE status IN ('pending','held') DO NOTHING RETURNING id",
                           d["id"] if d else None, kind, ref_id)
    if row:
        await say_admins(c, ctx, T.adm_approval(T.ADM_KIND.get(kind, kind), d["code"] if d else "-", body),
                         kb_approval(row["id"]), d["id"] if d else None)


async def decide(c, ctx: Ctx, approval_id: int, admin: int, approve: bool, note: str = "") -> str:
    """관리자 승인/보류. 반환: 결과 한 줄 (관리자에게 보여 줌)."""
    if admin not in ctx.admins:
        raise Refused("관리자만")
    a = await c.fetchrow("SELECT * FROM approvals WHERE id=$1 FOR UPDATE", approval_id)
    if not a or a["status"] not in ("pending", "held"):
        raise Refused("이미 처리된 건이에요.")
    if not approve:
        await c.execute("UPDATE approvals SET status='held', note=$2, decided_by=$3, decided_at=now() WHERE id=$1",
                        approval_id, note, admin)
        await event(c, a["deal_id"], admin, "approval_hold", {"id": approval_id, "kind": a["kind"]})
        return "보류했어요. 관리 페이지에서 다시 승인할 수 있어요."
    d = await _lock(c, a["deal_id"]) if a["deal_id"] else None
    kind = a["kind"]
    if kind == "deposit":
        await _slot_confirm(c, ctx, d, a["ref_id"], admin)
    elif kind == "start":
        if d["state"] != "START_REVIEW":
            raise Refused(f"지금 상태({d['state']})에선 시작 승인 못 해요.")
        await _start(c, ctx, d, admin)
    elif kind in ("partial", "final", "refund"):
        await _approve_split(c, ctx, d, a["ref_id"], admin)
    elif kind == "payout_done":
        await _payout_confirm(c, ctx, a["ref_id"], admin)
    else:
        raise Refused(f"모르는 종류 {kind}")
    await c.execute("UPDATE approvals SET status='approved', note=$2, decided_by=$3, decided_at=now() WHERE id=$1",
                    approval_id, note, admin)
    await event(c, a["deal_id"], admin, "approval_ok", {"id": approval_id, "kind": kind})
    return "승인했어요."


async def _void_approvals(c, deal_id: int, kinds: tuple[str, ...] | None = None):
    if kinds:
        await c.execute("UPDATE approvals SET status='void' WHERE deal_id=$1 AND status IN ('pending','held') AND kind = ANY($2)",
                        deal_id, list(kinds))
    else:
        await c.execute("UPDATE approvals SET status='void' WHERE deal_id=$1 AND status IN ('pending','held')", deal_id)


# ── 거래 중 ───────────────────────────────────────────
async def partial(c, ctx: Ctx, deal_id: int, payer: int, amount: int):
    """payer 몫에서 상대에게 먼저 보내기. 남는 돈이 수수료보다 적어지면 안 됨."""
    d = await _lock(c, deal_id)
    _party(d, payer)
    _expect(d, "FUNDED")
    bal = await balances(c, deal_id)
    remaining = sum(bal.values())
    if amount <= 0 or amount > bal.get(payer, 0) or remaining - amount < d["fee_micro"]:
        raise Refused(f"보낼 수 있는 금액을 넘어요. 최대 {fmt(max(0, min(bal.get(payer, 0), remaining - d['fee_micro'])))} USDT")
    to = _other(d, payer)
    sp = await c.fetchrow("INSERT INTO splits(deal_id, kind, plan, proposer, status) VALUES($1,'partial',$2,$3,'agreed') RETURNING id",
                          deal_id, {str(to): amount}, payer)
    po = await c.fetchrow("INSERT INTO payouts(deal_id, split_id, tg_id, payer, kind, gross_micro, fee_micro, amount_micro, status) "
                          "VALUES($1,$2,$3,$4,'partial',$5,0,$5,'need_address') RETURNING *", deal_id, sp["id"], to, payer, amount)
    await event(c, deal_id, payer, "partial", {"to": to, "amount": amount, "payout": po["id"]})
    await say_deal(c, d, T.partial_made(d["code"], await label(c, payer), await label(c, to), amount))
    await say(c, to, T.ask_address(d["code"], amount, 1), deal_id=deal_id)
    await set_input(c, to, "address", deal_id, po["id"], 7 * 24 * 60)
    return po


async def press_complete(c, ctx: Ctx, deal_id: int, tg_id: int, sure: bool):
    d = await _lock(c, deal_id)
    _party(d, tg_id)
    _expect(d, "FUNDED")
    if not sure:
        return d, False
    if await c.fetchval("SELECT count(*) FROM payouts WHERE deal_id=$1 AND kind='partial' AND status IN ('need_address','review','approved')",
                        deal_id):
        raise Refused("진행 중인 부분 지급이 끝난 뒤에 눌러주세요.")
    await c.execute("UPDATE deal_parties SET complete=TRUE WHERE deal_id=$1 AND tg_id=$2", deal_id, tg_id)
    rows = await c.fetch("SELECT tg_id, complete FROM deal_parties WHERE deal_id=$1", deal_id)
    if not all(r["complete"] for r in rows):
        await say_deal(c, d, T.complete_wait(d["code"], await label(c, _other(d, tg_id))))
        return d, False
    d = await _set_state(c, d, "SPLIT", tg_id)
    bal = await balances(c, deal_id)
    rows = [(await label(c, p), v) for p, v in bal.items()]
    await say_deal(c, d, T.split_ask(d["code"], rows, sum(bal.values()), d["fee_micro"]),
                   kb_split(deal_id, await label(c, d["creator"]), await label(c, d["partner"])))
    return d, True


async def propose_split(c, ctx: Ctx, deal_id: int, tg_id: int, how: str, custom_a: int | None = None):
    """how: each / a(전액 신청자) / b(전액 상대) / custom(신청자 몫 = custom_a, 나머지 상대)."""
    d = await _lock(c, deal_id)
    _party(d, tg_id)
    _expect(d, "SPLIT")
    bal = await balances(c, deal_id)
    total = sum(bal.values())
    a, b = d["creator"], d["partner"]
    if how == "each":
        plan = {a: bal.get(a, 0), b: bal.get(b, 0)}
    elif how == "a":
        plan = {a: total, b: 0}
    elif how == "b":
        plan = {a: 0, b: total}
    elif how == "custom":
        if custom_a is None or not 0 <= custom_a <= total:
            raise Refused(f"0 ~ {fmt(total)} 사이로 보내주세요.")
        plan = {a: custom_a, b: total - custom_a}
    else:
        raise Refused(T.STALE)
    await c.execute("UPDATE splits SET status='void' WHERE deal_id=$1 AND kind='final' AND status='proposed'", deal_id)
    sp = await c.fetchrow("INSERT INTO splits(deal_id, kind, plan, proposer, status) VALUES($1,'final',$2,$3,'proposed') RETURNING id",
                          deal_id, {str(k): v for k, v in plan.items()}, tg_id)
    fees = split_fee(plan, d["fee_micro"])
    rows = [(await label(c, p), plan[p], fees[p]) for p in plan]
    await event(c, deal_id, tg_id, "split_propose", {"split": sp["id"], "plan": {str(k): v for k, v in plan.items()}})
    await say_deal(c, d, T.split_proposed(d["code"], await label(c, tg_id), rows))
    await say(c, _other(d, tg_id), T.split_proposed(d["code"], await label(c, tg_id), rows), kb_split_agree(deal_id, sp["id"]), deal_id)
    return sp


async def answer_split(c, ctx: Ctx, deal_id: int, split_id: int, tg_id: int, agree: bool):
    d = await _lock(c, deal_id)
    _party(d, tg_id)
    _expect(d, "SPLIT")
    sp = await c.fetchrow("SELECT * FROM splits WHERE id=$1 AND deal_id=$2 FOR UPDATE", split_id, deal_id)
    if not sp or sp["status"] != "proposed":
        raise Refused(T.STALE)
    if sp["proposer"] == tg_id:
        raise Refused("제안한 사람 말고 상대가 동의해야 해요.")
    if not agree:
        await c.execute("UPDATE splits SET status='rejected' WHERE id=$1", split_id)
        await say_deal(c, d, T.split_rejected(d["code"], await label(c, tg_id)),
                       kb_split(deal_id, await label(c, d["creator"]), await label(c, d["partner"])))
        return d
    await c.execute("UPDATE splits SET status='agreed' WHERE id=$1", split_id)
    plan = {int(k): int(v) for k, v in sp["plan"].items()}
    if sum(plan.values()) != sum((await balances(c, deal_id)).values()):
        raise Refused("잔액이 바뀌었어요. 다시 정해주세요.")
    d = await _set_state(c, d, "ADDRESS", tg_id)
    await _make_payouts(c, ctx, d, split_id, plan, "final", d["fee_micro"])
    return d


async def _make_payouts(c, ctx: Ctx, d, split_id: int, plan: dict[int, int], kind: str, fee: int):
    """분배안 → 받는 사람마다 지급 줄(need_address). 지급 합계가 맡긴 돈을 넘으면 거절 (돈 규칙)."""
    statuses = ("received", "confirmed") if kind == "refund" else ("confirmed",)
    deposited = await held(c, d["id"], statuses)
    if await paid_out(c, d["id"]) + sum(v for v in plan.values() if v > 0) > deposited:
        raise Refused("맡긴 돈보다 많이 나갈 수 없어요.")
    fees = split_fee(plan, fee)
    made = 0
    for tg_id, gross in plan.items():
        if gross <= 0:
            continue
        po = await c.fetchrow(
            "INSERT INTO payouts(deal_id, split_id, tg_id, kind, gross_micro, fee_micro, amount_micro, status) "
            "VALUES($1,$2,$3,$4,$5,$6,$7,'need_address') RETURNING *",
            d["id"], split_id, tg_id, kind, gross, fees[tg_id], gross - fees[tg_id])
        if po["amount_micro"] <= 0:
            continue
        made += 1
        await say(c, tg_id, T.ask_address(d["code"], po["amount_micro"], 1), deal_id=d["id"])
        await set_input(c, tg_id, "address", d["id"], po["id"], 7 * 24 * 60)
    await event(c, d["id"], 0, "payouts", {"split": split_id, "kind": kind, "count": made})
    if not made:   # 받을 사람이 없음 (전부 0) — 바로 끝
        await _maybe_closed(c, ctx, d, 0)


async def set_address(c, ctx: Ctx, payout_id: int, tg_id: int, address: str):
    """두 번 입력·검사가 끝난 주소 저장 → 그 분배안의 주소가 다 모이면 관리자 확인(또는 자동 승인)."""
    po = await c.fetchrow("SELECT * FROM payouts WHERE id=$1 FOR UPDATE", payout_id)
    if not po or po["tg_id"] != tg_id or po["status"] != "need_address":
        raise Refused(T.STALE)
    d = await _lock(c, po["deal_id"])
    if d["state"] == "DISPUTE" and po["kind"] != "final":
        raise Refused(T.LOCKED)
    await c.execute("UPDATE payouts SET address=$2, status='review', updated_at=now() WHERE id=$1", payout_id, address)
    await c.execute("INSERT INTO addresses(tg_id, address) VALUES($1,$2) ON CONFLICT (tg_id, address) DO UPDATE SET last_used=now()",
                    tg_id, address)
    await c.execute("DELETE FROM inputs WHERE tg_id=$1 AND kind='address'", tg_id)
    await event(c, d["id"], tg_id, "address", {"payout": payout_id, "address": address})
    waiting = await c.fetchval("SELECT count(*) FROM payouts WHERE split_id=$1 AND status='need_address'", po["split_id"])
    st = await settings(c)
    kind = po["kind"]
    total = int(await c.fetchval("SELECT sum(amount_micro) FROM payouts WHERE split_id=$1 AND status <> 'cancelled'", po["split_id"]))
    sp = await c.fetchrow("SELECT * FROM splits WHERE id=$1", po["split_id"])
    manual = not (sp["kind"] == "dispute" or is_auto(st, kind, total))
    await say(c, tg_id, T.address_saved(po["amount_micro"], address, manual), deal_id=d["id"])
    if waiting:
        return
    if not manual:
        await _approve_split(c, ctx, d, po["split_id"], 0)
    else:
        lines = [f"{await label(c, r['tg_id'])}: {fmt(r['amount_micro'])} → <code>{r['address']}</code>"
                 for r in await c.fetch("SELECT * FROM payouts WHERE split_id=$1 AND status='review' ORDER BY id", po["split_id"])]
        await _ask_approval(c, ctx, d, kind, po["split_id"], "\n".join(lines))


async def _approve_split(c, ctx: Ctx, d, split_id: int, actor: int):
    rows = await c.fetch("UPDATE payouts SET status='approved', approved_by=$2, updated_at=now() "
                         "WHERE split_id=$1 AND status='review' RETURNING *", split_id, actor)
    if not rows:
        raise Refused("주소가 아직 다 안 모였어요.")
    await event(c, d["id"], actor, "payout_approved", {"split": split_id, "ids": [r["id"] for r in rows]})
    if d["state"] in ("ADDRESS",):
        d = await _set_state(c, d, "PAYOUT", actor)
    lines = "\n".join(f"{fmt(r['amount_micro'])} USDT → <code>{r['address']}</code>" for r in rows)
    await say_admins(c, ctx, f"💸 <b>보낼 것</b> · <code>{d['code']}</code>\n{lines}\n\n지갑에서 보내면 자동으로 확인돼요.", deal_id=d["id"])


async def record_outgoing(c, ctx: Ctx, tx_id: str, idx: int, frm: str, to: str, amount: int, block_ts: datetime) -> str:
    """우리 주소에서 나간 USDT → 승인된 지급 줄과 (주소, 금액)이 정확히 같으면 연결."""
    if await c.fetchval("SELECT 1 FROM chain_tx WHERE tx_id=$1 AND idx=$2", tx_id, idx):
        return "dup"
    po = await c.fetchrow("SELECT * FROM payouts WHERE status='approved' AND address=$1 AND amount_micro=$2 AND tx_id IS NULL "
                          "ORDER BY id LIMIT 1 FOR UPDATE", to, amount)
    status = "matched" if po else "unmatched"
    await c.execute("INSERT INTO chain_tx(tx_id, idx, direction, from_addr, to_addr, amount_micro, block_ts, status, payout_id) "
                    "VALUES($1,$2,'out',$3,$4,$5,$6,$7,$8)", tx_id, idx, frm, to, amount, block_ts, status, po["id"] if po else None)
    if not po:
        await say_admins(c, ctx, f"⚠️ 지급 목록에 없는 출금 {fmt(amount)} USDT → <code>{to}</code>\ntx <code>{tx_id}</code>")
        return status
    await _payout_sent(c, ctx, po, tx_id, 0)
    return status


async def _payout_sent(c, ctx: Ctx, po, tx_id: str, actor: int):
    await c.execute("UPDATE payouts SET status='sent', tx_id=$2, updated_at=now() WHERE id=$1", po["id"], tx_id)
    await event(c, po["deal_id"], actor, "payout_sent", {"payout": po["id"], "tx": tx_id})
    st = await settings(c)
    d = await c.fetchrow("SELECT * FROM deals WHERE id=$1", po["deal_id"])
    if is_auto(st, "payout_done", po["amount_micro"]) or actor:
        await _payout_confirm(c, ctx, po["id"], actor)
    else:
        await _ask_approval(c, ctx, d, "payout_done", po["id"],
                            f"{fmt(po['amount_micro'])} USDT → <code>{po['address']}</code>\ntx <code>{tx_id}</code>")


async def mark_payout_sent(c, ctx: Ctx, payout_id: int, tx_id: str, admin: int):
    """관리자가 직접: 지급 해시 입력 (자동 감지가 안 됐을 때). 바로 완료 처리."""
    if admin not in ctx.admins:
        raise Refused("관리자만")
    po = await c.fetchrow("SELECT * FROM payouts WHERE id=$1 FOR UPDATE", payout_id)
    if not po or po["status"] != "approved":
        raise Refused("승인된 지급만 완료 처리할 수 있어요.")
    tx_id = tx_id.strip().lower()
    if len(tx_id) != 64 or any(ch not in "0123456789abcdef" for ch in tx_id):
        raise Refused("tx 해시는 64자리 16진수예요.")
    await _payout_sent(c, ctx, po, tx_id, admin)


async def _payout_confirm(c, ctx: Ctx, payout_id: int, actor: int):
    po = await c.fetchrow("UPDATE payouts SET status='confirmed', updated_at=now() WHERE id=$1 AND status='sent' RETURNING *", payout_id)
    if not po:
        return
    d = await _lock(c, po["deal_id"])
    await event(c, d["id"], actor, "payout_ok", {"payout": payout_id})
    await say(c, po["tg_id"], T.payout_sent(d["code"], po["amount_micro"], po["tx_id"]), deal_id=d["id"])
    await _maybe_closed(c, ctx, d, actor)


async def _maybe_closed(c, ctx: Ctx, d, actor: int):
    """보낼 게 다 나갔으면 끝. 부분 지급이면 거래 계속."""
    open_ = await c.fetchval("SELECT count(*) FROM payouts WHERE deal_id=$1 AND status IN ('need_address','review','approved','sent')",
                             d["id"])
    if open_:
        return
    if d["state"] in ("PAYOUT", "ADDRESS"):
        d = await _set_state(c, d, "DONE", actor, close_reason="done")
        await say_deal(c, d, T.deal_done(d["code"]))
        await _close_room(c, d)
    elif d["state"] == "REFUND":
        d = await _set_state(c, d, "CANCELLED", actor, close_reason=d["close_reason"] or "refunded")
        await say_deal(c, d, T.cancel_done(d["code"], "맡긴 돈은 다 돌려드렸어요."))
        await _close_room(c, d)


async def _close_room(c, d):
    if d["room_id"]:
        await task(c, "room_close", {"deal_id": d["id"]}, d["room_id"], d["id"])
        await c.execute("UPDATE deals SET room_id=NULL WHERE id=$1", d["id"])   # 방은 다음 거래가 씀 → 이 거래 글은 이제 1:1 로
    await c.execute("UPDATE deposit_slots SET status='cancelled', updated_at=now() WHERE deal_id=$1 AND status='waiting'", d["id"])
    await c.execute("DELETE FROM inputs WHERE deal_id=$1", d["id"])


# ── 취소·환불·분쟁 ────────────────────────────────────
async def request_cancel(c, ctx: Ctx, deal_id: int, tg_id: int):
    """돈이 아직 없으면 바로 취소. 들어온 돈이 있으면 상대 동의 → 환불."""
    d = await _lock(c, deal_id)
    _party(d, tg_id)
    _expect(d, "INVITED", "ROOM", "NOTICE", "CHOOSE", "DEPOSIT", "START_REVIEW", "FUNDED")
    if not await held(c, deal_id, MONEY_HELD):
        d = await _set_state(c, d, "CANCELLED", tg_id, close_reason=f"cancel:{tg_id}")
        await _void_approvals(c, deal_id)
        await say_deal(c, d, T.cancel_done(d["code"]))
        await _close_room(c, d)
        return d
    if await c.fetchval("SELECT count(*) FROM payouts WHERE deal_id=$1 AND status IN ('approved','sent')", deal_id):
        raise Refused("송금이 진행 중이라 지금은 취소할 수 없어요. 관리자를 불러주세요.")
    await c.execute("UPDATE deal_parties SET cancel_req=TRUE WHERE deal_id=$1 AND tg_id=$2", deal_id, tg_id)
    rows = await c.fetch("SELECT cancel_req FROM deal_parties WHERE deal_id=$1", deal_id)
    if all(r["cancel_req"] for r in rows) and len(rows) == 2:
        return await _open_refund(c, ctx, d, tg_id)
    await say(c, _other(d, tg_id), T.cancel_asked(d["code"], await label(c, tg_id)), kb_cancel_agree(deal_id), deal_id)
    await say_deal(c, d, T.cancel_asked(d["code"], await label(c, tg_id)))
    return d


async def answer_cancel(c, ctx: Ctx, deal_id: int, tg_id: int, agree: bool):
    d = await _lock(c, deal_id)
    _party(d, tg_id)
    _expect(d, "NOTICE", "CHOOSE", "DEPOSIT", "START_REVIEW", "FUNDED")
    if not await c.fetchval("SELECT bool_or(cancel_req) FROM deal_parties WHERE deal_id=$1 AND tg_id<>$2", deal_id, tg_id):
        raise Refused(T.STALE)
    if not agree:
        await c.execute("UPDATE deal_parties SET cancel_req=FALSE WHERE deal_id=$1", deal_id)
        await say_deal(c, d, f"{await label(c, tg_id)} 님이 계속 진행하기로 했어요. (<code>{d['code']}</code>)")
        return d
    await c.execute("UPDATE deal_parties SET cancel_req=TRUE WHERE deal_id=$1 AND tg_id=$2", deal_id, tg_id)
    return await _open_refund(c, ctx, d, tg_id)


async def _open_refund(c, ctx: Ctx, d, actor: int, reopen: bool = False, reason: str = "cancel"):
    """들어온 돈(확인 전 포함)을 각자 맡긴 만큼 돌려줄 지급 줄 생성 → 주소 받기."""
    await _void_approvals(c, d["id"], ("deposit", "start"))
    await c.execute("UPDATE deposit_slots SET status='confirmed', updated_at=now() WHERE deal_id=$1 AND status='received'", d["id"])
    await c.execute("UPDATE deposit_slots SET status='cancelled', updated_at=now() WHERE deal_id=$1 AND status='waiting'", d["id"])
    # 아직 안 나간 지급은 취소 (주소 대기·확인 대기) → 그 몫도 돌려줌
    await c.execute("UPDATE payouts SET status='cancelled', updated_at=now() WHERE deal_id=$1 AND status IN ('need_address','review')",
                    d["id"])
    d = await _set_state(c, d, "REFUND", actor, close_reason=reason) if not reopen else \
        await c.fetchrow("UPDATE deals SET state='REFUND', closed_at=NULL, updated_at=now() WHERE id=$1 RETURNING *", d["id"])
    bal = await balances(c, d["id"])
    plan = {p: v for p, v in bal.items() if v > 0}
    sp = await c.fetchrow("INSERT INTO splits(deal_id, kind, plan, proposer, status) VALUES($1,'refund',$2,NULL,'agreed') RETURNING id",
                          d["id"], {str(k): v for k, v in plan.items()})
    st = await settings(c)
    await say_deal(c, d, T.refund_wait(d["code"]))
    await _make_payouts(c, ctx, d, sp["id"], plan, "refund", int(st["refund_fee_micro"]))
    return d


async def open_dispute(c, ctx: Ctx, deal_id: int, tg_id: int):
    d = await _lock(c, deal_id)
    _party(d, tg_id)
    if d["state"] in CLOSED or d["state"] == "DISPUTE":
        raise Refused(T.LOCKED if d["state"] == "DISPUTE" else T.STALE)
    if d["state"] in ("PAYOUT",):
        raise Refused("송금 단계예요. 문의는 고객센터로 해주세요.")
    d = await _set_state(c, d, "DISPUTE", tg_id, prev_state=d["state"])
    await say_deal(c, d, T.dispute_opened(d["code"], await label(c, tg_id)))
    await say_admins(c, ctx, f"🆘 <b>관리자 호출</b> · <code>{d['code']}</code>\n{await label(c, tg_id)} 님 · 이전 단계 {d['prev_state']}\n"
                             f"맡긴 돈 {fmt(await held(c, deal_id, MONEY_HELD))} USDT", deal_id=deal_id)
    return d


async def resolve_dispute(c, ctx: Ctx, deal_id: int, admin: int, plan: dict[int, int] | None, fee: int | None = None):
    """관리자 결정. plan=None 이면 분쟁 풀고 이전 단계로. plan 이면 그대로 지급 (주소 받은 뒤 바로 승인)."""
    if admin not in ctx.admins:
        raise Refused("관리자만")
    d = await _lock(c, deal_id)
    if d["state"] != "DISPUTE":
        raise Refused("분쟁 중인 거래가 아니에요.")
    if plan is None:
        d = await _set_state(c, d, d["prev_state"] or "FUNDED", admin)
        await say_deal(c, d, f"관리자가 확인을 마쳤어요. 거래를 이어서 진행하세요. (<code>{d['code']}</code>)",
                       kb_funded(deal_id) if d["state"] == "FUNDED" else None)
        return d
    await c.execute("UPDATE deposit_slots SET status='confirmed', updated_at=now() WHERE deal_id=$1 AND status='received'", deal_id)
    await c.execute("UPDATE payouts SET status='cancelled', updated_at=now() WHERE deal_id=$1 AND status IN ('need_address','review')",
                    deal_id)
    await _void_approvals(c, deal_id)
    remaining = sum((await balances(c, deal_id)).values())
    if any(v < 0 for v in plan.values()) or sum(plan.values()) != remaining or set(plan) - {d["creator"], d["partner"]}:
        raise Refused(f"합계가 남은 금액 {fmt(remaining)} USDT 와 같아야 해요.")
    fee = d["fee_micro"] if fee is None else max(0, int(fee))
    sp = await c.fetchrow("INSERT INTO splits(deal_id, kind, plan, proposer, status) VALUES($1,'dispute',$2,NULL,'agreed') RETURNING id",
                          deal_id, {str(k): v for k, v in plan.items()})
    d = await _set_state(c, d, "ADDRESS", admin)
    await event(c, deal_id, admin, "dispute_decide", {"plan": {str(k): v for k, v in plan.items()}, "fee": fee})
    await say_deal(c, d, T.dispute_decided(d["code"], [(await label(c, p), v) for p, v in plan.items()]))
    await _make_payouts(c, ctx, d, sp["id"], plan, "final", fee)
    return d


async def admin_cancel(c, ctx: Ctx, deal_id: int, admin: int, reason: str):
    """관리자 직권 취소: 돈 없으면 바로 끝, 있으면 환불."""
    if admin not in ctx.admins:
        raise Refused("관리자만")
    d = await _lock(c, deal_id)
    if d["state"] in CLOSED:
        raise Refused("이미 끝난 거래예요.")
    if await c.fetchval("SELECT count(*) FROM payouts WHERE deal_id=$1 AND status IN ('approved','sent')", deal_id):
        raise Refused("송금이 진행 중인 지급이 있어요. 먼저 그걸 끝내주세요.")
    if not await held(c, deal_id, MONEY_HELD):
        d = await _set_state(c, d, "CANCELLED", admin, close_reason=f"admin:{reason}"[:100])
        await _void_approvals(c, deal_id)
        await say_deal(c, d, T.cancel_done(d["code"], "관리자가 거래를 취소했어요."))
        await _close_room(c, d)
        return d
    return await _open_refund(c, ctx, d, admin, reason=f"admin:{reason}"[:100])


async def assign_deposit(c, ctx: Ctx, tx_id: str, idx: int, slot_id: int, admin: int):
    """확인 필요 입금을 관리자가 거래 자리에 연결 (금액이 조금 다름·늦게 옴). 자리 금액은 실제 받은 금액으로 고침."""
    if admin not in ctx.admins:
        raise Refused("관리자만")
    tx = await c.fetchrow("SELECT * FROM chain_tx WHERE tx_id=$1 AND idx=$2 FOR UPDATE", tx_id, idx)
    if not tx or tx["direction"] != "in" or tx["status"] not in ("unmatched", "flagged"):
        raise Refused("연결할 수 있는 입금이 아니에요.")
    slot = await c.fetchrow("SELECT * FROM deposit_slots WHERE id=$1", slot_id)
    if not slot or slot["status"] not in ("waiting", "expired"):
        raise Refused("그 입금 자리는 이미 처리됐어요.")
    d = await _lock(c, slot["deal_id"])
    await c.execute("UPDATE deposit_slots SET amount_micro=$2, base_micro=LEAST(base_micro,$2), status='received', tx_id=$3, "
                    "updated_at=now() WHERE id=$1", slot_id, tx["amount_micro"], tx_id)
    await c.execute("UPDATE chain_tx SET status='matched', slot_id=$3, note=note || ' · 관리자 연결' WHERE tx_id=$1 AND idx=$2",
                    tx_id, idx, slot_id)
    await event(c, d["id"], admin, "deposit_assign", {"tx": tx_id, "slot": slot_id, "amount": tx["amount_micro"]})
    if d["state"] == "DEPOSIT":
        await _slot_confirm(c, ctx, d, slot_id, admin)
    elif d["state"] in CLOSED or d["state"] == "REFUND":
        await _open_refund(c, ctx, d, admin, reopen=d["state"] in CLOSED)


async def mark_tx(c, ctx: Ctx, tx_id: str, idx: int, status: str, note: str, admin: int):
    """확인 필요 입금을 '돌려줌'·'무시' 로 표시 (실제 반환 송금은 관리자가 직접)."""
    if admin not in ctx.admins or status not in ("returned", "ignored"):
        raise Refused("관리자만 / returned·ignored 만")
    r = await c.execute("UPDATE chain_tx SET status=$3, note=$4 WHERE tx_id=$1 AND idx=$2 AND status IN ('unmatched','flagged')",
                        tx_id, idx, status, note[:200])
    if r.endswith(" 0"):
        raise Refused("처리할 수 없는 입금이에요.")
    await event(c, None, admin, "tx_mark", {"tx": tx_id, "status": status, "note": note[:200]})


# ── 시간 초과 ─────────────────────────────────────────
async def expire_due(c, ctx: Ctx) -> int:
    """30초마다: 시간 넘긴 거래 정리. 들어온 돈이 있으면 환불, 없으면 취소. 처리한 수."""
    st = await settings(c)
    grace = timedelta(minutes=int(st["slot_grace_minutes"]))
    await c.execute("UPDATE deposit_slots SET status='expired', updated_at=now() WHERE status='waiting' AND expires_at + $1 < now()",
                    grace)
    ids = [r["id"] for r in await c.fetch("SELECT id FROM deals WHERE state = ANY($1) AND deadline < now() "
                                          "ORDER BY deadline LIMIT 50", list(EXPIRING))]
    n = 0
    for deal_id in ids:
        try:
            n += await _expire_one(c, ctx, deal_id)
        except Exception:   # noqa: BLE001 — 한 거래 문제로 나머지 정리가 멈추지 않게 (그 거래만 되돌림)
            log.exception("시간 초과 처리 실패 deal=%s", deal_id)
    await c.execute("DELETE FROM inputs WHERE expires_at < now()")
    return n


async def _expire_one(c, ctx: Ctx, deal_id: int) -> int:
    if True:
        async with c.transaction():
            d = await _lock(c, deal_id)
            if d["state"] not in EXPIRING or not d["deadline"] or d["deadline"] >= now():
                return 0
            if d["state"] == "DEPOSIT" and await c.fetchval(
                    "SELECT count(*) FROM deposit_slots WHERE deal_id=$1 AND status='waiting'", deal_id):
                return 0   # 아직 안내 시간 안
            if d["state"] == "DEPOSIT" and await c.fetchval(
                    "SELECT count(*) FROM approvals WHERE deal_id=$1 AND kind='deposit' AND status IN ('pending','held')", deal_id):
                return 0   # 관리자 입금 확인 기다리는 중 → 손님 잘못 아님
            if await held(c, deal_id, MONEY_HELD):
                await say_deal(c, d, T.expired(d["code"], True))
                await _open_refund(c, ctx, d, 0, reason="expired")
            else:
                d = await _set_state(c, d, "CANCELLED", 0, close_reason="expired")
                await _void_approvals(c, deal_id)
                await say_deal(c, d, T.expired(d["code"], False))
                await _close_room(c, d)
            return 1


# ── 거래방 풀 ─────────────────────────────────────────
async def room_register(c, ctx: Ctx, chat_id: int, title: str, ok: bool, note: str = ""):
    """봇이 관리자로 들어간 그룹 → 풀에. ok=False(권한 부족·기록 공개)면 broken."""
    row = await c.fetchrow("SELECT * FROM rooms WHERE chat_id=$1 FOR UPDATE", chat_id)
    state = "free" if ok else "broken"
    if row and row["state"] == "used":
        await c.execute("UPDATE rooms SET title=$2, note=$3, updated_at=now() WHERE chat_id=$1", chat_id, title, note)
        return
    await c.execute("INSERT INTO rooms(chat_id, title, state, note) VALUES($1,$2,$3,$4) ON CONFLICT (chat_id) DO UPDATE "
                    "SET title=EXCLUDED.title, state=EXCLUDED.state, note=EXCLUDED.note, deal_id=NULL, updated_at=now()",
                    chat_id, title[:100], state, note)
    await event(c, None, 0, "room", {"chat": chat_id, "state": state, "note": note})
    await say_admins(c, ctx, f"거래방 {'등록' if ok else '등록 안 됨'}: {T.esc(title)}" + (f"\n{T.esc(note)}" if note else ""))


async def room_freed(c, chat_id: int):
    await c.execute("UPDATE rooms SET state='free', deal_id=NULL, invite_link=NULL, updated_at=now() WHERE chat_id=$1 AND state='used'",
                    chat_id)


async def room_broken(c, ctx: Ctx, chat_id: int, note: str):
    await c.execute("UPDATE rooms SET state='broken', note=$2, updated_at=now() WHERE chat_id=$1", chat_id, note[:200])
    await say_admins(c, ctx, f"⚠️ 거래방 문제: {chat_id}\n{T.esc(note)}")


# ── 조회 (메뉴·관리 페이지) ───────────────────────────
async def user_deals(c, tg_id: int, kind: str):
    if kind == "active":
        where = "state <> ALL($2) AND state <> 'REFUND'"
        arg = list(CLOSED)
    elif kind == "refund":
        where = "state = 'REFUND' AND $2::text[] IS NOT NULL"
        arg = []
    else:
        where = "state = ANY($2)"
        arg = list(CLOSED)
    return await c.fetch(f"SELECT * FROM deals WHERE (creator=$1 OR partner=$1) AND {where} ORDER BY id DESC LIMIT 20", tg_id, arg)


def dumps(v) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


async def room_failed(c, ctx: Ctx, deal_id: int, chat_id: int, note: str):
    """거래방 링크를 못 만들었을 때: 그 방은 고장 표시, 거래는 1:1 로 이어감."""
    await room_broken(c, ctx, chat_id, note)
    d = await _lock(c, deal_id)
    if d["state"] != "ROOM" or d["room_id"] != chat_id:
        return
    d = await c.fetchrow("UPDATE deals SET room_id=NULL WHERE id=$1 RETURNING *", deal_id)
    for p in (d["creator"], d["partner"]):
        await say(c, p, T.no_room(d["code"]), deal_id=deal_id)
    await _enter_notice(c, ctx, d, 0)


async def stats(c, pay_balance: int | None = None) -> dict:
    """관리 페이지 대시보드 숫자."""
    held_ = await c.fetchval("""SELECT coalesce(sum(s.amount_micro),0) FROM deposit_slots s JOIN deals d ON d.id=s.deal_id
                                WHERE s.status IN ('received','confirmed')""")
    out_ = await c.fetchval("SELECT coalesce(sum(amount_micro),0) FROM payouts WHERE status IN ('sent','confirmed')")
    fees = await c.fetchval("SELECT coalesce(sum(fee_micro),0) FROM payouts WHERE status IN ('sent','confirmed')")
    return {
        "active": await c.fetchval("SELECT count(*) FROM deals WHERE state <> ALL($1)", list(CLOSED)),
        "today": await c.fetchval("SELECT count(*) FROM deals WHERE created_at > now() - interval '1 day'"),
        "done_total": await c.fetchval("SELECT count(*) FROM deals WHERE state='DONE'"),
        "ledger_micro": int(held_) - int(out_),        # 장부상 지갑에 있어야 할 돈 (맡긴 돈 − 실제로 보낸 돈, 번 수수료 포함)
        "fees_micro": int(fees),
        "pending_approvals": await c.fetchval("SELECT count(*) FROM approvals WHERE status='pending'"),
        "held_approvals": await c.fetchval("SELECT count(*) FROM approvals WHERE status='held'"),
        "payouts_to_send": await c.fetchval("SELECT count(*) FROM payouts WHERE status='approved'"),
        "unmatched_in": await c.fetchval("SELECT count(*) FROM chain_tx WHERE status IN ('unmatched','flagged')"),
        "disputes": await c.fetchval("SELECT count(*) FROM deals WHERE state='DISPUTE'"),
        "rooms_free": await c.fetchval("SELECT count(*) FROM rooms WHERE state='free'"),
        "wallet_micro": pay_balance,
    }
