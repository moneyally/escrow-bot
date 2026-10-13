"""수동 중개 (관리자가 두 사람 대신 단계를 누름) — 관리 페이지 전용, 설정 'proxy_mode' 가 켜져 있을 때만.

원칙: 기존 거래 흐름(core)은 안 바꾼다. 여기선 손님이 봇에서 누를 때 부르는 core 함수를 그 사람 이름으로 그대로 부르고,
누가 대신 눌렀는지는 events 'admin_proxy' 로 따로 남긴다. 텔레그램 쪽엔 새 버튼이 없다 (진행 메시지는 평소처럼 감).
예외 하나: 수동 입금 — 체인 확인 없이 관리자가 '들어왔다'고 인정 (tx_id 'manual:…' 입금 기록 → 기존 assign_deposit).
"""
from __future__ import annotations

import secrets

from . import core
from . import texts as T
from .tron import is_address

STEPS = ("accept", "skip_room", "notice", "choose", "amount", "deposit", "complete", "split", "address")
MANUAL_PREFIX = "manual:"


async def enabled(c) -> bool:
    return bool((await core.settings(c))["proxy_mode"])


async def _log(c, deal_id: int, admin: int, action: str, as_tg: int | None, reason: str, **extra):
    await core.event(c, deal_id, admin, "admin_proxy", {"action": action, "as": as_tg, "reason": reason[:200], **extra})


async def new_deal(c, ctx: core.Ctx, admin: int, a: int, b: int, reason: str = ""):
    """두 사람(이미 봇을 쓴 적 있는 사람)으로 거래를 만들고 B 수락까지 대신."""
    if a == b:
        raise core.Refused("두 사람이 같아요.")
    for t in (a, b):
        if not await core.user(c, t):
            raise core.Refused(f"{t}: 이 봇을 한 번도 쓴 적 없는 사람이에요. 먼저 봇에 /start 를 보내야 해요.")
    d = await core.create_deal(c, ctx, a, b, None)
    await _log(c, d["id"], admin, "create", None, reason, a=a, b=b)
    d = await core.answer_invite(c, ctx, d["id"], b, True)
    await _log(c, d["id"], admin, "accept", b, reason)
    return d


async def act(c, ctx: core.Ctx, admin: int, deal_id: int, action: str, as_tg: int | None, value=None, reason: str = ""):
    """한 단계 대신 처리. value: choose→bool, amount→마이크로, deposit→마이크로(없으면 안내 금액), split→{"how","a"}, address→주소."""
    if action not in STEPS:
        raise core.Refused("모르는 단계")
    d = await c.fetchrow("SELECT * FROM deals WHERE id=$1", deal_id)
    if not d:
        raise core.Refused(T.NOTHING)
    if action not in ("split", "skip_room") and as_tg not in (d["creator"], d["partner"]):
        raise core.Refused("거래 당사자를 골라주세요.")
    out = None
    if action == "accept":
        out = await core.answer_invite(c, ctx, deal_id, as_tg, True)
    elif action == "skip_room":
        out = await _skip_room(c, ctx, deal_id)
    elif action == "notice":
        out = await core.ack_notice(c, ctx, deal_id, as_tg)
    elif action == "choose":
        out = await core.choose(c, ctx, deal_id, as_tg, bool(value))
    elif action == "amount":
        out = await core.open_slot(c, ctx, deal_id, as_tg, int(value))
        await c.execute("DELETE FROM inputs WHERE tg_id=$1 AND kind='amount' AND deal_id=$2", as_tg, deal_id)
    elif action == "deposit":
        out = await manual_deposit(c, ctx, admin, deal_id, as_tg, int(value) if value else None, reason)
    elif action == "complete":
        out, _ = await core.press_complete(c, ctx, deal_id, as_tg, True)
    elif action == "split":
        how = (value or {}).get("how")
        sp = await core.propose_split(c, ctx, deal_id, d["creator"], how, (value or {}).get("a"))
        out = await core.answer_split(c, ctx, deal_id, sp["id"], d["partner"], True)
    elif action == "address":
        out = await _address(c, ctx, deal_id, as_tg, str(value or "").strip())
    await _log(c, deal_id, admin, action, as_tg, reason, value=value if action != "address" else str(value)[:40])
    return out


async def _skip_room(c, ctx, deal_id: int):
    """거래방 없이 1:1 로 (입장을 안 해도 진행). 쓰던 방은 정리해서 다음 거래에."""
    d = await core._lock(c, deal_id)
    if d["state"] != "ROOM":
        raise core.Refused("거래방 입장 단계가 아니에요.")
    old = d["room_id"]
    if old:
        await core._to_dm(c, ctx, deal_id)
        await core.task(c, "room_close", {"deal_id": deal_id}, old, deal_id)
        return await c.fetchrow("SELECT * FROM deals WHERE id=$1", deal_id)
    return await core._enter_notice(c, ctx, d, 0)    # 도우미가 방을 만드는 중이었으면, 다 만든 방은 빈 방으로 남음


async def manual_deposit(c, ctx, admin: int, deal_id: int, tg_id: int, amount: int | None, reason: str):
    """체인 확인 없이 '입금됨' 인정. 기록은 tx_id 'manual:…' 입금 → 기존 assign_deposit 로 그 사람 입금 자리에 연결."""
    await core._lock(c, deal_id)
    slot = await c.fetchrow("SELECT * FROM deposit_slots WHERE deal_id=$1 AND tg_id=$2 AND status IN ('waiting','expired') "
                            "ORDER BY id DESC LIMIT 1", deal_id, tg_id)
    if not slot:
        raise core.Refused("그 사람의 입금 자리가 없어요. 먼저 예치 금액을 넣어주세요.")
    amount = amount or slot["amount_micro"]
    if amount <= 0:
        raise core.Refused("금액이 이상해요.")
    tx = MANUAL_PREFIX + secrets.token_hex(12)
    await c.execute("INSERT INTO chain_tx(tx_id, idx, direction, from_addr, to_addr, amount_micro, block_ts, status, note) "
                    "VALUES($1,0,'in','MANUAL',$2,$3,now(),'unmatched',$4)", tx, ctx.cfg.pay_address, amount,
                    f"수동 입금(체인 확인 없음) · 관리자 {admin} · {reason[:100]}")
    await core.assign_deposit(c, ctx, tx, 0, slot["id"], admin)
    return {"tx": tx, "amount": amount}


async def _address(c, ctx, deal_id: int, tg_id: int, addr: str):
    if not is_address(addr):
        raise core.Refused(T.ADDRESS_BAD)
    if addr == ctx.cfg.pay_address:
        raise core.Refused("그건 우리 입금 주소예요.")
    po = await c.fetchrow("SELECT * FROM payouts WHERE deal_id=$1 AND tg_id=$2 AND status='need_address' ORDER BY id LIMIT 1",
                          deal_id, tg_id)
    if not po:
        raise core.Refused("그 사람에게 받을 주소를 기다리는 지급이 없어요.")
    await core.set_address(c, ctx, po["id"], tg_id, addr)
    return po


async def board(c) -> list[dict]:
    """수동 중개 페이지 표: 진행 중 거래마다 두 사람의 단계별 상태."""
    out = []
    for d in await c.fetch("SELECT * FROM deals WHERE state <> ALL($1) ORDER BY id DESC LIMIT 50", list(core.CLOSED)):
        people = []
        for tg in (d["creator"], d["partner"]):
            if not tg:
                continue
            u = await core.user(c, tg)
            p = await c.fetchrow("SELECT * FROM deal_parties WHERE deal_id=$1 AND tg_id=$2", d["id"], tg)
            slot = await c.fetchrow("SELECT * FROM deposit_slots WHERE deal_id=$1 AND tg_id=$2 AND status <> 'cancelled' ORDER BY id DESC LIMIT 1",
                                    d["id"], tg)
            need_addr = await c.fetchval("SELECT count(*) FROM payouts WHERE deal_id=$1 AND tg_id=$2 AND status='need_address'", d["id"], tg)
            got = await c.fetchrow("SELECT amount_micro, address, status FROM payouts WHERE deal_id=$1 AND tg_id=$2 AND status <> 'cancelled' "
                                   "ORDER BY id DESC LIMIT 1", d["id"], tg)
            people.append({
                "tg_id": tg, "username": u["username"] if u else None, "name": u["name"] if u else "",
                "role": "creator" if tg == d["creator"] else "partner",
                "accepted": bool(p) or tg == d["creator"], "joined": bool(p and p["joined"]), "notice_ok": bool(p and p["notice_ok"]),
                "choice": p["choice"] if p else None, "complete": bool(p and p["complete"]),
                "slot": {"amount_micro": slot["amount_micro"], "status": slot["status"], "tx_id": slot["tx_id"]} if slot else None,
                "need_address": bool(need_addr),
                "payout": {"amount_micro": got["amount_micro"], "address": got["address"], "status": got["status"]} if got else None,
            })
        split = await c.fetchrow("SELECT * FROM splits WHERE deal_id=$1 AND kind='final' AND status='proposed' ORDER BY id DESC LIMIT 1", d["id"])
        bal = await core.balances(c, d["id"], core.MONEY_HELD)
        out.append({"id": d["id"], "code": d["code"], "state": d["state"], "room_id": d["room_id"], "fee_micro": d["fee_micro"],
                    "people": people, "split_proposed": bool(split), "held_micro": sum(bal.values())})
    return out
