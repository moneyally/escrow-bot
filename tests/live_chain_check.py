"""실제 트론 메인넷 데이터로 감시 코드(읽기)를 검증: 최근 USDT 이체 하나를 골라 우리 코드가 똑같이 읽는지."""
import asyncio, sys, time
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
import httpx
from escrow.tron import TronGrid, USDT_CONTRACT, transfers_from_info, is_address, from_hex
import os
KEY = os.environ["TRONGRID_KEY"]   # 실행: TRONGRID_KEY=... python tests/live_chain_check.py (pytest 대상 아님)

async def main():
    async with httpx.AsyncClient(timeout=30, headers={"TRON-PRO-API-KEY": KEY}) as h:
        ev = (await h.get(f"https://api.trongrid.io/v1/contracts/{USDT_CONTRACT}/events",
                          params={"event_name": "Transfer", "only_confirmed": "true", "limit": 20})).json()["data"]
    tg = TronGrid("https://api.trongrid.io", KEY)
    checked = 0
    for e in ev:
        r = e["result"]; tx = e["transaction_id"]
        frm, to, val = from_hex("41" + r["from"][2:]), from_hex("41" + r["to"][2:]), int(r["value"])
        # 1) 받는 주소 기준 후보 조회 (감시가 쓰는 그 함수)
        cands = await tg.candidate_txids(to, e["block_timestamp"] - 60_000, limit_pages=2)
        found = any(t == tx for t, _ in cands)
        # 2) 확정 블록 조회 + Transfer 로그 해석
        info = await tg.solid_info(tx)
        ts = [t for t in transfers_from_info(info) if t.to_addr == to and t.from_addr == frm] if info else []
        ok = bool(ts) and ts[0].amount == val
        print(f"tx {tx[:12]}… {val/1e6:,.6f} USDT  {frm[:6]}…→{to[:6]}…  후보에 있음={found}  확정조회={info is not None}  금액·주소 일치={ok}")
        checked += 1
        if checked >= 3:
            break
    print("동결 조회(USDT 계약 주소):", await tg.is_blacklisted(USDT_CONTRACT))
    print("우리 받는 주소 형식:", is_address("TR7sTzwA9nVfDiSyge8Ez7GJ6jFng7tVC1"),
          "· 잔액:", await tg.usdt_balance("TR7sTzwA9nVfDiSyge8Ez7GJ6jFng7tVC1"), "· 동결:", await tg.is_blacklisted("TR7sTzwA9nVfDiSyge8Ez7GJ6jFng7tVC1"))
    await tg.close()
asyncio.run(main())
