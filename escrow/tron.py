"""TRON: 주소 검사 + TronGrid 조회 (읽기만 — 이 프로젝트엔 서명·전송 코드가 없다).

확인 원칙 (TRON 공식 confirmation-semantics 문서): TronGrid 목록(/v1/accounts/…/transactions/trc20)은 '후보'로만 쓰고,
돈 판정은 확정 블록 조회(/walletsolidity/gettransactioninfobyid)의 성공 결과 + Transfer 로그로 한다.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
TRANSFER_TOPIC = "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"   # Transfer(address,address,uint256)
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58decode(s: str) -> bytes:
    n = 0
    for ch in s:
        i = _B58.find(ch)
        if i < 0:
            raise ValueError("base58 아님")
        n = n * 58 + i
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + raw


def _b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    pad = len(b) - len(b.lstrip(b"\x00"))
    return "1" * pad + out


def _checksum(payload: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]


def is_address(addr: str) -> bool:
    """T 로 시작하는 base58check 34자, 버전 바이트 0x41, 체크섬 맞음."""
    if not isinstance(addr, str) or len(addr) != 34 or not addr.startswith("T"):
        return False
    try:
        raw = _b58decode(addr)
    except ValueError:
        return False
    if len(raw) != 25 or raw[0] != 0x41:
        return False
    return _checksum(raw[:21]) == raw[21:]


def to_hex(addr: str) -> str:
    """T… → '41' + 20바이트 hex."""
    if not is_address(addr):
        raise ValueError(f"트론 주소 아님: {addr!r}")
    return _b58decode(addr)[:21].hex()


def from_hex(h: str) -> str:
    """'41…'(42자) 또는 20바이트(40자) hex → T…"""
    h = h.lower().removeprefix("0x")
    if len(h) == 40:
        h = "41" + h
    if len(h) != 42 or not h.startswith("41"):
        raise ValueError(f"트론 hex 주소 아님: {h!r}")
    payload = bytes.fromhex(h)
    return _b58encode(payload + _checksum(payload))


@dataclass(frozen=True)
class Transfer:
    """확정 블록에서 확인한 USDT 전송 한 건 (tx 안의 Transfer 로그 하나)."""
    tx_id: str
    from_addr: str
    to_addr: str
    amount: int          # 마이크로 USDT
    block_ts: int        # ms


def transfers_from_info(info: dict, contract: str = USDT_CONTRACT) -> list[Transfer]:
    """gettransactioninfobyid 결과 → 그 컨트랙트의 Transfer 로그들. 실패한 tx 는 빈 목록."""
    if not info or info.get("result") == "FAILED":
        return []
    if (info.get("receipt") or {}).get("result") != "SUCCESS":
        return []
    want = to_hex(contract)[2:]
    out = []
    for lg in info.get("log") or []:
        topics = lg.get("topics") or []
        if (lg.get("address") or "").lower().removeprefix("41") != want or len(topics) != 3:
            continue
        if topics[0].lower() != TRANSFER_TOPIC:
            continue
        try:
            frm = from_hex(topics[1][-40:])
            to = from_hex(topics[2][-40:])
            amount = int(lg.get("data") or "0", 16)
        except ValueError:
            continue
        out.append(Transfer(info["id"], frm, to, amount, int(info.get("blockTimeStamp") or 0)))
    return out


class TronGrid:
    """TronGrid HTTP (읽기 전용). 429/403 이면 RateLimited — 부르는 쪽이 쉬었다 다시."""

    class RateLimited(Exception):
        pass

    def __init__(self, base: str = "https://api.trongrid.io", api_key: str = "", client: httpx.AsyncClient | None = None):
        headers = {"Accept": "application/json"}
        if api_key:
            headers["TRON-PRO-API-KEY"] = api_key
        self.base = base.rstrip("/")
        self.http = client or httpx.AsyncClient(timeout=15, headers=headers)
        if client is not None:
            self.http.headers.update(headers)

    async def _get(self, path: str, params: dict) -> dict:
        r = await self.http.get(self.base + path, params=params)
        if r.status_code in (403, 429):
            raise TronGrid.RateLimited(r.status_code)
        r.raise_for_status()
        return r.json()

    async def _post(self, path: str, body: dict) -> dict:
        r = await self.http.post(self.base + path, json=body)
        if r.status_code in (403, 429):
            raise TronGrid.RateLimited(r.status_code)
        r.raise_for_status()
        return r.json()

    async def candidate_txids(self, address: str, since_ms: int, limit_pages: int = 5) -> list[tuple[str, int]]:
        """그 주소가 보내거나 받은 USDT tx 후보 (tx_id, block_ts ms), 오래된 순. 판정은 solid_info 로."""
        params = {"contract_address": USDT_CONTRACT, "min_timestamp": since_ms, "limit": 200,
                  "order_by": "block_timestamp,asc"}
        out: list[tuple[str, int]] = []
        for _ in range(limit_pages):
            data = await self._get(f"/v1/accounts/{address}/transactions/trc20", params)
            for row in data.get("data") or []:
                tid = row.get("transaction_id")
                if tid and (row.get("token_info") or {}).get("address") == USDT_CONTRACT:
                    out.append((tid, int(row.get("block_timestamp") or 0)))
            fp = (data.get("meta") or {}).get("fingerprint")
            if not fp:
                break
            params["fingerprint"] = fp
        return out

    async def solid_info(self, tx_id: str) -> dict | None:
        """확정 블록에 들어간 tx 정보. 아직 확정 전이면 None."""
        data = await self._post("/walletsolidity/gettransactioninfobyid", {"value": tx_id})
        return data if data and data.get("id") else None

    async def is_blacklisted(self, address: str) -> bool | None:
        """USDT 컨트랙트의 isBlackListed(address). 조회 실패면 None (모름)."""
        try:
            param = to_hex(address)[2:].rjust(64, "0")
            data = await self._post("/wallet/triggerconstantcontract", {
                "owner_address": USDT_CONTRACT, "contract_address": USDT_CONTRACT,
                "function_selector": "isBlackListed(address)", "parameter": param, "visible": True})
            res = (data.get("constant_result") or [None])[0]
            return None if res is None else int(res, 16) != 0
        except (httpx.HTTPError, ValueError, TronGrid.RateLimited) as e:
            log.warning("블랙리스트 조회 실패 %s: %r", address, e)
            return None

    async def usdt_balance(self, address: str) -> int | None:
        try:
            param = to_hex(address)[2:].rjust(64, "0")
            data = await self._post("/wallet/triggerconstantcontract", {
                "owner_address": address, "contract_address": USDT_CONTRACT,
                "function_selector": "balanceOf(address)", "parameter": param, "visible": True})
            res = (data.get("constant_result") or [None])[0]
            return None if res is None else int(res, 16)
        except (httpx.HTTPError, ValueError, TronGrid.RateLimited) as e:
            log.warning("잔액 조회 실패: %r", e)
            return None

    async def close(self) -> None:
        await self.http.aclose()
