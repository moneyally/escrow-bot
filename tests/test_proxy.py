"""수동 중개: 관리 페이지 버튼만으로 거래 처음 → 끝 (두 사람은 아무것도 안 누름). 토글이 꺼져 있으면 전부 막힘."""
import httpx

from conftest import A, ADDR, ADMIN, B
from escrow import core
from escrow.admin import app as adminapp
from escrow.config import Config
from escrow.money import MICRO

PAY = "TR7sTzwA9nVfDiSyge8Ez7GJ6jFng7tVC1"
SECRET = "p" * 40


async def _client(db):
    cfg = Config(admin_ids=frozenset({ADMIN}), pay_address=PAY, admin_secret=SECRET)
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=adminapp.create_app(cfg, db)), base_url="https://admin.test",
                          headers={"X-Requested-With": "escrow"})
    c.cookies.set(adminapp.COOKIE, adminapp.make_session(SECRET, ADMIN))
    return c


async def _approve(c, kind):
    for a in [x for x in (await c.get("/api/approvals")).json() if x["kind"] == kind]:
        assert (await c.post(f"/api/approvals/{a['id']}/approve", json={})).status_code == 200


async def test_proxy_off_blocks_everything(users):
    async with await _client(users) as c:
        assert (await c.get("/api/proxy")).json() == {"enabled": False, "deals": []}
        assert (await c.post("/api/proxy/new", json={"a": "@alice", "b": "@bobby"})).status_code == 403
        assert (await c.post("/api/proxy/1/act", json={"action": "notice", "as_tg": A})).status_code == 403


async def test_proxy_full_deal_with_manual_deposit(users):
    db = users
    async with await _client(db) as c:
        assert (await c.put("/api/settings", json={"proxy_mode": True})).status_code == 200
        r = await c.post("/api/proxy/new", json={"a": "@alice", "b": str(B)})
        assert r.status_code == 200, r.text
        [d] = (await c.get("/api/proxy")).json()["deals"]
        did = d["id"]
        assert d["state"] == "NOTICE", "방이 없으면 바로 공지 (수락까지 대신 됨)"

        async def act(action, as_tg=None, value=None):
            r = await c.post(f"/api/proxy/{did}/act", json={"action": action, "as_tg": as_tg, "value": value})
            assert r.status_code == 200, (action, r.text)

        for who in (A, B):
            await act("notice", who)
        await act("choose", A, True)
        await act("choose", B, False)
        await act("amount", A, "100")
        await act("deposit", A)                                   # 체인 확인 없이 입금 완료 처리
        await _approve(c, "start")
        assert (await c.get("/api/proxy")).json()["deals"][0]["state"] == "FUNDED"
        for who in (A, B):
            await act("complete", who)
        await act("split", None, {"how": "b"})
        await act("address", B, ADDR)
        await _approve(c, "final")
        [p] = (await c.get("/api/payouts?status=approved")).json()
        assert p["address"] == ADDR
        tx = "f" * 64
        assert (await c.post(f"/api/payouts/{p['id']}/done", json={"tx_id": tx})).status_code == 200
        assert (await db.fetchval("SELECT state FROM deals WHERE id=$1", did)) == "DONE"
        # 기록: 누가 대신 눌렀는지 / 화면엔 수동 입금 흔적 안 보임
        n = await db.fetchval("SELECT count(*) FROM events WHERE deal_id=$1 AND kind='admin_proxy'", did)
        assert n >= 9
        detail = (await c.get(f"/api/deals/{d['code']}")).text
        assert "manual:" not in detail
        assert all(not t["tx_id"].startswith("manual:") for t in (await c.get("/api/deposits")).json())
        assert await db.fetchval("SELECT count(*) FROM chain_tx WHERE tx_id LIKE 'manual:%' AND status='matched'") == 1


async def test_proxy_refuses_wrong_step_and_bad_address(users):
    db = users
    async with await _client(db) as c:
        await c.put("/api/settings", json={"proxy_mode": True})
        await c.post("/api/proxy/new", json={"a": "@alice", "b": "@bobby"})
        [d] = (await c.get("/api/proxy")).json()["deals"]
        r = await c.post(f"/api/proxy/{d['id']}/act", json={"action": "complete", "as_tg": A})
        assert r.status_code == 400                               # 아직 거래 중 아님
        r = await c.post(f"/api/proxy/{d['id']}/act", json={"action": "notice", "as_tg": 999})
        assert r.status_code == 400                               # 당사자 아님
    assert MICRO and core
