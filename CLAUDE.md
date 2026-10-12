# 에스크로 봇 — Claude 작업 메모

USDT(TRC20) 거래 중개 텔레그램 봇. 설계 = `docs/DESIGN.md`, 사진 대조 = `docs/PHOTO_CHECK.md`. 소담(sodam_bot)과 **별개 프로젝트** (코드·서버·DB 공유 안 함).

## 사용자와 말하는 법
- **한국어 반말, 짧게, 결론 먼저.** 영어 문장 금지 (사용자는 영어를 모름). 코드·명령어만 원래 글자.
- 서버에서 직접 확인한 것만 '됐다'. 아니면 '아직/미확인'.

## 절대 규칙
- **서버·저장소에 지갑 개인키·시드 금지.** 이 프로젝트엔 서명·전송 코드가 없다 (지급은 오너가 자기 지갑에서 직접).
- 비밀값(.env: BOT_TOKEN·DATABASE_URL·ADMIN_SECRET·TRONGRID_KEY)은 커밋 금지. 저장소는 비공개지만 그래도.
- 돈 숫자는 정수 마이크로 USDT (`money.py`). float 금지.
- 돈·상태를 바꾸는 건 `core.py` 함수로만, 한 트랜잭션 + 거래 줄 잠금(`_lock`) + `events` 기록. 봇·관리 페이지·감시는 core 만 부른다.
- 지급 합계 ≤ 맡긴 합계 (`_make_payouts`). 지급 줄은 `approved` 가 돼야 관리 페이지 '보낼 것'에 뜸.
- 입금 주소·금액은 1:1 로만 (거래방에 안 올림).
- 텔레그램 보내기는 outbox 로 (상태 변경과 같은 트랜잭션) — 커밋돼야 나감.
- 고객 문구는 `texts.py` 한 곳. 사람이 쓴 것처럼 짧게, 상담원 말투·느낌표 남발 금지.

## 구조
- `escrow/core.py` 거래 규칙·상태 · `bot.py` 텔레그램 화면 · `watcher.py` 트론 감시 · `admin/app.py` 관리 페이지 API + `admin/static/index.html`
- `tron.py` 주소 검사·TronGrid(읽기) · `money.py` 금액 · `texts.py` 문구 · `mock.py` 연습 거래 · `schema.sql` 표
- 상태: INVITED → ROOM → NOTICE → CHOOSE → DEPOSIT → (START_REVIEW) → FUNDED → SPLIT → ADDRESS → PAYOUT → DONE /
  CANCELLED · REFUND · DISPUTE. 30분 시간 초과는 돈이 안 걸린 단계(INVITED~DEPOSIT)만.
- 단계별 수동/자동: settings `mode.deposit|start|partial|final|refund|payout_done` (시작은 전부 manual), `auto_max_micro` 넘으면 수동.

## 테스트
- 진짜 PostgreSQL 필요. 로컬: `TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:55432/escrow_test python -m pytest -q`
  (컨테이너에선 `/usr/lib/postgresql/16/bin/initdb` 로 띄움 — 세션 메모 참고).
- 고친 버그는 테스트로: 고친 줄을 되돌리면 그 테스트가 실패해야 함.

## 오너가 아직 안 준 값 (STUB)
ADMIN_IDS · PAY_ADDRESS · BOT_NAME · SUPPORT · BOT_TOKEN → `.env` (예시 `.env.example`). STUB 이면 봇이 경고, 주소가 STUB 이면 입금 안내를 막음.
