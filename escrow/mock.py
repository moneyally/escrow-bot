"""연습 거래 (튜토리얼 뒤 '연습 거래 해보기'). 실제 거래와 같은 화면 순서지만 DB·돈 없음 — 사람마다 메모리에만.
상대(연습상대)는 봇이 대신 1~2초 뒤에 움직인다.
"""
from __future__ import annotations

from . import texts as T
from .money import MICRO, fmt

CODE = "DEMO01"
PARTNER = "연습상대"
FAKE_ADDR = "T연습용주소-실제로-보내지-마세요"


def kb(*rows):
    return [list(r) for r in rows]


QUIT = ("연습 그만두기", "m:quit")


def start():
    return (f"<b>연습 거래방이 열렸어요</b>\n{T.LINE}\n거래코드: <code>{CODE}</code>\n상대: {PARTNER} (봇이 대신 움직여요)\n{T.LINE}\n\n"
            "실제 거래에선 여기서 거래방 링크가 1:1 로 와요. 실제 돈은 전혀 안 움직입니다.",
            kb([("거래방 들어가기", "m:room")], [QUIT]))


def notice(fee: int, minimum: int, support: str):
    return (T.notice(CODE, "나", PARTNER, fee, minimum, support) + "\n\n<i>(연습) 실제 거래에선 두 분 다 눌러야 넘어가요.</i>",
            kb([("공지 확인", "m:ntc"), ("규칙 전문", "m:nfull")], [QUIT]))


def notice_mine():
    return T.notice_status(CODE, [("나", True), (PARTNER, False)]), None


def choose():
    return (T.choose(CODE, [("나", None), (PARTNER, "deposit")]) + f"\n\n<i>(연습) {PARTNER} 님은 맡기기를 골랐어요.</i>",
            kb([("맡기기", "m:dep"), ("안 맡기기", "m:nodep")], [QUIT]))


def ask_amount():
    return ("얼마 맡겨볼까요? (연습)\n실제로는 숫자를 직접 보내요.",
            kb([("100", "m:amt:100"), ("1,000", "m:amt:1000"), ("10,000", "m:amt:10000")], [QUIT]))


def deposit_guide(amount: int):
    exact = amount * MICRO + 137 * 100
    text = T.deposit_guide(CODE, FAKE_ADDR, exact, 30).replace("<b>입금 안내</b>", "<b>입금 안내 (연습)</b>")
    return (text + "\n\n<b>연습용이에요. 위 주소로 실제로 보내지 마세요.</b>",
            kb([("입금했어요 (연습)", "m:paid")], [QUIT]))


def checking():
    return "입금 확인 중… (실제로는 보통 1~2분, 담당자 확인이 있으면 10분 안)", None


def funded(mine: int, theirs: int):
    rows = [("나", mine * MICRO)] if mine else []
    rows.append((PARTNER, theirs * MICRO))
    total = sum(v for _, v in rows)
    return (T.funded(CODE, rows, total),
            kb([("부분 지급", "m:part"), ("거래완료", "m:fin")], [("관리자 호출", "m:sos")], [QUIT]))


def complete_confirm():
    return T.complete_confirm(CODE), kb([("예", "m:finy"), ("아니요", "m:finn")])


def complete_wait():
    return T.complete_wait(CODE, PARTNER), None


def split(mine: int, theirs: int, fee: int):
    rows = ([("나", mine * MICRO)] if mine else []) + [(PARTNER, theirs * MICRO)]
    return (T.split_ask(CODE, rows, sum(v for _, v in rows), fee),
            kb([("각자 맡긴 만큼 돌려받기", "m:sp")], [("전액 나에게", "m:sp")], [(f"전액 {PARTNER} 에게", "m:sp")],
               [("직접 입력", "m:sp")], [QUIT]))


def finished():
    return (f"{PARTNER} 님이 동의했어요.\n\n실제 거래라면 여기서 받을 주소를 1:1 로 두 번 입력하고, 확인이 끝나면 그 주소로 보내드려요.\n\n"
            "<b>연습 끝.</b> 이제 실제 거래를 시작해도 돼요.", None)


PARTIAL_INFO = "부분 지급: 내가 맡긴 돈 중 일부를 상대에게 먼저 보내는 거예요. 실제론 금액 입력 → 상대 주소 → 송금 순서예요."
SOS_INFO = "관리자 호출: 거래가 잠기고 관리자가 증빙을 보고 분배를 정해요. 연습에선 눌러도 아무 일 없어요."
ON_NOTICE_FULL = T.NOTICE_FULL


def partner_line(text: str) -> str:
    return f"<i>{PARTNER}</i>: {text}"


def money(v: int) -> str:
    return fmt(v * MICRO)
