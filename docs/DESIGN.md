# 에스크로 봇 설계 (v1, 2026-10-12)

USDT(TRC20) 거래 중개 텔레그램 봇. 두 사람이 거래 대금(보증금)을 운영자에게 맡기고, 거래가 끝나면 합의한 대로 돌려받는다.
참고한 화면: 기존 에스크로 봇 15장 (튜토리얼 3장 → 모의거래 → 공지 확인 → 예치 선택 → 입금 안내 → 예치 완료 → 거래완료 → 분배 → 메인 메뉴).
**그 봇의 이름·로고·문구는 쓰지 않는다** (흐름만 참고, 문구는 전부 새로).

## 0. 확정된 결정 (오너, 2026-10-12)
| 항목 | 값 |
|---|---|
| 체인 / 코인 | TRON / USDT (TRC20, 공식 컨트랙트 `TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t` 만) |
| 받는 주소 | **오너 주소 하나** (거래마다 주소를 만들지 않음) |
| 지급 | **오너가 자기 지갑에서 직접 송금** → 봇이 출금을 감지해서 확인. 서버에 개인키·시드 **없음** |
| 수수료 | 건당 20 USDT (받는 금액에서 차감) |
| 최소 예치 | 50 USDT |
| 단계별 처리 | 전부 **수동**(관리자 승인)으로 시작, 관리 페이지에서 단계마다 자동으로 바꿀 수 있음 |
| 서버 / DB | 헤츠너 VPS / PostgreSQL 16 |
| 언어 | Python 3.12, python-telegram-bot 21, FastAPI(관리 페이지), asyncpg |

아직 받을 값 (`.env`, 코드엔 넣지 않음): 관리자 텔레그램 ID들 · 받는 주소 · 봇 이름 · 고객센터 @아이디 · 봇 토큰 · TronGrid API 키.

## 1. 리서치 결과 요약 (2026-10-12)
- **그대로 쓸 오픈소스 없음.** 텔레그램 에스크로 봇 오픈소스(Pycomet escrow-service-bot·kresswell EscrowBot·NovaEscrow 등)는
  DB 잠금 없음·지급 중복 방지 없음·서버에 개인키·float 금액 → 돈 다루는 데 못 씀. 흐름만 참고.
- 아이디어만 가져옴 (코드 X, GPL): **assimon/epusdt** 고유 금액 대조 — (주소, 금액)이 쓰이고 있으면 0.0001 씩 올려 다시(최대 100번), 일정 시간 점유.
  **lnp2pBot** 분쟁 흐름 — 중재자 배정·분쟁 횟수 기록·시간 초과 시 관리자 알림.
- 라이브러리: `tronpy` 0.6.2 (MIT, AsyncTron, `get_solid_transaction_info`) · python-telegram-bot 21.
- TronGrid `/v1/accounts/{addr}/transactions/trc20` 는 **후보 찾기용만** — 응답에 블록·성공 여부가 없고 `only_confirmed` 정의가 문서에 없음.
  최종 판정은 `/walletsolidity/gettransactioninfobyid`(확정 블록, 보통 ~1분)의 성공 결과 + Transfer 로그로.
  API 키는 `TRON-PRO-API-KEY` 헤더, 한도는 요금제별(429/403 이면 쉬었다가).
- USDT 컨트랙트에 `isBlackListed` 있음 → 입금 보낸 주소·받을 주소 검사.
- 텔레그램: **봇은 그룹을 못 만듦** → 미리 만든 방 풀. `createChatInviteLink(creates_join_request=True)` 는 `member_limit` 과 같이 못 씀 →
  가입 신청으로 받고 당사자 ID 만 `approveChatJoinRequest`, 나머지 `decline`. 내보내기 = `banChatMember` → `unbanChatMember`.
  `deleteMessages` 한 번 100개·48시간 지난 건 못 지움. 관리 페이지 로그인 = 텔레그램 로그인(OIDC 방식 권장, 옛 위젯은 보관 문서).

## 2. 구조
```
[텔레그램] ⇄ bot (PTB 21) ─┐
                            ├─ PostgreSQL ─ admin (FastAPI + 웹 화면, 텔레그램 로그인)
[TronGrid]  ⇄ watcher ─────┘
```
- 프로세스 3개 (systemd): `bot`, `watcher`(입금·출금 감시), `admin`(관리 페이지·API). 같은 서버, Caddy HTTPS.
- **돈이 나가는 건 사람(오너)만.** 서버는 '보낼 목록'을 만들고, 나간 걸 확인만 한다.

## 3. 거래 흐름 (상태)
```
OPEN(신청) → INVITED(상대 수락 대기) → ROOM(거래방 배정·입장)
→ NOTICE(공지 확인, 둘 다) → CHOOSE(예치/안 함, 둘 다)
→ AWAIT_DEPOSIT(입금 대기) → [수동: 관리자 입금 확인] → FUNDED(거래 진행)
   ⇄ 부분 지급 (둘 다 동의 → [수동: 관리자 승인] → 지급 대기 → 송금 → 확인)
→ COMPLETE_REQ(거래완료, 둘 다) → SPLIT(분배 방법 선택 → 상대 동의)
→ ADDRESS(받을 주소, 1:1, 두 번 입력) → [수동: 관리자 승인] → PAYOUT(지급 대기)
→ 오너 송금 → watcher 감지 → [수동: 관리자 완료 확인] → DONE → 방 정리·반납
```
빠지는 길:
- `CANCELLED`: 입금 전엔 한쪽 취소 가능 / 입금 후엔 둘 다 동의 → `REFUND`(환불 지급 대기).
- `EXPIRED`: 단계마다 30분 무응답 → 입금된 돈이 없으면 취소, 있으면 `REFUND`(환불 대기 목록).
- `DISPUTE`: 관리자 호출 → 거래 잠김(버튼 막힘) → 관리자가 분배 결정 → `PAYOUT`.

규칙:
- 버튼은 **콜백에 거래 ID + 단계 번호(version)** 를 담고, 처리 때 `SELECT … FOR UPDATE` 로 지금 단계·당사자인지 다시 확인. 지난 단계 버튼 = '지난 화면이에요'.
- 상태 바꾸기는 한 트랜잭션 + `events` 표에 한 줄 (누가·무엇·이전→다음).

## 4. 단계별 수동/자동 (관리 페이지 설정, 시작은 전부 수동)
| 키 | 단계 | 수동이면 | 자동이면 |
|---|---|---|---|
| `mode.deposit` | 입금 인정 | 관리자 [확인] | 끝자리까지 맞으면 바로 |
| `mode.start` | 거래 시작 | 관리자 [시작 승인] | 양쪽 예치 끝나면 바로 |
| `mode.partial` | 부분 지급 | 관리자 [승인] | 양쪽 동의면 바로 지급 대기 |
| `mode.final` | 최종 분배 | 관리자 [승인] | 양쪽 동의면 바로 지급 대기 |
| `mode.payout_done` | 지급 완료 | 관리자 [완료] (또는 해시 입력) | 출금 감지되면 바로 |
| `mode.cancel` | 입금 후 취소·환불 | 관리자 [승인] | 둘 다 동의면 바로 |
- 금액 기준 섞기: `auto_max_usdt` (이하만 자동, 넘으면 수동). 수동 단계에선 고객에게 "담당자가 확인 중이에요".
- 승인은 관리 페이지 또는 관리자 텔레그램 알림의 [승인][보류] 버튼 (둘 다 같은 함수, 누를 때 관리자 ID 재확인).

## 5. 입금 구분 (주소 하나)
- 예치 금액 X 를 정하면 봇이 **끝자리 금액**을 배정: `X.000100` 부터, 이미 진행 중 거래가 쓰는 금액이면 +0.0001 (최대 100번).
  DB 고유 제약 `(amount_micro) WHERE active` 로 겹침 불가. 배정 금액은 입금 대기 동안 점유, 끝나거나 만료되면 풀림.
- 끝자리 금액은 고객 몫 (예: 1000.0137 보내면 1000.0137 로 장부 기록). 수수료는 지급 때 차감.
- 감시: TronGrid 목록(우리 주소로 들어온 USDT, 3초마다·최근 것만) → 처음 보는 tx → 확정 블록 영수증으로 성공·컨트랙트·받는 주소·금액 재확인
  → `chain_tx` (tx_id 유일) 저장 → 금액이 배정표와 정확히 맞으면 그 거래의 '입금 후보'.
- 안 맞는 입금(금액 다름·늦게 옴·다른 토큰): 관리 페이지 '확인 필요' → 관리자가 거래에 수동 연결 또는 반환 처리.
- 보낸 주소가 `isBlackListed` 면 자동 처리 안 함 + 관리자 경고.

## 6. 지급 (오너 직접 송금)
- `payouts` 줄: 거래·받는 사람·받는 주소·금액(수수료 차감 뒤)·상태(`draft`→`approved`→`sent`→`confirmed`)·tx_id.
- 관리 페이지 '지급 대기'에 주소·금액 복사 버튼. 오너가 지갑에서 송금.
- watcher 가 **우리 주소에서 나간** USDT 를 보고, 받는 주소 + 금액이 `approved` 지급 줄과 정확히 맞으면 `sent` + tx_id 연결
  (`mode.payout_done` 수동이면 관리자 [완료] 뒤 `confirmed`). 맞는 줄이 없으면 '설명 안 되는 출금' 경고.
- 거래당 지급 합계 ≤ 예치 합계 − 수수료: DB 에서 지급 줄 만들 때 같은 트랜잭션으로 검사 (중복 지급 불가).
- 받을 주소는 두 번 입력해서 같아야 하고, `T` + base58 34자 + 체크섬 검사, 예전에 쓴 주소와 다르면 한 번 더 확인, 블랙리스트 검사.

## 7. 거래방 (방 풀)
- 오너가 비공개 그룹 N 개를 만들고 봇을 관리자(초대·메시지 삭제·차단 권한)로 넣음 → 봇이 `room_pool` 에 등록(봇이 관리자가 된 걸 감지).
- 거래 배정: 빈 방 하나 잠금 → 가입 신청 링크 만들기 → 두 당사자에게 1:1 로 링크 → 신청자가 당사자면 승인, 아니면 거절.
- 끝나면: 대화 백업(우리가 받은 메시지만 — 봇은 방 기록을 못 읽음) → 봇 메시지 삭제 → 두 사람 내보내기 → 링크 폐기 → 반납.
- 빈 방 5개 이하면 관리자 알림. 방이 없으면 거래방 없이 1:1 로만 진행하는 길도 둠.

## 8. DB (PostgreSQL)
| 표 | 핵심 열 |
|---|---|
| `users` | tg_id PK, username, name, banned, created_at |
| `deals` | id, code(유일, 6자), state, version, buyer/seller(=신청자/상대) tg_id, room_id, fee_micro, created/updated |
| `deal_parties` | deal_id, tg_id, role, notice_ok, deposit_choice, deposit_target_micro, complete_pressed |
| `deposit_slots` | deal_id, tg_id, amount_micro(활성일 때 유일), expires_at, status |
| `chain_tx` | tx_id 유일, direction(in/out), from, to, amount_micro, block_ts, solid, matched_slot / matched_payout |
| `payouts` | id, deal_id, to_tg_id, address, amount_micro, kind(partial/final/refund), status, tx_id, approved_by |
| `splits` | deal_id, proposer, plan(JSON: 사람별 금액), agreed_by |
| `room_pool` | chat_id, title, state(free/used/broken), deal_id, invite_link |
| `disputes` | deal_id, opened_by, reason, decision(JSON), decided_by |
| `settings` | key, value (mode.*, fee, min, timeouts, auto_max) |
| `admin_actions` / `events` | 누가·언제·무엇 (지우기·고치기 금지: 권한으로 UPDATE/DELETE 막음) |
- 금액은 전부 정수 **마이크로 USDT** (1 USDT = 1,000,000). float 금지.

## 9. 관리 페이지 API (FastAPI, 텔레그램 로그인 → 관리자 ID 만)
| 화면 | API |
|---|---|
| 대시보드 | `GET /api/stats` (예치 중 합계·지급 대기·오늘 거래·수수료·지갑 잔액 vs 장부) |
| 거래 | `GET /api/deals?state=` · `GET /api/deals/{code}` · `POST /api/deals/{code}/cancel` |
| 승인 대기 | `GET /api/approvals` · `POST /api/approvals/{id}/approve` · `/hold` |
| 입금 | `GET /api/deposits?unmatched=1` · `POST /api/deposits/{tx}/assign` · `/return` |
| 지급 | `GET /api/payouts?status=` · `POST /api/payouts/{id}/approve` · `/done` (tx 해시) |
| 분쟁 | `GET /api/disputes` · `POST /api/disputes/{id}/resolve` |
| 사용자 | `GET /api/users/{id}` · `POST /api/users/{id}/ban` · `/unban` |
| 방 풀 | `GET /api/rooms` · `POST /api/rooms/{id}/reset` |
| 설정 | `GET /api/settings` · `PUT /api/settings` (단계별 수동/자동·수수료·최소·시간) |
| 기록 | `GET /api/audit` |
- 돈이 움직이는 POST 는 CSRF 토큰 + 관리자 재확인 + `admin_actions` 기록. 같은 요청 두 번 = 한 번만 처리(멱등 키).

## 10. 문구 원칙 (사람이 쓴 것처럼)
- 이모지는 제목에 하나 정도. 느낌표·'~해드릴게요!'·'걱정 마세요 😊' 같은 상담원 말투 X. 짧게, 운영자가 직접 쓰는 말.
- 주소·금액은 `<code>` 로 (눌러서 복사). 경고는 실제로 사고 나는 것만 (다른 코인·방에 올라온 주소·먼저 오는 관리자 DM).
- 문구는 `texts/ko.py` 한 곳에 (코드에 흩지 않음) → 나중에 바꾸기 쉽게.

## 11. 사진 대조 (구현 뒤 표로 채움)
튜토리얼 1/3·2/3·3/3 · 모의 거래방 생성 · 공지 확인 · 예치 선택 · 입금 안내(1:1) · 입금 확인 중 · 양측 예치 완료 · 거래완료 확인 ·
양측 완료 → 분배 4버튼 · 튜토리얼 완료 → 메인 메뉴 · 에스크로 신청(아이디 입력) · 환불 대기 · 진행 중 거래 — 각각 '됨 / 다르게 됨(이유) / 안 됨'.
알려진 차이: 그 봇은 '트론 50개도 함께 보내라' → 우리는 오너 지갑에서 수수료가 나가니 필요 없음. 입금 주소는 거래마다가 아니라 하나 + 끝자리 금액.
