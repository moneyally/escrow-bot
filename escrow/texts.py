"""고객에게 보이는 문구는 전부 여기. 원칙: 사람이 직접 쓴 것처럼 짧게. 이모지는 제목에 하나 정도, 느낌표·상담원 말투 X.
주소·금액은 <code> (누르면 복사). HTML 이라 사용자 입력은 반드시 esc().
"""
from __future__ import annotations

import html

from .money import fmt, fmt_exact

LINE = "──────────────"


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=False)


def who(username: str | None, name: str = "", tg_id: int | None = None) -> str:
    if username:
        return "@" + esc(username)
    if name:
        return esc(name)
    return f"#{tg_id}" if tg_id else "상대"


# ── 튜토리얼 ─────────────────────────────────────────
TUTORIAL = [
    ("에스크로가 뭐냐면 (1/3)",
     "돈을 중간에서 잠깐 예치해두는 거예요.\n\n"
     "모르는 사람이랑 거래하면 늘 이렇게 되죠.\n"
     "A: 돈 먼저 보내세요\nB: 아니 물건 먼저요\n\n"
     "여기선 그럴 필요 없어요.\n"
     "1. 정한 금액을 저희한테 예치하고\n"
     "2. 물건이나 작업을 주고받고\n"
     "3. 둘 다 완료 누르면\n"
     "4. 합의한 대로 저희가 보내드립니다."),
    ("진행 순서 (2/3)",
     "<b>1. 신청</b>\n[거래 신청] → 상대 아이디 입력 → 상대가 수락하면 둘만 들어가는 거래방이 열려요.\n\n"
     "<b>2. 공지 확인 → 예치</b>\n공지를 둘 다 확인하고, 각자 예치할지 말지 고릅니다.\n\n"
     "<b>3. 입금</b>\n입금 주소랑 정확한 금액은 봇 1:1 로만 가요. USDT(TRC20)만 됩니다.\n\n"
     "<b>4. 완료 → 정산</b>\n둘 다 [거래완료] → 나눌 방법 정하고 상대가 동의 → 받을 주소 입력 → 송금.\n\n"
     "수수료는 거래당 {fee} USDT, 받는 금액에서 빠집니다."),
    ("이것만은 지켜주세요 (3/3)",
     "· 거래방 밖에서 따로 돈 보내지 마세요.\n"
     "· 물건이나 작업 확인 전엔 [거래완료] 누르지 마세요.\n"
     "· 주소는 봇 1:1 로만 갑니다. 방이나 다른 대화에 올라온 주소로는 보내지 마세요.\n"
     "· 관리자는 먼저 DM 하지 않아요.\n"
     "· USDT(TRC20) 말고 다른 코인은 복구가 안 됩니다.\n\n"
     "문제 생기면 거래방에서 [관리자 호출]. 문의: {support}"),
]


def tutorial(i: int, fee: int, support: str) -> str:
    title, body = TUTORIAL[i]
    return f"<b>{title}</b>\n{LINE}\n\n" + body.format(fee=fmt(fee), support=esc(support))


TUTORIAL_END = "다 봤어요. 연습 거래 한번 해볼래요? 실제 돈은 안 움직여요."


# ── 메인 ─────────────────────────────────────────────
def main_menu(bot_name: str, support: str) -> str:
    return (f"<b>{esc(bot_name)}</b>\n{LINE}\n"
            "USDT(TRC20) 거래 대금을 중간에서 예치받아 드려요.\n\n"
            "1. [거래 신청] 누르고\n2. 상대 아이디 입력하고\n3. 거래방에서 진행하면 끝\n\n"
            f"문의: {esc(support)}")


ASK_PARTNER = ("<b>거래 신청</b>\n\n상대 텔레그램 아이디를 보내주세요.\n예: <code>@username</code>\n\n"
               "그만두려면 '취소'라고 보내면 돼요.")
PARTNER_SELF = "본인하고는 거래할 수 없어요. 상대 아이디를 보내주세요."
PARTNER_BAD = "아이디 모양이 아니에요. @ 붙여서 다시 보내주세요. 예: <code>@username</code>"
CANCELLED_INPUT = "그만뒀어요."
BANNED = "이용이 제한된 계정이에요. 문의: {support}"
DEPOSITS_OFF = "지금은 새 거래를 받지 않고 있어요. 잠시 뒤에 다시 해주세요."


def partner_unknown(username: str, link: str) -> str:
    return (f"@{esc(username)} 님은 아직 이 봇을 시작한 적이 없어요.\n\n"
            "아래 링크를 상대에게 보내주세요. 상대가 열고 수락하면 바로 이어집니다.\n"
            f"{esc(link)}\n\n이 링크는 30분 동안만 쓸 수 있어요.")


def invite_sent(partner: str, code: str) -> str:
    return f"{partner} 님에게 거래 요청을 보냈어요.\n거래코드: <code>{code}</code>\n\n상대가 수락하면 알려드릴게요."


def invite(creator: str, code: str, tg_id: int, done: int, disputes: int) -> str:
    history = f"이 봇에서 끝낸 거래 {done}건" + (f" · 분쟁 {disputes}건" if disputes else "")
    return (f"<b>거래 요청</b>\n{LINE}\n{creator} 님이 거래를 요청했어요.\n"
            f"숫자 ID: <code>{tg_id}</code> · {history}\n거래코드: <code>{code}</code>\n\n"
            "아는 사람인지 꼭 확인하고 수락하세요. 이름은 누구나 바꿀 수 있으니 숫자 ID 로 확인하세요.")


def declined(code: str) -> str:
    return f"상대가 거래 요청을 거절했어요. (<code>{code}</code>)"


def room_link(code: str, link: str) -> str:
    return (f"<b>거래방이 열렸어요</b>\n거래코드: <code>{code}</code>\n\n"
            f"아래 링크로 들어오세요. 거래 당사자 두 분만 들어올 수 있어요.\n{esc(link)}")


def no_room(code: str) -> str:
    return (f"거래방이 지금 다 차 있어서, 이 거래(<code>{code}</code>)는 봇 1:1 에서 진행할게요.\n"
            "상대에게도 같은 메시지가 갑니다.")


def notice(code: str, a: str, b: str, fee: int, minimum: int, support: str) -> str:
    return (f"<b>공지</b>\n{LINE}\n거래코드: <code>{code}</code>\n{a} · {b}\n{LINE}\n\n"
            f"· 수수료는 거래당 {fmt(fee)} USDT, 받는 금액에서 빠져요.\n"
            f"· 예치할 땐 한 사람당 최소 {fmt(minimum)} USDT.\n"
            "· 입금 주소와 정확한 금액은 봇 1:1 로만 보냅니다. 여기 올라온 주소로는 보내지 마세요.\n"
            "· 물건이나 작업을 확인한 뒤에만 [거래완료].\n"
            "· 단계마다 30분 안에 응답이 없으면 자동으로 취소돼요. 예치금이 있으면 환불 대기로 넘어갑니다.\n"
            "· 관리자는 먼저 DM 하지 않아요.\n\n"
            f"두 분 다 [공지 확인]을 눌러야 다음으로 넘어갑니다.\n문의: {esc(support)}")


NOTICE_FULL = ("<b>이용 규칙 전문</b>\n\n"
               "1. 예치금은 두 분이 합의한 분배안대로만 나갑니다. 한 사람 말만으론 못 움직여요.\n"
               "2. 분배가 안 맞으면 [관리자 호출]. 관리자가 거래방 기록과 증빙을 보고 정합니다.\n"
               "3. 끝자리까지 정확한 금액으로 보내야 자동으로 확인돼요. 금액이 다르면 확인이 늦어집니다.\n"
               "4. TRC20 USDT 외의 코인, 다른 네트워크로 보낸 건 돌려드리기 어렵습니다.\n"
               "5. 수수료는 정산할 때 받는 금액 비율대로 나눠서 뺍니다.\n"
               "6. 이 방 대화는 분쟁이 생기면 증빙으로 씁니다.")


def notice_status(code: str, rows: list[tuple[str, bool]]) -> str:
    lines = "\n".join(f"{n}: {'확인' if ok else '아직'}" for n, ok in rows)
    return f"공지 확인 (<code>{code}</code>)\n{lines}"


def choose(code: str, rows: list[tuple[str, str | None]]) -> str:
    label = {"deposit": "예치", "none": "예치 안 함", None: "아직"}
    lines = "\n".join(f"{n}: {label[c]}" for n, c in rows)
    return (f"<b>예치 선택</b>\n{LINE}\n거래코드: <code>{code}</code>\n\n"
            "각자 예치할지 골라주세요. 최소 한 분은 예치해야 진행돼요.\n\n" + lines)


BOTH_NONE = "두 분 다 예치 안 하면 진행할 게 없어요. 한 분은 [예치하기]를 골라주세요."


def ask_amount(code: str, minimum: int) -> str:
    return (f"얼마 예치하실 건가요? (거래 <code>{code}</code>)\n숫자만 보내주세요. 최소 {fmt(minimum)} USDT\n예: <code>1000</code>")


def amount_bad(minimum: int) -> str:
    return f"금액을 다시 확인해주세요. 숫자만, 최소 {fmt(minimum)} USDT 예요."


def deposit_guide(code: str, address: str, amount: int, minutes: int) -> str:
    return (f"<b>입금 안내</b> (거래 <code>{code}</code>)\n{LINE}\n"
            f"보낼 주소 (USDT · TRC20)\n<code>{esc(address)}</code>\n\n"
            f"보낼 금액 — 끝자리까지 그대로\n<code>{fmt_exact(amount)}</code> USDT\n{LINE}\n"
            "· 끝자리가 이 거래 표시예요. 금액이 다르면 자동 확인이 안 됩니다.\n"
            "· 거래소에서 보낼 땐 출금 수수료가 빠지지 않게, 받는 금액이 위와 같도록 보내주세요.\n"
            f"· {minutes}분 안에 보내주세요. 지나면 이 금액은 다시 받아야 해요.\n"
            "· 트론(TRX)은 따로 안 보내셔도 돼요.\n"
            "· 이 주소·금액은 이 메시지로만 드려요. 다른 데서 받은 주소면 보내지 마세요.\n\n"
            f"<a href=\"https://tronscan.org/#/address/{esc(address)}\">트론스캔에서 주소 확인</a>")


def deposit_seen(code: str, who_: str, amount: int, manual: bool) -> str:
    tail = "\n담당자가 확인하고 있어요. 보통 10분 안에 끝나요." if manual else ""
    return f"{who_} 님 입금이 들어왔어요. {fmt(amount)} USDT (<code>{code}</code>){tail}"


def deposit_confirmed(code: str, who_: str, amount: int) -> str:
    return f"{who_} 님 입금 확인 끝. {fmt(amount)} USDT (<code>{code}</code>)"


def funded(code: str, rows: list[tuple[str, int]], total: int) -> str:
    lines = "\n".join(f"{n}: {fmt(v)} USDT" for n, v in rows)
    return (f"<b>예치 완료 · 거래 시작</b>\n{LINE}\n거래코드: <code>{code}</code>\n\n{lines}\n합계: {fmt(total)} USDT\n{LINE}\n"
            "이제 거래 진행하시면 돼요.\n"
            "· 중간에 일부만 먼저 보내야 하면 [부분 지급]\n"
            "· 다 끝나면 두 분 다 [거래완료]\n"
            "· 문제 생기면 [관리자 호출]")


START_WAIT = "두 분 입금 다 확인됐어요. 담당자가 거래 시작을 승인하면 바로 알려드릴게요."


def complete_confirm(code: str) -> str:
    return f"거래완료로 처리할까요? (<code>{code}</code>)\n물건이나 작업 확인이 끝났을 때만 눌러주세요."


def complete_wait(code: str, other: str) -> str:
    return f"거래완료 눌렀어요. {other} 님 확인을 기다리는 중이에요. (<code>{code}</code>)"


def split_ask(code: str, rows: list[tuple[str, int]], total: int, fee: int) -> str:
    lines = "\n".join(f"{n}: {fmt(v)} USDT" for n, v in rows)
    return (f"<b>정산</b>\n{LINE}\n거래코드: <code>{code}</code>\n\n남은 금액\n{lines}\n합계: {fmt(total)} USDT\n{LINE}\n"
            f"어떻게 나눌지 골라주세요. 한 분이 고르면 다른 분이 동의해야 해요.\n수수료 {fmt(fee)} USDT 는 받는 금액 비율대로 빠집니다.")


def split_proposed(code: str, proposer: str, rows: list[tuple[str, int, int]]) -> str:
    lines = "\n".join(f"{n}: {fmt(g)} → 실수령 {fmt(g - f)} USDT (수수료 {fmt(f)})" for n, g, f in rows if g > 0)
    return f"<b>정산안</b> (<code>{code}</code>)\n{proposer} 님 제안\n\n{lines}\n\n동의하면 받을 주소를 1:1 로 물어볼게요."


def split_rejected(code: str, who_: str) -> str:
    return f"{who_} 님이 정산안에 동의하지 않았어요. 다시 골라주세요. (<code>{code}</code>)"


def ask_custom(code: str, other: str, total: int) -> str:
    return (f"<b>본인</b>이 받을 금액을 보내주세요. 나머지는 {other} 님이 받아요. (<code>{code}</code>)\n"
            f"0 ~ {fmt(total)} 사이, 숫자만.")


def ask_address(code: str, amount: int, step: int) -> str:
    if step == 1:
        return (f"<b>받을 주소</b> (거래 <code>{code}</code>)\n받을 금액: {fmt(amount)} USDT\n\n"
                "USDT(TRC20) 주소를 보내주세요. T 로 시작하는 34자예요.\n"
                "거래소 입금 주소라면 네트워크가 TRC20 인지 꼭 확인하세요.")
    return "확인용으로 같은 주소를 한 번 더 보내주세요."


ADDRESS_BAD = "TRC20 주소가 아니에요. T 로 시작하는 34자 주소를 다시 보내주세요."
ADDRESS_MISMATCH = "처음 보낸 주소랑 달라요. 처음부터 다시 보내주세요."
ADDRESS_BLOCKED = "이 주소는 USDT 를 받을 수 없는 주소예요(테더 동결 목록). 다른 주소를 보내주세요."


def address_new_warning(address: str) -> str:
    return (f"전에 쓰신 주소랑 다른 주소예요.\n<code>{esc(address)}</code>\n맞으면 한 번 더 같은 주소를 보내주세요.")


def address_saved(amount: int, address: str, manual: bool) -> str:
    tail = "담당자 확인 뒤 보내드려요." if manual else "곧 보내드려요."
    return f"받을 주소 저장했어요.\n<code>{esc(address)}</code>\n{fmt(amount)} USDT · {tail}"


def payout_sent(code: str, amount: int, tx: str) -> str:
    return (f"<b>송금 완료</b> (<code>{code}</code>)\n{fmt(amount)} USDT 보냈어요.\n"
            f"<a href=\"https://tronscan.org/#/transaction/{esc(tx)}\">트론스캔에서 확인</a>")


def deal_done(code: str) -> str:
    return f"<b>거래 끝</b> (<code>{code}</code>)\n이용해 주셔서 고마워요. 거래방은 곧 정리됩니다."


def partial_ask(code: str, other: str, balance: int, room_for: int) -> str:
    return (f"<b>부분 지급</b> (<code>{code}</code>)\n내 예치금에서 {other} 님에게 먼저 보낼 금액을 보내주세요.\n"
            f"보낼 수 있는 최대: {fmt(room_for)} USDT (내 잔액 {fmt(balance)})\n숫자만.")


def partial_made(code: str, frm: str, to: str, amount: int) -> str:
    return f"{frm} 님이 {to} 님에게 {fmt(amount)} USDT 부분 지급을 요청했어요. (<code>{code}</code>)\n{to} 님에게 받을 주소를 물어볼게요."


def cancel_done(code: str, reason: str = "") -> str:
    return f"거래가 취소됐어요. (<code>{code}</code>)" + (f"\n{esc(reason)}" if reason else "")


def cancel_asked(code: str, who_: str) -> str:
    return (f"{who_} 님이 거래 취소를 요청했어요. (<code>{code}</code>)\n"
            "동의하면 예치금은 각자에게 돌려드립니다.")


def refund_wait(code: str) -> str:
    return (f"예치금을 돌려드릴게요. (<code>{code}</code>)\n받을 주소를 1:1 로 물어볼게요.")


def expired(code: str, refunded: bool) -> str:
    tail = "\n예치금은 환불 대기로 넘어갔어요. 1:1 로 받을 주소를 물어볼게요." if refunded else ""
    return f"30분 동안 응답이 없어서 거래를 멈췄어요. (<code>{code}</code>){tail}"


def dispute_opened(code: str, who_: str) -> str:
    return (f"{who_} 님이 관리자를 불렀어요. (<code>{code}</code>)\n"
            "관리자가 확인할 때까지 거래가 잠깁니다. 증빙(대화·사진)이 있으면 이 방에 올려주세요.")


def dispute_decided(code: str, rows: list[tuple[str, int]]) -> str:
    lines = "\n".join(f"{n}: {fmt(v)} USDT" for n, v in rows if v > 0)
    return f"<b>관리자 결정</b> (<code>{code}</code>)\n{lines}\n\n받을 분에게 1:1 로 주소를 물어볼게요."


STALE = "지난 화면이에요."
NOT_PARTY = "이 거래 당사자만 누를 수 있어요."
LOCKED = "관리자 확인 중이라 잠겨 있어요."
NOTHING = "해당하는 거래가 없어요."


def deal_line(code: str, state_label: str, other: str, total: int) -> str:
    return f"<code>{code}</code> · {state_label} · {other} · {fmt(total)} USDT"


STATE_LABEL = {
    "INVITED": "상대 수락 대기", "ROOM": "거래방 입장 대기", "NOTICE": "공지 확인", "CHOOSE": "예치 선택",
    "DEPOSIT": "입금 대기", "START_REVIEW": "시작 승인 대기", "FUNDED": "거래 중", "SPLIT": "정산 방법",
    "ADDRESS": "받을 주소", "PAYOUT": "송금 대기", "DISPUTE": "관리자 확인", "REFUND": "환불 대기",
    "DONE": "끝", "CANCELLED": "취소",
}

# ── 관리자 알림 ──────────────────────────────────────
def adm_approval(kind_label: str, code: str, body: str) -> str:
    return f"🔔 <b>{kind_label}</b> · <code>{code}</code>\n{body}"


ADM_KIND = {"deposit": "입금 확인", "start": "거래 시작", "partial": "부분 지급", "final": "최종 정산",
            "refund": "환불", "payout_done": "송금 완료 확인"}


def address_again(code: str, reason: str) -> str:
    why = f"\n사유: {esc(reason)}" if reason.strip() else ""
    return f"<code>{code}</code> 받을 주소를 다시 받을게요. 아직 보내지 않았으니 걱정 마세요.{why}"


def room_lost(code: str) -> str:
    return f"거래방을 쓸 수 없게 돼서 <code>{code}</code> 거래는 여기 1:1 에서 이어갈게요. 돈과 진행 상황은 그대로예요."


def room_relink(code: str, link: str) -> str:
    return f"거래방 링크를 새로 만들었어요. (<code>{code}</code>)\n{esc(link)}\n2시간 동안 쓸 수 있어요."


ADDRESS_LATER = "알겠어요. 나중에 [받을 주소 입력] 버튼이나 /start 로 다시 하시면 돼요."
