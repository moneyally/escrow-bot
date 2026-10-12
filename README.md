# 에스크로 봇

USDT(TRC20) 거래를 중간에서 맡아 주는 텔레그램 봇 + 관리 페이지.

## 프로세스
| 이름 | 명령 | 하는 일 |
|---|---|---|
| 봇 | `python -m escrow.bot` | 텔레그램 화면·버튼, 보낼 메시지(outbox) 처리, 30초마다 시간 초과 정리 |
| 감시 | `python -m escrow.watcher` | 받는 주소의 USDT 입금·출금을 확정 블록 기준으로 찾아 거래에 연결 |
| 관리 페이지 | `uvicorn escrow.admin.app:app --port 8090` | 확인 대기·보낼 것·거래·입금·분쟁·사용자·거래방·설정·기록 |

## 서버 설치 (헤츠너 Ubuntu 24.04)
```
git clone <이 저장소> /opt/escrow   # 비공개 저장소라 배포 키나 토큰 필요
bash /opt/escrow/deploy/install.sh admin.도메인.com
nano /opt/escrow/.env                # BOT_TOKEN · ADMIN_IDS · PAY_ADDRESS · BOT_NAME · SUPPORT
systemctl restart escrow-bot escrow-watcher escrow-admin
```
업데이트: `bash /opt/escrow/deploy/update.sh`

## 운영 순서
1. 봇에 `/id` → 내 숫자 ID → `.env` 의 ADMIN_IDS
2. 거래방 만들기: 비공개 그룹 → 슈퍼그룹 · '새 멤버에게 대화 기록 숨기기' 켜기 → 봇을 관리자로(사용자 초대·차단·메시지 삭제) — 관리자 계정이 넣어야 등록됨
3. 관리 페이지: 봇에 `/login` → 5분짜리 1회용 링크
4. 단계별 수동/자동은 관리 페이지 '설정' (처음엔 전부 수동)

## 테스트
```
pip install -r requirements-dev.txt
TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5432/escrow_test python -m pytest -q
```
