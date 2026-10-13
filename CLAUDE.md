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
- **장부 = 지급 줄의 debits** (누구 몫에서 얼마). `balances()` = 맡긴 돈 − 취소 안 된 모든 지급의 debits. 정산·환불은 받는 사람 자기 몫부터.
  받을 몫이 전부 수수료면 amount 0 지급 줄(confirmed)로 남김 — 안 남기면 늦은 입금 환불 때 수수료까지 돌려줌.
- 분쟁: 열면 확인 대기 전부 무효 + 승인 금지, 풀면 `_resume` 이 다시 걸고 끝낼 수 있으면 끝냄. 돈이 걸린 동안은 시간 초과로 안 멈춤.
- 늦은 입금(취소·만료·환불 뒤): 기다리는 입금 자리는 시간 지날 때까지 살려 둠 → 들어오면 그 거래 환불을 다시 엶.
- 입금 주소·금액은 1:1 로만 (거래방에 안 올림). 방엔 끝자리 없는 금액(base)만.
- **끝자리 금액**: 최근 24시간 안에 쓰인 금액은 다시 안 줌(`AMOUNT_HOLD`), 시작 끝자리 무작위. 입금 매칭은 금액 + 시간 창
  (`created_at ≤ block_ts ≤ expires_at+여유`, 만료된 자리 포함) — 늦은 돈이 다른 거래로 잡히지 않게.
- **잠금 순서는 항상 거래(`_lock`) → 하위 줄(자리·지급·승인)**. 하위 줄을 먼저 FOR UPDATE 하지 말 것 (교착).
- 지급 연결은 (tx_id, tx_idx) — 한 tx 에 여러 송금 가능. 승인된 지급도 안 보냈으면 관리자가 `cancel_payout` 으로 취소·주소 다시.
- 텔레그램 보내기는 outbox 로 (상태 변경과 같은 트랜잭션) — 커밋돼야 나감.
- 고객 문구는 `texts.py` 한 곳. 사람이 쓴 것처럼 짧게, 상담원 말투·느낌표 남발 금지.

## 구조
- `escrow/core.py` 거래 규칙·상태 · `bot.py` 텔레그램 화면 · `watcher.py` 트론 감시 · `admin/app.py` 관리 페이지 API + `admin/static/index.html`
- `tron.py` 주소 검사·TronGrid(읽기) · `money.py` 금액 · `texts.py` 문구 · `mock.py` 연습 거래 · `schema.sql` 표
- 상태: INVITED → ROOM → NOTICE → CHOOSE → DEPOSIT → (START_REVIEW) → FUNDED → SPLIT → ADDRESS → PAYOUT → DONE /
  CANCELLED · REFUND · DISPUTE. 30분 시간 초과는 돈이 안 걸린 단계(INVITED~DEPOSIT)만.
- 단계별 수동/자동: settings `mode.deposit|start|partial|final|refund|payout_done` (시작은 전부 manual), `auto_max_micro` 넘으면 수동.

## 배포 (서버 2.28.14.76 · https://2-28-14-76.sslip.io)
- **main 에 push = 2분 안에 서버 반영** (`escrow-update.timer` → `deploy/update.sh`: import 검사 → 반영 → 안 켜지면 되돌림, 결과는 관리자 텔레그램).
  그러니 push 전에 전체 테스트 필수.
- 저장소가 공개일 땐 HTTPS 로 받음. 비공개로 바꾸면 서버 배포 키(`/root/.ssh/escrow_deploy.pub`)를 GitHub 저장소 Deploy keys 에 등록해야 함.
- 첫 설치용 부트 통로(/_boot)는 닫힘 (10-13). 서버 SSH 는 비밀번호 로그인 꺼짐 — 급하면 헤츠너 웹 콘솔.
- 백업: 서버 안 매일 04:10 (14일) + 04:30 암호화본을 첫 관리자 텔레그램으로 (`escrow/backup.py`, 비밀번호 BACKUP_PASS).

## 테스트
- 진짜 PostgreSQL 필요. 로컬: `TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:55432/escrow_test python -m pytest -q`
  (컨테이너에선 `/usr/lib/postgresql/16/bin/initdb` 로 띄움 — 세션 메모 참고).
- 고친 버그는 테스트로: 고친 줄을 되돌리면 그 테스트가 실패해야 함.

## 운영 값 (10-13 채움)
봇 @dealscrow_safe_bot(딜스크로) · 고객센터 @jwjw0101 · 관리자 6835538952 · 받는 주소 TR7sTzwA9nVfDiSyge8Ez7GJ6jFng7tVC1 · TronGrid 키 있음.
PAY_ADDRESS 가 체크섬에 안 맞으면 입금 안내를 막음(`can_take_deposits`). 고객 문구는 '맡기다' 대신 '예치'.
