"""거래 규칙 (텔레그램·웹과 무관한 순수 로직 + DB).

- 모든 함수는 이미 열린 트랜잭션 연결 `c` 를 받는다. 거래를 바꾸는 함수는 맨 처음 `_lock(c, deal_id)` 로 그 줄을 잠근다
  (버튼 연타·두 사람 동시 클릭·관리자와 봇 동시 처리에도 한 번만).
- 텔레그램으로 보낼 것은 같은 트랜잭션에서 outbox 에 넣는다 → 커밋돼야만 나가고, 재시작해도 안 잃는다.
- 돈 규칙: 지급 합계 ≤ 맡긴 합계. 지급 줄을 만들 때마다 같은 트랜잭션에서 다시 센다.
"""
from __future__ import annotations

import html
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
DUST_MICRO = 1 * 1_000_000      # 1 USDT 미만 + 맞는 거래 없음 = 먼지·주소 사칭 tx → 기록만, 알림 X

DEFAULTS = {
    "fee_micro": 20 * MICRO,      # 고정 수수료 (비율 방식이면 최소 수수료)
    "fee_percent_bp": 0,          # 0 = 고정. 예: 150 = 예치 합계의 1.5%
    "fee_max_micro": 0,           # 비율 방식 최대 (0 = 없음)
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
SETTING_MIN = {"stage_minutes": 5, "slot_minutes": 10, "min_micro": 1 * MICRO}   # 0분 단계·0원 최소 같은 실수 막기
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
        if not isinstance(value, bool):          # "false" 같은 글자가 True 로 바뀌지 않게
            raise Refused(f"{key}: 켜기/끄기(true/false)만")
    elif isinstance(d, int):
        if isinstance(value, bool) or not isinstance(value, int):
            raise Refused(f"{key}: 정수만")
        if value < SETTING_MIN.get(key, 0):
            raise Refused(f"{key}: {SETTING_MIN.get(key, 0)} 이상")
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


async def upsert_user(c, tg_id: int, username: str | None, name: str, dm: bool = False) -> asyncpg.Record:
    """dm=True: 봇과 1:1 에서 뭔가 한 사람 (봇이 먼저 1:1 을 보낼 수 있음)."""
    if username:      # 아이디를 바꾼 예전 주인 줄에 남은 같은 아이디 → 엉뚱한 사람에게 요청 가지 않게
        await c.execute("UPDATE users SET username=NULL WHERE lower(username)=lower($2) AND tg_id<>$1", tg_id, username)
    return await c.fetchrow(
        "INSERT INTO users(tg_id, username, name, dm_ok) VALUES($1,$2,$3,$4) ON CONFLICT (tg_id) DO UPDATE "
        "SET username=EXCLUDED.username, name=EXCLUDED.name, dm_ok=users.dm_ok OR EXCLUDED.dm_ok, updated_at=now() RETURNING *",
        tg_id, username, name[:64], dm)


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
    return [[["예치하기", f"d:dep:{deal_id}"], ["예치 안 함", f"d:nodep:{deal_id}"]],
            [["관리자 호출", f"d:sos:{deal_id}"], ["거래 취소", f"d:cx:{deal_id}"]]]


def kb_funded(deal_id):
    return [[["부분 지급", f"d:part:{deal_id}"], ["거래완료", f"d:fin:{deal_id}"]],
            [["관리자 호출", f"d:sos:{deal_id}"], ["거래 취소", f"d:cx:{deal_id}"]]]


def kb_complete_confirm(deal_id):
    return [[["예", f"d:finy:{deal_id}"], ["아니요", f"d:finn:{deal_id}"]]]


def kb_split(deal_id, a_label: str, b_label: str):
    a_label, b_label = html.unescape(a_label), html.unescape(b_label)     # label() 은 HTML 용 (& → &amp;) — 버튼엔 원래 글자
    return [[["각자 예치한 만큼 돌려받기", f"d:sp:each:{deal_id}"]],
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
async def create_deal(c, ctx: Ctx, creator: int, partner: int | None, partner_hint: str | None, open_link: bool = False):
    """open_link: 아이디(@) 없는 상대용 — 링크를 연 사람을 신청자가 직접 확인하고 나서 상대로 묶음."""
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
        "INSERT INTO deals(code, state, creator, partner, partner_hint, fee_micro, deadline, fee_bp, fee_max_micro, open_link) "
        "VALUES($1,'INVITED',$2,$3,$4,$5,$6,$7,$8,$9) RETURNING *",
        code, creator, partner, partner_hint, int(st["fee_micro"]), now() + timedelta(minutes=int(st["stage_minutes"])),
        int(st["fee_percent_bp"]), int(st["fee_max_micro"]), open_link and not partner and not partner_hint)
    await c.execute("INSERT INTO deal_parties(deal_id, tg_id, role) VALUES($1,$2,'creator')", deal["id"], creator)
    await event(c, deal["id"], creator, "create", {"partner": partner, "hint": partner_hint})
    if partner:
        await _send_invite(c, deal)
    return deal


async def track_record(c, tg_id: int) -> tuple[int, int]:
    """이 봇에서 끝낸 거래 수, 분쟁이 열렸던 거래 수."""
    done = await c.fetchval("SELECT count(*) FROM deals WHERE (creator=$1 OR partner=$1) AND state='DONE'", tg_id)
    disputes = await c.fetchval("SELECT count(DISTINCT e.deal_id) FROM events e JOIN deals d ON d.id=e.deal_id "
                                "WHERE (d.creator=$1 OR d.partner=$1) AND e.kind='state' AND e.data->>'to'='DISPUTE'", tg_id)
    return int(done), int(disputes)


async def _send_invite(c, deal):
    done, disputes = await track_record(c, deal["creator"])
    await say(c, deal["partner"], T.invite(await label(c, deal["creator"]), deal["code"], deal["creator"], done, disputes),
              kb_invite(deal["id"]), deal["id"])


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
    hint = (d["partner_hint"] or "").lower()
    if not d["partner"] and hint and (not u or (u["username"] or "").lower() != hint):
        raise Refused(f"이 링크는 @{T.esc(d['partner_hint'])} 님 전용이에요.")
    if not d["partner"] and d["open_link"]:
        # 아이디 없는 상대 링크: 링크는 누구에게나 전달될 수 있으니, 연 사람을 신청자가 확인해야 상대로 묶음
        if d["pending_partner"] and d["pending_partner"] != tg_id:
            raise Refused("다른 분이 이 링크로 먼저 들어와서 신청자 확인을 기다리고 있어요.")
        await c.execute("UPDATE deals SET pending_partner=$2, updated_at=now() WHERE id=$1", d["id"], tg_id)
        await event(c, d["id"], tg_id, "claim_wait", {})
        done, disputes = await track_record(c, tg_id)
        u2 = await user(c, tg_id)
        await say(c, d["creator"], T.claim_confirm(d["code"], await label(c, tg_id), tg_id, (u2["name"] if u2 else ""), done, disputes),
                  [[["이 사람 맞아요", f"d:pok:{d['id']}"], ["아니에요", f"d:pno:{d['id']}"]]], d["id"])
        await say(c, tg_id, T.claim_wait(d["code"]), deal_id=d["id"])
        return d
    if not d["partner"]:
        d = await c.fetchrow("UPDATE deals SET partner=$2, updated_at=now() WHERE id=$1 RETURNING *", d["id"], tg_id)
        await event(c, d["id"], tg_id, "claim", {})
        await say(c, d["creator"], f"{await label(c, tg_id)} 님이 링크로 들어왔어요. 수락하면 이어집니다. (<code>{d['code']}</code>)",
                  deal_id=d["id"])
    await _send_invite(c, d)
    return d


async def confirm_claim(c, ctx: Ctx, deal_id: int, tg_id: int, ok: bool):
    """신청자가 '링크로 들어온 사람'을 확인. 맞으면 상대로 묶고 요청을 보냄, 아니면 비워서 다른 사람이 들어올 수 있게."""
    d = await _lock(c, deal_id)
    if d["creator"] != tg_id:
        raise Refused(T.NOT_PARTY)
    if d["state"] != "INVITED" or d["partner"] or not d["pending_partner"]:
        raise Refused(T.STALE)
    who = d["pending_partner"]
    if not ok:
        await c.execute("UPDATE deals SET pending_partner=NULL, updated_at=now() WHERE id=$1", deal_id)
        await event(c, deal_id, tg_id, "claim_reject", {"who": who})
        await say(c, who, T.claim_rejected(d["code"]), deal_id=deal_id)
        return d
    d = await c.fetchrow("UPDATE deals SET partner=$2, pending_partner=NULL, updated_at=now() WHERE id=$1 RETURNING *", deal_id, who)
    await event(c, deal_id, tg_id, "claim_ok", {"who": who})
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
        if free <= 5 and not await c.fetchval("SELECT 1 FROM kv WHERE key='helper' AND value ? 'session'"):
            await say_admins(c, ctx, f"빈 거래방이 {free}개 남았어요. 방을 더 만들어 봇을 관리자로 넣어주세요.")
    elif await c.fetchval("SELECT 1 FROM kv WHERE key='helper' AND value ? 'session'"):
        # 빈 방이 없으면 도우미 계정이 새로 만듦 (봇 프로세스의 outbox 가 처리 → room_created / room_create_failed)
        d = await _set_state(c, d, "ROOM", tg_id, int(st["stage_minutes"]))
        await task(c, "room_create", {"deal_id": d["id"]}, None, d["id"])
        for p in (d["creator"], d["partner"]):
            await say(c, p, T.room_making(d["code"]), deal_id=d["id"])
    else:
        await say_admins(c, ctx, f"빈 거래방이 없어서 <code>{d['code']}</code> 는 1:1 로 진행해요.")
        for p in (d["creator"], d["partner"]):
            await say(c, p, T.no_room(d["code"]), deal_id=d["id"])
        d = await _enter_notice(c, ctx, d, tg_id)
    return d


async def room_created(c, ctx: Ctx, deal_id: int, chat_id: int, title: str):
    """도우미가 만든 방 → 그 거래에 붙이고 링크 보내기. 그 사이 거래가 끝났으면 방은 빈 방으로."""
    d = await _lock(c, deal_id)
    ok = d["state"] == "ROOM" and not d["room_id"]
    await c.execute("INSERT INTO rooms(chat_id, title, state, deal_id, note) VALUES($1,$2,$3,$4,'도우미가 만듦') "
                    "ON CONFLICT (chat_id) DO UPDATE SET state=EXCLUDED.state, deal_id=EXCLUDED.deal_id, title=EXCLUDED.title, "
                    "updated_at=now()", chat_id, title[:100], "used" if ok else "free", deal_id if ok else None)
    await event(c, deal_id, 0, "room_created", {"chat": chat_id, "used": ok})
    if ok:
        await c.execute("UPDATE deals SET room_id=$2, updated_at=now() WHERE id=$1", deal_id, chat_id)
        await task(c, "room_open", {"deal_id": deal_id}, chat_id, deal_id)


async def room_create_failed(c, ctx: Ctx, deal_id: int, note: str):
    """도우미가 방을 못 만듦 → 1:1 로 이어감 + 관리자 알림."""
    d = await _lock(c, deal_id)
    await say_admins(c, ctx, f"⚠️ 거래방 자동 생성 실패: {T.esc(note)[:200]}\n<code>{d['code']}</code> 는 1:1 로 진행해요.")
    if d["state"] != "ROOM" or d["room_id"]:
        return
    for p in (d["creator"], d["partner"]):
        await say(c, p, T.no_room(d["code"]), deal_id=deal_id)
    await _enter_notice(c, ctx, d, 0)


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
                                  d["fee_micro"], int(st["min_micro"]), ctx.cfg.support, ctx.cfg.bot_username,
                                  d["fee_bp"], d["fee_max_micro"]), kb_notice(d["id"]))
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
SLOT_STEP = 100                    # 0.0001 USDT
SLOT_STEPS = 999                   # 끝자리 0.0001 ~ 0.0999
AMOUNT_HOLD = timedelta(hours=24)  # 끝난 자리 금액을 다시 주기 전 쉬는 시간
CLOCK_SKEW = timedelta(minutes=1)  # 블록 시각 vs 서버 시각 오차 허용
MAX_OPEN_SLOTS = 3                 # 한 사람이 동시에 기다릴 수 있는 입금 수 (끝자리 금액을 일부러 다 잡는 것 막기)


def _offsets() -> list[int]:
    """끝자리 후보 순서: 무작위 시작에서 한 바퀴 (다음 금액을 짐작 못 하게). 테스트는 1,2,3… 로 바꿔 끼움."""
    start = secrets.randbelow(SLOT_STEPS)
    return [(start + k) % SLOT_STEPS + 1 for k in range(SLOT_STEPS)]


async def open_slot(c, ctx: Ctx, deal_id: int, tg_id: int, base: int):
    """예치 금액을 받아 끝자리 금액 자리를 만든다 → (slot). 끝자리 0.0001~0.0999 중 최근 24시간 안 쓴 것을 무작위 순서로."""
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
    if await c.fetchval("SELECT count(*) FROM deposit_slots WHERE tg_id=$1 AND status='waiting' AND deal_id<>$2", tg_id, deal_id) >= MAX_OPEN_SLOTS:
        raise Refused("입금 기다리는 거래가 너무 많아요. 먼저 하나를 끝내거나 취소해주세요.")
    if old:
        await c.execute("UPDATE deposit_slots SET status='cancelled', updated_at=now() WHERE id=$1", old["id"])
    expires = now() + timedelta(minutes=int(st["slot_minutes"]))
    # 최근 24시간 안에 쓰인 끝자리 금액은 다시 안 줌 — 끝난 자리로 늦게 온 돈이 새 거래로 잡히지 않게
    used = {r["amount_micro"] for r in await c.fetch(
        "SELECT amount_micro FROM deposit_slots WHERE amount_micro BETWEEN $1 AND $2 AND (status='waiting' OR updated_at > now() - $3::interval)",
        base + SLOT_STEP, base + SLOT_STEPS * SLOT_STEP, AMOUNT_HOLD)}
    slot = None
    for i in [k for k in _offsets() if base + k * SLOT_STEP not in used][:20]:
        amount = base + i * SLOT_STEP
        try:
            async with c.transaction():      # SAVEPOINT: 동시에 같은 금액을 잡으면 이 시도만 되돌리고 다음 금액
                slot = await c.fetchrow(
                    "INSERT INTO deposit_slots(deal_id, tg_id, base_micro, amount_micro, status, expires_at) "
                    "VALUES($1,$2,$3,$4,'waiting',$5) RETURNING *", deal_id, tg_id, base, amount, expires)
            break
        except asyncpg.UniqueViolationError:
            continue
    if slot is None:
        raise Refused("지금 같은 금액대 입금이 몰려 있어요. 금액을 조금 바꿔서 다시 보내주세요.")
    deadline = expires + timedelta(minutes=int(st["slot_grace_minutes"]))
    if not d["deadline"] or d["deadline"] < deadline:
        await c.execute("UPDATE deals SET deadline=$2 WHERE id=$1", deal_id, deadline)
    await event(c, deal_id, tg_id, "slot", {"amount": amount})
    await say(c, tg_id, T.deposit_guide(d["code"], ctx.cfg.pay_address, amount, int(st["slot_minutes"])),
              [[["금액 다시 입력", f"d:amt:{deal_id}"]]], deal_id=deal_id)
    return slot


async def record_incoming(c, ctx: Ctx, tx_id: str, idx: int, frm: str, to: str, amount: int, block_ts: datetime,
                          blacklisted: bool | None = False) -> str:
    """확정 블록에서 본 입금 한 건. blacklisted: True 동결 / False 깨끗 / None 조회 실패(→ 자동 모드여도 관리자 확인).
    반환: matched / unmatched / flagged / ignored(먼지) / dup."""
    if await c.fetchval("SELECT 1 FROM chain_tx WHERE tx_id=$1 AND idx=$2", tx_id, idx):
        return "dup"
    st = await settings(c)
    grace = timedelta(minutes=int(st["slot_grace_minutes"]))
    # 금액 + 시간 창으로 찾음: 자리를 만든 뒤 ~ 안내 시간+여유 안에 들어온 돈만. 감시가 늦어 자리가 먼저 만료됐어도 인정.
    slot = await c.fetchrow("SELECT * FROM deposit_slots WHERE amount_micro=$1 AND status IN ('waiting','expired') "
                            "AND created_at - $3::interval <= $2 AND $2 <= expires_at + $4::interval ORDER BY created_at DESC LIMIT 1",
                            amount, block_ts, CLOCK_SKEW, grace)
    note = ""
    if slot:
        await _lock(c, slot["deal_id"])
        slot = await c.fetchrow("SELECT * FROM deposit_slots WHERE id=$1 AND status IN ('waiting','expired') FOR UPDATE", slot["id"])
        if slot and slot["status"] == "expired" and await c.fetchval(
                "SELECT 1 FROM deposit_slots WHERE deal_id=$1 AND tg_id=$2 AND status IN ('waiting','received','confirmed')",
                slot["deal_id"], slot["tg_id"]):
            slot, note = None, "만료된 자리 금액 — 그 사람은 새 입금 자리가 있음"
    elif await c.fetchval("SELECT 1 FROM deposit_slots WHERE amount_micro=$1 AND updated_at > now() - $2::interval", amount, AMOUNT_HOLD):
        note = "안내 시간 밖에 들어옴"
    status, slot_id = "unmatched", None
    if blacklisted:
        status, note = "flagged", "보낸 주소가 테더 동결 목록"
    elif slot:
        status, slot_id = "matched", slot["id"]
        if blacklisted is None:
            note = "동결 목록 조회 실패 — 관리자 확인"
    elif amount < DUST_MICRO and not note:
        status, note = "ignored", "먼지 입금"
    await c.execute("INSERT INTO chain_tx(tx_id, idx, direction, from_addr, to_addr, amount_micro, block_ts, status, slot_id, note) "
                    "VALUES($1,$2,'in',$3,$4,$5,$6,$7,$8,$9)", tx_id, idx, frm, to, amount, block_ts, status, slot_id, note)
    if status == "ignored":
        return status
    if status != "matched":
        await say_admins(c, ctx, f"⚠️ 확인 필요한 입금 {fmt(amount)} USDT\n보낸 곳 <code>{frm}</code>\n"
                                 f"tx <code>{tx_id}</code>\n{note or '맞는 거래 없음'}")
        return status
    await _slot_received(c, ctx, slot, tx_id, actor=0, force_manual=blacklisted is None)
    return "matched"


async def _slot_received(c, ctx: Ctx, slot, tx_id: str, actor: int, force_manual: bool = False):
    d = await _lock(c, slot["deal_id"])
    await c.execute("UPDATE deposit_slots SET status='received', tx_id=$2, updated_at=now() WHERE id=$1", slot["id"], tx_id)
    await event(c, d["id"], actor, "deposit_in", {"slot": slot["id"], "amount": slot["amount_micro"], "tx": tx_id})
    st = await settings(c)
    who_ = await label(c, slot["tg_id"])
    if d["state"] != "DEPOSIT":
        await say_admins(c, ctx, f"<code>{d['code']}</code> 상태 {d['state']} 에서 입금 {fmt(slot['amount_micro'])} USDT 들어옴.")
        if d["state"] in CLOSED or d["state"] == "REFUND":
            await _open_refund(c, ctx, d, 0, reopen=True)       # 취소·만료·환불 뒤 늦게 온 돈 → 그 돈도 환불
        elif d["state"] != "DISPUTE":                            # 분쟁 중이면 분쟁 처리 때 같이 (_resume / resolve)
            await _ask_approval(c, ctx, d, "deposit", slot["id"],
                                f"{who_} 입금 {fmt(slot['amount_micro'])} USDT (상태 {d['state']} 에서)\ntx <code>{tx_id}</code>")
        return
    if not force_manual and is_auto(st, "deposit", slot["amount_micro"]):
        await _slot_confirm(c, ctx, d, slot["id"], actor)
    else:
        await say_deal(c, d, T.deposit_seen(d["code"], who_, slot["base_micro"], True))
        await _ask_approval(c, ctx, d, "deposit", slot["id"],
                            f"{who_} 입금 {fmt(slot['amount_micro'])} USDT\ntx <code>{tx_id}</code>")


async def _slot_confirm(c, ctx: Ctx, d, slot_id: int, actor: int):
    slot = await c.fetchrow("UPDATE deposit_slots SET status='confirmed', updated_at=now() WHERE id=$1 AND status='received' "
                            "RETURNING *", slot_id)
    if not slot:
        return
    await event(c, d["id"], actor, "deposit_ok", {"slot": slot_id})
    await say_deal(c, d, T.deposit_confirmed(d["code"], await label(c, slot["tg_id"]), slot["base_micro"]))
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


def deal_fee(base: int, bp: int, cap: int, total: int) -> int:
    """비율 방식이면 예치 합계 × bp/10000 (최소 base, 최대 cap). 고정이면 base."""
    if bp <= 0:
        return base
    fee = max(base, total * bp // 10_000)
    return min(fee, cap) if cap > 0 else fee


async def _start(c, ctx: Ctx, d, actor: int):
    if d["fee_bp"]:      # 비율 수수료는 예치 합계가 정해지는 지금 확정
        base = await c.fetchval("SELECT coalesce(sum(base_micro),0) FROM deposit_slots WHERE deal_id=$1 AND status='confirmed'", d["id"])
        fee = deal_fee(int(d["fee_micro"]), int(d["fee_bp"]), int(d["fee_max_micro"]), int(base))   # 끝자리(0.0001…) 빼고 계산
        await c.execute("UPDATE deals SET fee_micro=$2 WHERE id=$1", d["id"], fee)
        await event(c, d["id"], actor, "fee_fixed", {"fee": fee})
    d = await _set_state(c, d, "FUNDED", actor)
    bal = await balances(c, d["id"])
    rows = [(await label(c, p), v) for p, v in bal.items()]
    await say_deal(c, d, T.funded(d["code"], rows, sum(bal.values())), kb_funded(d["id"]))


async def held(c, deal_id: int, statuses=("confirmed",)) -> int:
    return int(await c.fetchval("SELECT coalesce(sum(amount_micro),0) FROM deposit_slots WHERE deal_id=$1 AND status = ANY($2)",
                                deal_id, list(statuses)))


async def balances(c, deal_id: int, statuses=("confirmed",)) -> dict[int, int]:
    """사람별 남은 몫 = 예치금 − 그 사람 몫에서 나간 지급(취소 안 된 모든 지급의 debits). 두 당사자 모두 키로 (0 이어도)."""
    parties = [r["tg_id"] for r in await c.fetch("SELECT tg_id FROM deal_parties WHERE deal_id=$1 ORDER BY role", deal_id)]
    out = {p: 0 for p in parties}
    for r in await c.fetch("SELECT tg_id, sum(amount_micro) s FROM deposit_slots WHERE deal_id=$1 AND status = ANY($2) GROUP BY tg_id",
                           deal_id, list(statuses)):
        out[r["tg_id"]] = out.get(r["tg_id"], 0) + int(r["s"])
    for r in await c.fetch("SELECT debits FROM payouts WHERE deal_id=$1 AND status <> 'cancelled'", deal_id):
        for k, v in (r["debits"] or {}).items():
            out[int(k)] = out.get(int(k), 0) - int(v)
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
    a = await c.fetchrow("SELECT * FROM approvals WHERE id=$1", approval_id)
    if not a:
        raise Refused("이미 처리된 건이에요.")
    d = await _lock(c, a["deal_id"]) if a["deal_id"] else None      # 잠금 순서: 거래 → 승인 줄
    a = await c.fetchrow("SELECT * FROM approvals WHERE id=$1 FOR UPDATE", approval_id)
    if a["status"] not in ("pending", "held"):
        raise Refused("이미 처리된 건이에요.")
    if not approve:
        await c.execute("UPDATE approvals SET status='held', note=$2, decided_by=$3, decided_at=now() WHERE id=$1",
                        approval_id, note, admin)
        await event(c, a["deal_id"], admin, "approval_hold", {"id": approval_id, "kind": a["kind"]})
        return "보류했어요. 관리 페이지에서 다시 승인할 수 있어요."
    if d and d["state"] == "DISPUTE":
        raise Refused("분쟁 중인 거래예요. 분쟁을 먼저 처리해주세요.")
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
    po = await c.fetchrow("INSERT INTO payouts(deal_id, split_id, tg_id, payer, kind, gross_micro, fee_micro, amount_micro, status, debits) "
                          "VALUES($1,$2,$3,$4,'partial',$5,0,$5,'need_address',$6) RETURNING *",
                          deal_id, sp["id"], to, payer, amount, {str(payer): amount})
    await event(c, deal_id, payer, "partial", {"to": to, "amount": amount, "payout": po["id"]})
    await say_deal(c, d, T.partial_made(d["code"], await label(c, payer), await label(c, to), amount))
    await _ask_address(c, d, po)
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
    """분배안 → 받는 사람마다 지급 줄(need_address). 지급 합계가 예치금을 넘으면 거절 (돈 규칙)."""
    statuses = ("received", "confirmed") if kind == "refund" else ("confirmed",)
    avail = {p: max(0, v) for p, v in (await balances(c, d["id"], statuses)).items()}
    debits: dict[int, dict[str, int]] = {}
    for r, g in plan.items():          # 받는 사람 자기 몫부터, 모자라면 상대 몫에서 (환불 = 자기 돈 그대로)
        if g <= 0:
            continue
        need, dd = g, {}
        for p in [r] + sorted(k for k in avail if k != r):
            take = min(need, avail.get(p, 0))
            if take:
                dd[str(p)] = dd.get(str(p), 0) + take
                avail[p] -= take
                need -= take
        if need:
            raise Refused("예치금보다 많이 나갈 수 없어요.")
        debits[r] = dd
    fees = split_fee(plan, fee)
    made = 0
    for tg_id, gross in plan.items():
        if gross <= 0:
            continue
        amount = gross - fees[tg_id]
        po = await c.fetchrow(
            "INSERT INTO payouts(deal_id, split_id, tg_id, kind, gross_micro, fee_micro, amount_micro, status, debits) "
            "VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING *",
            d["id"], split_id, tg_id, kind, gross, fees[tg_id], amount, "need_address" if amount > 0 else "confirmed",
            debits[tg_id])
        if amount <= 0:
            continue                    # 받을 몫이 전부 수수료 → 보낼 것 없음
        made += 1
        await _ask_address(c, d, po)
    await event(c, d["id"], 0, "payouts", {"split": split_id, "kind": kind, "count": made})
    if not made:   # 받을 사람이 없음 (전부 0) — 바로 끝
        await _maybe_closed(c, ctx, d, 0)


def kb_address(payout_id):
    return [[["받을 주소 입력", f"d:addr:{payout_id}"]]]


async def _ask_address(c, d, po):
    """받을 주소 묻기 + 입력 대기. 입력 대기가 지워져도(다른 질문·/start) 메시지의 [받을 주소 입력] 으로 다시 열 수 있음."""
    await say(c, po["tg_id"], T.ask_address(d["code"], po["amount_micro"], 1), kb_address(po["id"]), d["id"])
    await set_input(c, po["tg_id"], "address", d["id"], po["id"], 7 * 24 * 60)


async def reopen_address(c, payout_id: int, tg_id: int):
    """[받을 주소 입력] 버튼 → 입력 대기 다시 (처음부터)."""
    po = await c.fetchrow("SELECT p.*, d.code FROM payouts p JOIN deals d ON d.id=p.deal_id WHERE p.id=$1", payout_id)
    if not po or po["tg_id"] != tg_id or po["status"] != "need_address":
        raise Refused(T.STALE)
    await set_input(c, tg_id, "address", po["deal_id"], payout_id, 7 * 24 * 60)
    return po


async def pending_addresses(c, tg_id: int):
    return await c.fetch("SELECT p.*, d.code FROM payouts p JOIN deals d ON d.id=p.deal_id "
                         "WHERE p.tg_id=$1 AND p.status='need_address' ORDER BY p.id", tg_id)


async def set_address(c, ctx: Ctx, payout_id: int, tg_id: int, address: str):
    """두 번 입력·검사가 끝난 주소 저장 → 그 분배안의 주소가 다 모이면 관리자 확인(또는 자동 승인)."""
    deal_id = await c.fetchval("SELECT deal_id FROM payouts WHERE id=$1", payout_id)
    if deal_id is None:
        raise Refused(T.STALE)
    d = await _lock(c, deal_id)
    po = await c.fetchrow("SELECT * FROM payouts WHERE id=$1 FOR UPDATE", payout_id)
    if po["tg_id"] != tg_id or po["status"] != "need_address":
        raise Refused(T.STALE)
    if d["state"] == "DISPUTE":
        raise Refused(T.LOCKED)
    await c.execute("UPDATE payouts SET address=$2, status='review', updated_at=now() WHERE id=$1", payout_id, address)
    await c.execute("INSERT INTO addresses(tg_id, address) VALUES($1,$2) ON CONFLICT (tg_id, address) DO UPDATE SET last_used=now()",
                    tg_id, address)
    await c.execute("DELETE FROM inputs WHERE tg_id=$1 AND kind='address'", tg_id)
    await event(c, d["id"], tg_id, "address", {"payout": payout_id, "address": address})
    manual = await _split_manual(c, po["split_id"])
    await say(c, tg_id, T.address_saved(po["amount_micro"], address, manual), deal_id=d["id"])
    await _after_addresses(c, ctx, d, po["split_id"])


async def _split_manual(c, split_id: int) -> bool:
    st = await settings(c)
    sp = await c.fetchrow("SELECT * FROM splits WHERE id=$1", split_id)
    kind = await c.fetchval("SELECT kind FROM payouts WHERE split_id=$1 LIMIT 1", split_id)
    total = int(await c.fetchval("SELECT coalesce(sum(amount_micro),0) FROM payouts WHERE split_id=$1 AND status <> 'cancelled'", split_id))
    return not (sp["kind"] == "dispute" or is_auto(st, kind, total))


async def _after_addresses(c, ctx: Ctx, d, split_id: int):
    """그 분배안의 주소가 다 모였으면 → 자동 승인 또는 관리자 확인 요청."""
    if await c.fetchval("SELECT count(*) FROM payouts WHERE split_id=$1 AND status='need_address'", split_id):
        return
    if not await c.fetchval("SELECT count(*) FROM payouts WHERE split_id=$1 AND status='review'", split_id):
        return
    if not await _split_manual(c, split_id):
        await _approve_split(c, ctx, d, split_id, 0)
        return
    kind = await c.fetchval("SELECT kind FROM payouts WHERE split_id=$1 LIMIT 1", split_id)
    lines = [f"{await label(c, r['tg_id'])}: {fmt(r['amount_micro'])} → <code>{r['address']}</code>"
             for r in await c.fetch("SELECT * FROM payouts WHERE split_id=$1 AND status='review' ORDER BY id", split_id)]
    await _ask_approval(c, ctx, d, kind, split_id, "\n".join(lines))


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
    # 관리자가 해시로 먼저 완료 처리한 지급 (묶음 송금이면 한 해시에 여러 건 → 주소·금액까지 맞춰 봄)
    already = await c.fetchrow("SELECT * FROM payouts WHERE tx_id=$1 AND (tx_idx=$2 OR (tx_idx IS NULL AND address=$3 "
                               "AND amount_micro=$4)) ORDER BY tx_idx NULLS LAST, id LIMIT 1", tx_id, idx, to, amount)
    if already and already["tx_idx"] is None:
        await c.execute("UPDATE payouts SET tx_idx=$2 WHERE id=$1", already["id"], idx)
    po = None
    if not already:
        cand = await c.fetchrow("SELECT * FROM payouts WHERE status='approved' AND address=$1 AND amount_micro=$2 AND tx_id IS NULL "
                                "ORDER BY id LIMIT 1", to, amount)
        if cand:
            await _lock(c, cand["deal_id"])                        # 잠금 순서: 거래 → 지급 줄
            po = await c.fetchrow("SELECT * FROM payouts WHERE id=$1 AND status='approved' AND tx_id IS NULL FOR UPDATE", cand["id"])
    status = "matched" if (po or already) else ("ignored" if amount < DUST_MICRO else "unmatched")
    await c.execute("INSERT INTO chain_tx(tx_id, idx, direction, from_addr, to_addr, amount_micro, block_ts, status, payout_id) "
                    "VALUES($1,$2,'out',$3,$4,$5,$6,$7,$8)", tx_id, idx, frm, to, amount, block_ts, status,
                    (po or already)["id"] if (po or already) else None)
    if already or status == "ignored":
        return status
    if not po:
        await say_admins(c, ctx, f"⚠️ 지급 목록에 없는 출금 {fmt(amount)} USDT → <code>{to}</code>\ntx <code>{tx_id}</code>")
        return status
    await _payout_sent(c, ctx, po, tx_id, 0, idx)
    return status


async def _payout_sent(c, ctx: Ctx, po, tx_id: str, actor: int, tx_idx: int | None = None):
    await c.execute("UPDATE payouts SET status='sent', tx_id=$2, tx_idx=$3, updated_at=now() WHERE id=$1", po["id"], tx_id, tx_idx)
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
    deal_id = await c.fetchval("SELECT deal_id FROM payouts WHERE id=$1", payout_id)
    if deal_id is None:
        raise Refused("승인된 지급만 완료 처리할 수 있어요.")
    await _lock(c, deal_id)
    po = await c.fetchrow("SELECT * FROM payouts WHERE id=$1 FOR UPDATE", payout_id)
    if po["status"] != "approved":
        raise Refused("승인된 지급만 완료 처리할 수 있어요.")
    tx_id = tx_id.strip().lower()
    if len(tx_id) != 64 or any(ch not in "0123456789abcdef" for ch in tx_id):
        raise Refused("tx 해시는 64자리 16진수예요.")
    await _payout_sent(c, ctx, po, tx_id, admin)


async def cancel_payout(c, ctx: Ctx, payout_id: int, admin: int, reason: str):
    """관리자: 아직 안 보낸 지급(주소 대기·확인 대기·승인됨)을 취소하고 같은 몫으로 주소를 다시 받음 (주소가 틀렸을 때).
    이미 보낸(sent·confirmed) 건 못 돌림."""
    if admin not in ctx.admins:
        raise Refused("관리자만")
    deal_id = await c.fetchval("SELECT deal_id FROM payouts WHERE id=$1", payout_id)
    if deal_id is None:
        raise Refused("없는 지급이에요.")
    d = await _lock(c, deal_id)
    po = await c.fetchrow("SELECT * FROM payouts WHERE id=$1 FOR UPDATE", payout_id)
    if po["status"] not in ("need_address", "review", "approved") or po["amount_micro"] <= 0:
        raise Refused("이미 보냈거나 끝난 지급이라 취소할 수 없어요.")
    await c.execute("UPDATE payouts SET status='cancelled', updated_at=now() WHERE id=$1", payout_id)
    await c.execute("UPDATE approvals SET status='void' WHERE kind=$1 AND ref_id=$2 AND status IN ('pending','held')",
                    po["kind"], po["split_id"])
    new = await c.fetchrow(
        "INSERT INTO payouts(deal_id, split_id, tg_id, payer, kind, gross_micro, fee_micro, amount_micro, status, debits) "
        "VALUES($1,$2,$3,$4,$5,$6,$7,$8,'need_address',$9) RETURNING *",
        po["deal_id"], po["split_id"], po["tg_id"], po["payer"], po["kind"], po["gross_micro"], po["fee_micro"], po["amount_micro"],
        po["debits"])
    await event(c, deal_id, admin, "payout_cancel", {"payout": payout_id, "new": new["id"], "reason": reason[:100]})
    await say(c, po["tg_id"], T.address_again(d["code"], reason), deal_id=deal_id)
    await _ask_address(c, d, new)
    return new


async def _payout_confirm(c, ctx: Ctx, payout_id: int, actor: int):
    deal_id = await c.fetchval("SELECT deal_id FROM payouts WHERE id=$1", payout_id)
    d = await _lock(c, deal_id)
    po = await c.fetchrow("UPDATE payouts SET status='confirmed', updated_at=now() WHERE id=$1 AND status='sent' RETURNING *", payout_id)
    if not po:
        return
    await event(c, d["id"], actor, "payout_ok", {"payout": payout_id})
    await say(c, po["tg_id"], T.payout_sent(d["code"], po["amount_micro"], po["tx_id"]), deal_id=d["id"])
    await _maybe_closed(c, ctx, d, actor)


async def _maybe_closed(c, ctx: Ctx, d, actor: int):
    """보낼 게 다 나갔으면 끝. 부분 지급이면 거래 계속."""
    open_ = await c.fetchval("SELECT count(*) FROM payouts WHERE deal_id=$1 AND status IN ('need_address','review','approved','sent')",
                             d["id"])
    if open_:
        return
    if d["state"] not in ("PAYOUT", "ADDRESS", "REFUND"):
        return
    if await held(c, d["id"], MONEY_HELD) > await c.fetchval(
            "SELECT coalesce(sum(gross_micro),0) FROM payouts WHERE deal_id=$1 AND status <> 'cancelled'", d["id"]):
        await _open_refund(c, ctx, d, actor, reopen=True, reason=d["close_reason"] or "late_deposit")   # 정산 뒤 늦게 들어온 돈
        return
    settled = await c.fetchval("SELECT count(*) FROM payouts WHERE deal_id=$1 AND kind IN ('final','partial') AND status='confirmed'",
                               d["id"])
    if d["state"] in ("PAYOUT", "ADDRESS") and settled:
        d = await _set_state(c, d, "DONE", actor, close_reason="done")
        await say_deal(c, d, T.deal_done(d["code"]))
        await _receipts(c, d)
        await _close_room(c, d)
    elif d["state"] in ("PAYOUT", "ADDRESS"):
        d = await _set_state(c, d, "CANCELLED", actor, close_reason="no_money")
        await say_deal(c, d, T.cancel_done(d["code"]))
        await _close_room(c, d)
    elif settled:      # 정산 끝난 거래에 늦게 온 돈까지 돌려줌 → 거래 자체는 '끝'
        d = await _set_state(c, d, "DONE", actor, close_reason="done")
        await _close_room(c, d)
    else:
        d = await _set_state(c, d, "CANCELLED", actor, close_reason=d["close_reason"] or "refunded")
        await say_deal(c, d, T.cancel_done(d["code"], "예치금은 다 돌려드렸어요."))
        await _close_room(c, d)


async def _receipts(c, d):
    """거래가 끝나면 두 사람 1:1 에 영수증 한 장 (예치금·받은 돈·수수료·송금 tx)."""
    for p in (d["creator"], d["partner"]):
        if not p:
            continue
        dep = await c.fetchval("SELECT coalesce(sum(amount_micro),0) FROM deposit_slots WHERE deal_id=$1 AND tg_id=$2 AND status='confirmed'",
                               d["id"], p)
        rows = await c.fetch("SELECT amount_micro, fee_micro, tx_id FROM payouts WHERE deal_id=$1 AND tg_id=$2 AND status='confirmed'",
                             d["id"], p)
        await say(c, p, T.receipt(d["code"], int(dep), sum(r["amount_micro"] for r in rows), sum(r["fee_micro"] for r in rows),
                                  [r["tx_id"] for r in rows if r["tx_id"]]), deal_id=d["id"])


async def check_deal(c, code: str, tg_id: int) -> str:
    """/check 거래코드 — 이 봇의 진짜 거래인지 (가짜 봇·사칭 대응). 누구끼리인지는 당사자에게만."""
    d = await c.fetchrow("SELECT * FROM deals WHERE code=$1", code.strip().upper().lstrip("#"))
    if not d:
        return T.CHECK_NONE
    parties = None
    if tg_id in (d["creator"], d["partner"]):
        parties = f"{await label(c, d['creator'])} · {await label(c, d['partner'])}"
    return T.check_result(d["code"], T.STATE_LABEL.get(d["state"], d["state"]), parties)


async def _close_room(c, d):
    if d["room_id"]:
        await task(c, "room_close", {"deal_id": d["id"]}, d["room_id"], d["id"])
        await c.execute("UPDATE deals SET room_id=NULL WHERE id=$1", d["id"])   # 방은 다음 거래가 씀 → 이 거래 글은 이제 1:1 로
    # 기다리는 입금 자리는 안 지움: 이미 보낸 돈이 늦게 확정돼도 이 거래 환불로 잡히게 (자리는 시간 지나면 만료)
    await c.execute("DELETE FROM inputs WHERE deal_id=$1 AND kind <> 'address'", d["id"])


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
    await _void_approvals(c, d["id"], ("deposit", "start", "partial", "final"))
    await c.execute("UPDATE deposit_slots SET status='confirmed', updated_at=now() WHERE deal_id=$1 AND status='received'", d["id"])
    # 기다리는 입금 자리는 그대로 — 안내 시간 안에 들어오면 _slot_received 가 환불을 다시 만듦
    # 아직 안 나간 지급은 취소 (주소 대기·확인 대기) → 그 몫도 돌려줌
    await c.execute("UPDATE payouts SET status='cancelled', updated_at=now() WHERE deal_id=$1 AND status IN ('need_address','review')",
                    d["id"])
    if d["state"] != "REFUND":
        d = await _set_state(c, d, "REFUND", actor, close_reason=reason, closed_at=None)
    bal = await balances(c, d["id"])
    plan = {p: v for p, v in bal.items() if v > 0}
    if not plan:
        await _maybe_closed(c, ctx, d, actor)
        return d
    sp = await c.fetchrow("INSERT INTO splits(deal_id, kind, plan, proposer, status) VALUES($1,'refund',$2,NULL,'agreed') RETURNING id",
                          d["id"], {str(k): v for k, v in plan.items()})
    st = await settings(c)
    fee = int(st["refund_fee_micro"])
    if await c.fetchval("SELECT count(*) FROM payouts WHERE deal_id=$1 AND kind IN ('partial','final') AND status <> 'cancelled' "
                        "AND gross_micro > 0", d["id"]):
        fee = max(fee, int(d["fee_micro"]))      # 일부라도 정산이 나간 거래 = 거래가 된 것 → 거래 수수료 (부분 지급 뒤 취소로 수수료 피하기 막음)
        fee -= int(await c.fetchval("SELECT coalesce(sum(fee_micro),0) FROM payouts WHERE deal_id=$1 AND status <> 'cancelled'", d["id"]))
        fee = max(0, fee)
    await say_deal(c, d, T.refund_wait(d["code"]))
    await _make_payouts(c, ctx, d, sp["id"], plan, "refund", fee)
    return d


async def open_dispute(c, ctx: Ctx, deal_id: int, tg_id: int):
    d = await _lock(c, deal_id)
    _party(d, tg_id)
    if d["state"] in CLOSED or d["state"] == "DISPUTE":
        raise Refused(T.LOCKED if d["state"] == "DISPUTE" else T.STALE)
    if d["state"] in ("PAYOUT",):
        raise Refused("송금 단계예요. 문의는 고객센터로 해주세요.")
    d = await _set_state(c, d, "DISPUTE", tg_id, prev_state=d["state"])
    await _void_approvals(c, deal_id)          # 분쟁 중엔 아무것도 승인 안 됨 (풀 때 _resume 이 다시 만듦)
    await say_deal(c, d, T.dispute_opened(d["code"], await label(c, tg_id)))
    await say_admins(c, ctx, f"🆘 <b>관리자 호출</b> · <code>{d['code']}</code>\n{await label(c, tg_id)} 님 · 이전 단계 {d['prev_state']}\n"
                             f"예치금 {fmt(await held(c, deal_id, MONEY_HELD))} USDT", deal_id=deal_id)
    return d


async def resolve_dispute(c, ctx: Ctx, deal_id: int, admin: int, plan: dict[int, int] | None, fee: int | None = None):
    """관리자 결정. plan=None 이면 분쟁 풀고 이전 단계로. plan 이면 그대로 지급 (주소 받은 뒤 바로 승인)."""
    if admin not in ctx.admins:
        raise Refused("관리자만")
    d = await _lock(c, deal_id)
    if d["state"] != "DISPUTE":
        raise Refused("분쟁 중인 거래가 아니에요.")
    if plan is None:
        st = await settings(c)
        back = d["prev_state"] or "FUNDED"
        d = await _set_state(c, d, back, admin, int(st["stage_minutes"]) if back in EXPIRING else None)
        await say_deal(c, d, f"관리자가 확인을 마쳤어요. 거래를 이어서 진행하세요. (<code>{d['code']}</code>)",
                       kb_funded(deal_id) if d["state"] == "FUNDED" else None)
        await _resume(c, ctx, d, admin)
        return d
    await c.execute("UPDATE deposit_slots SET status='confirmed', updated_at=now() WHERE deal_id=$1 AND status='received'", deal_id)
    await c.execute("UPDATE deposit_slots SET status='cancelled', updated_at=now() WHERE deal_id=$1 AND status='waiting'", deal_id)
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


async def _resume(c, ctx: Ctx, d, actor: int):
    """분쟁이 풀린 뒤: 분쟁 중 멈췄던 일(입금 확인·시작 승인·지급 승인·송금 확인·끝내기)을 다시 건다."""
    d = await c.fetchrow("SELECT * FROM deals WHERE id=$1", d["id"])
    st = await settings(c)
    if d["state"] == "DEPOSIT":
        for s in await c.fetch("SELECT * FROM deposit_slots WHERE deal_id=$1 AND status='received'", d["id"]):
            if is_auto(st, "deposit", s["amount_micro"]):
                await _slot_confirm(c, ctx, d, s["id"], actor)
            else:
                await _ask_approval(c, ctx, d, "deposit", s["id"], f"{await label(c, s['tg_id'])} 입금 {fmt(s['amount_micro'])} USDT (분쟁 뒤)")
        await _maybe_funded(c, ctx, await c.fetchrow("SELECT * FROM deals WHERE id=$1", d["id"]), actor)
    elif d["state"] == "START_REVIEW":
        await _ask_approval(c, ctx, d, "start", d["id"], f"합계 {fmt(await held(c, d['id']))} USDT (분쟁 뒤)")
    for r in await c.fetch("SELECT DISTINCT split_id FROM payouts WHERE deal_id=$1 AND status='review'", d["id"]):
        await _after_addresses(c, ctx, d, r["split_id"])
    for po in await c.fetch("SELECT * FROM payouts WHERE deal_id=$1 AND status='sent'", d["id"]):
        if is_auto(st, "payout_done", po["amount_micro"]):
            await _payout_confirm(c, ctx, po["id"], actor)
        else:
            await _ask_approval(c, ctx, d, "payout_done", po["id"],
                                f"{fmt(po['amount_micro'])} USDT → <code>{po['address']}</code>\ntx <code>{po['tx_id']}</code>")
    await _maybe_closed(c, ctx, await c.fetchrow("SELECT * FROM deals WHERE id=$1", d["id"]), actor)


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
    deal_id = await c.fetchval("SELECT deal_id FROM deposit_slots WHERE id=$1", slot_id)
    if deal_id is None:
        raise Refused("그 입금 자리는 이미 처리됐어요.")
    d = await _lock(c, deal_id)       # 잠금 순서: 거래 → 자리·입금 (감시와 같은 순서 — 동시에 같은 자리를 잡지 못하게)
    tx = await c.fetchrow("SELECT * FROM chain_tx WHERE tx_id=$1 AND idx=$2 FOR UPDATE", tx_id, idx)
    if not tx or tx["direction"] != "in" or tx["status"] not in ("unmatched", "flagged"):
        raise Refused("연결할 수 있는 입금이 아니에요.")
    slot = await c.fetchrow("SELECT * FROM deposit_slots WHERE id=$1 FOR UPDATE", slot_id)
    if slot["status"] not in ("waiting", "expired", "cancelled"):
        raise Refused("그 입금 자리는 이미 처리됐어요.")
    if d["state"] == "DISPUTE":
        raise Refused("분쟁 중인 거래예요. 분쟁을 먼저 처리해주세요.")
    if await c.fetchval("SELECT count(*) FROM deposit_slots WHERE deal_id=$1 AND tg_id=$2 AND id<>$3 "
                        "AND status IN ('waiting','received','confirmed')", d["id"], slot["tg_id"], slot_id):
        raise Refused("그 사람은 이 거래에 이미 다른 입금 자리가 있어요. 그 자리에 연결하거나 먼저 정리해주세요.")
    await c.execute("UPDATE deposit_slots SET amount_micro=$2, base_micro=LEAST(base_micro,$2), status='received', tx_id=$3, "
                    "updated_at=now() WHERE id=$1", slot_id, tx["amount_micro"], tx_id)
    await c.execute("UPDATE chain_tx SET status='matched', slot_id=$3, note=note || ' · 관리자 연결' WHERE tx_id=$1 AND idx=$2",
                    tx_id, idx, slot_id)
    await event(c, d["id"], admin, "deposit_assign", {"tx": tx_id, "slot": slot_id, "amount": tx["amount_micro"]})
    if d["state"] in CLOSED or d["state"] == "REFUND":
        await _open_refund(c, ctx, d, admin, reopen=True)
    else:
        await _slot_confirm(c, ctx, d, slot_id, admin)     # 관리자가 직접 연결 = 확인한 것


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
    await c.execute("UPDATE deposit_slots SET status='expired', updated_at=now() WHERE id IN ("
                    "SELECT id FROM deposit_slots WHERE status='waiting' AND expires_at + $1 < now() FOR UPDATE SKIP LOCKED)", grace)
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
    """방을 못 쓰게 됨(봇이 쫓겨남·권한 빠짐 등) → 고장 표시 + 그 방에서 진행 중이던 거래는 두 사람 1:1 로 이어감."""
    for r in await c.fetch("SELECT id FROM deals WHERE room_id=$1 AND state <> ALL($2)", chat_id, list(CLOSED)):
        await _to_dm(c, ctx, r["id"])
    await c.execute("UPDATE rooms SET state='broken', note=$2, updated_at=now() WHERE chat_id=$1", chat_id, note[:200])
    await say_admins(c, ctx, f"⚠️ 거래방 문제: {chat_id}\n{T.esc(note)}")


async def _to_dm(c, ctx: Ctx, deal_id: int):
    d = await _lock(c, deal_id)
    if d["state"] in CLOSED or not d["room_id"]:
        return
    d = await c.fetchrow("UPDATE deals SET room_id=NULL, updated_at=now() WHERE id=$1 RETURNING *", deal_id)
    await event(c, deal_id, 0, "room_lost", {})
    for p in (d["creator"], d["partner"]):
        if p:
            await say(c, p, T.room_lost(d["code"]), deal_id=deal_id)
    if d["state"] == "ROOM":
        await _enter_notice(c, ctx, d, 0)
        return
    sc = await screen(c, ctx, d)
    if sc:
        await say_deal(c, d, *sc)


async def screen(c, ctx: Ctx, d):
    """지금 단계의 화면(글, 버튼) — 방을 잃었거나 '지금 단계 다시 보기'. 돌려줄 게 없으면 None."""
    st = await settings(c)
    code, s = d["code"], d["state"]
    if s == "NOTICE":
        return (T.notice(code, await label(c, d["creator"]), await label(c, d["partner"]), d["fee_micro"], int(st["min_micro"]),
                         ctx.cfg.support, ctx.cfg.bot_username, d["fee_bp"], d["fee_max_micro"]), kb_notice(d["id"]))
    if s == "CHOOSE":
        rows = await c.fetch("SELECT tg_id, choice FROM deal_parties WHERE deal_id=$1 ORDER BY role", d["id"])
        return T.choose(code, [(await label(c, r["tg_id"]), r["choice"]) for r in rows]), kb_choose(d["id"])
    if s == "FUNDED":
        bal = await balances(c, d["id"])
        return T.funded(code, [(await label(c, p), v) for p, v in bal.items()], sum(bal.values())), kb_funded(d["id"])
    if s == "SPLIT":
        bal = await balances(c, d["id"])
        return (T.split_ask(code, [(await label(c, p), v) for p, v in bal.items()], sum(bal.values()), d["fee_micro"]),
                kb_split(d["id"], await label(c, d["creator"]), await label(c, d["partner"])))
    return None


async def room_migrated(c, old: int, new: int):
    """일반 그룹이 슈퍼그룹으로 바뀌면 chat_id 가 바뀜 → 방·거래를 새 id 로."""
    if not await c.fetchval("SELECT 1 FROM rooms WHERE chat_id=$1", old) or await c.fetchval("SELECT 1 FROM rooms WHERE chat_id=$1", new):
        return
    await c.execute("INSERT INTO rooms(chat_id, title, state, note, deal_id, invite_link) SELECT $2, title, state, note, deal_id, invite_link "
                    "FROM rooms WHERE chat_id=$1", old, new)
    await c.execute("UPDATE deals SET room_id=$2 WHERE room_id=$1", old, new)
    await c.execute("DELETE FROM rooms WHERE chat_id=$1", old)
    await event(c, None, 0, "room_migrate", {"old": old, "new": new})


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
        "ledger_micro": int(held_) - int(out_),        # 장부상 지갑에 있어야 할 돈 (예치금 − 실제로 보낸 돈, 번 수수료 포함)
        "fees_micro": int(fees),
        "pending_approvals": await c.fetchval("SELECT count(*) FROM approvals WHERE status='pending'"),
        "held_approvals": await c.fetchval("SELECT count(*) FROM approvals WHERE status='held'"),
        "payouts_to_send": await c.fetchval("SELECT count(*) FROM payouts WHERE status='approved'"),
        "unmatched_in": await c.fetchval("SELECT count(*) FROM chain_tx WHERE status IN ('unmatched','flagged')"),
        "disputes": await c.fetchval("SELECT count(*) FROM deals WHERE state='DISPUTE'"),
        "rooms_free": await c.fetchval("SELECT count(*) FROM rooms WHERE state='free'"),
        "wallet_micro": pay_balance,
    }
