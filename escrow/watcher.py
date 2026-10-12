"""입금·출금 감시: python -m escrow.watcher

5초마다 TronGrid 에서 우리 주소의 USDT tx 후보를 받고 → 확정 블록 조회로 성공·Transfer 로그 확인 → core.record_incoming/outgoing.
- 커서: '여기까지는 전부 확정·기록됨' 시각. 다음 조회는 커서 10분 전부터 (겹쳐서 받고 chain_tx 로 중복 거름).
- 아직 확정 안 된 tx 가 나오면 커서를 그 앞에서 멈춤 → 다음 차례에 다시.
- 한 시간마다 우리 주소 동결 여부·잔액 확인 → 동결이면 관리자 알림.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from . import config as cfgmod
from . import core
from .db import DB
from .money import fmt
from .tron import TronGrid, transfers_from_info

log = logging.getLogger("escrow.watcher")
OVERLAP_MS = 10 * 60 * 1000
CURSOR_KEY = "watch_cursor_ms"


async def get_cursor(db: DB) -> int:
    v = await db.fetchval("SELECT value FROM kv WHERE key=$1", CURSOR_KEY)
    return int(v) if v is not None else int(time.time() * 1000) - 60 * 60 * 1000   # 처음엔 1시간 전부터


async def set_cursor(db: DB, ms: int) -> None:
    await db.execute("INSERT INTO kv(key, value) VALUES($1,$2) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                     CURSOR_KEY, ms)


async def scan_once(db: DB, ctx: core.Ctx, tron: TronGrid, failed: set[str], done: set[str] | None = None) -> int:
    """한 번 훑기. 새로 기록한 Transfer 수.
    - done: 이 프로세스에서 끝까지 처리한 tx (다시 조회 안 함). 재시작하면 겹침 구간을 다시 보는데, 기록은 (tx_id, idx) 로 중복 거름.
    - tx 하나가 오류여도 감시 전체는 계속: 그 tx 는 failed 에 넣고 관리자에게 한 번 알림."""
    done = done if done is not None else set()
    pay = ctx.cfg.pay_address
    cursor = await get_cursor(db)
    cands = await tron.candidate_txids(pay, max(0, cursor - OVERLAP_MS))
    new = 0
    safe = cursor
    blocked = False
    seen: set[str] = set()
    for tx_id, block_ts in cands:
        if tx_id in seen:
            continue
        seen.add(tx_id)
        if tx_id in done or tx_id in failed:
            if not blocked:
                safe = max(safe, block_ts)
            continue
        info = await tron.solid_info(tx_id)
        if info is None:
            blocked = True            # 아직 확정 전 → 커서는 여기서 멈춤 (다음 차례에 다시)
            continue
        try:
            new += await _record_tx(db, ctx, tron, pay, info)
            done.add(tx_id)
        except Exception as e:   # noqa: BLE001
            log.exception("tx 처리 실패 %s", tx_id)
            failed.add(tx_id)
            async with db.tx() as c:
                await core.say_admins(c, ctx, f"⚠️ 감시가 이 tx 를 처리하지 못했어요. 관리 페이지에서 직접 확인해주세요.\n"
                                              f"<code>{tx_id}</code>\n{type(e).__name__}")
        if not blocked:
            safe = max(safe, block_ts)
    if safe > cursor:
        await set_cursor(db, safe)
    if len(done) > 20000:
        done.clear()
    return new


async def _record_tx(db: DB, ctx: core.Ctx, tron: TronGrid, pay: str, info: dict) -> int:
    transfers = transfers_from_info(info)
    if not transfers:
        log.info("확정됐지만 USDT 이동 없음/실패: %s", info.get("id"))
        return 0
    new = 0
    for idx, t in enumerate(transfers):
        ts = datetime.fromtimestamp(t.block_ts / 1000, timezone.utc)
        if t.to_addr == pay and t.from_addr != pay:
            bl = await tron.is_blacklisted(t.from_addr)
            async with db.tx() as c:
                r = await core.record_incoming(c, ctx, t.tx_id, idx, t.from_addr, t.to_addr, t.amount, ts, bl)
        elif t.from_addr == pay and t.to_addr != pay:
            async with db.tx() as c:
                r = await core.record_outgoing(c, ctx, t.tx_id, idx, t.from_addr, t.to_addr, t.amount, ts)
        else:
            continue
        log.info("tx %s #%d %s %s USDT → %s", t.tx_id[:12], idx, "입금" if t.to_addr == pay else "출금", fmt(t.amount), r)
        new += r != "dup"
    return new


async def health_check(db: DB, ctx: core.Ctx, tron: TronGrid) -> None:
    pay = ctx.cfg.pay_address
    frozen = await tron.is_blacklisted(pay)
    bal = await tron.usdt_balance(pay)
    if bal is not None:
        await db.execute("INSERT INTO kv(key, value) VALUES('wallet_balance', $1) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                         {"micro": bal, "at": int(time.time())})
    if frozen:
        async with db.tx() as c:
            await core.say_admins(c, ctx, "🚨 <b>받는 주소가 테더 동결 목록에 있어요.</b> 새 거래를 멈추고 확인해주세요.")
            await core.set_setting(c, "new_deals", False, 0)


async def run(cfg) -> None:
    db = await DB.connect(cfg.database_url, max_size=4)
    await db.migrate()
    ctx = core.Ctx(cfg)
    tron = TronGrid(cfg.trongrid_url, cfg.trongrid_key)
    failed: set[str] = set()
    done: set[str] = set()
    last_health = 0.0
    if not cfg.can_take_deposits:
        log.warning("PAY_ADDRESS 가 STUB 이라 감시 안 함 — .env 채우고 다시 켜주세요")
        while True:
            await asyncio.sleep(3600)
    log.info("감시 시작 %s", cfg.pay_address)
    backoff = 5
    while True:
        try:
            await scan_once(db, ctx, tron, failed, done)
            if time.time() - last_health > 3600:
                last_health = time.time()
                await health_check(db, ctx, tron)
            backoff = 5
        except TronGrid.RateLimited as e:
            backoff = min(backoff * 2, 120)
            log.warning("TronGrid 한도 %s → %d초 쉼", e, backoff)
        except Exception:   # noqa: BLE001 — 감시는 멈추지 않음
            log.exception("감시 오류")
            backoff = min(backoff * 2, 60)
        if len(failed) > 5000:
            failed.clear()
        await asyncio.sleep(backoff)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(run(cfgmod.load()))


if __name__ == "__main__":
    main()
