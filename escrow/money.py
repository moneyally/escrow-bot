"""금액: 전부 정수 마이크로 USDT (1 USDT = 1_000_000). float 금지.

USDT(TRC20) 는 소수 6자리라 체인 값(value)과 그대로 같다.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

MICRO = 1_000_000
_NUM = re.compile(r"^\d{1,12}(\.\d{1,6})?$")


def parse_usdt(text: str) -> int | None:
    """'1,000' · '1000.5' · '1000 USDT' → 마이크로. 음수·지수·7자리 이상 소수는 거절(None)."""
    t = (text or "").strip().upper().replace(",", "").replace("USDT", "").replace("테더", "").strip()
    if not _NUM.match(t):
        return None
    try:
        v = Decimal(t)
    except InvalidOperation:
        return None
    return int(v * MICRO)


def fmt(micro: int, places: int | None = None) -> str:
    """1000000 → '1', 1000013700 → '1,000.0137' (끝 0 은 지움). places 를 주면 그 자리까지 고정."""
    neg = micro < 0
    micro = abs(int(micro))
    whole, frac = divmod(micro, MICRO)
    s = f"{whole:,}"
    if places is not None:
        if places:
            s += "." + f"{frac:06d}"[:places]
    elif frac:
        s += "." + f"{frac:06d}".rstrip("0")
    return ("-" if neg else "") + s


def fmt_exact(micro: int) -> str:
    """보낼 금액 안내용: 쉼표 없이 소수 끝 0 지움 ('1000.0137'). 지갑에 그대로 붙여 넣는 값."""
    whole, frac = divmod(int(micro), MICRO)
    return f"{whole}" + (("." + f"{frac:06d}".rstrip("0")) if frac else "")


def split_fee(plan: dict[int, int], fee: int) -> dict[int, int]:
    """수수료를 받는 금액 비율로 나눔 (정수, 큰 나머지 순). 받는 금액이 0 인 사람은 0.
    합계 == min(fee, 총액). 각자 몫이 자기 받는 금액을 넘지 않음."""
    total = sum(v for v in plan.values() if v > 0)
    fee = min(max(fee, 0), total)
    if total == 0 or fee == 0:
        return {k: 0 for k in plan}
    base = {k: (v * fee) // total if v > 0 else 0 for k, v in plan.items()}
    rest = fee - sum(base.values())
    order = sorted((k for k in plan if plan[k] > 0), key=lambda k: (-((plan[k] * fee) % total), -plan[k], k))
    for k in order[:rest]:
        base[k] += 1
    return base
