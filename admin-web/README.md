# 관리 페이지 (React)

`escrow/admin/app.py`(FastAPI) API 를 쓰는 관리 화면. 디자인은 Apple HIG 토큰
([axiaoge2/Apple-Hig-Designer](https://github.com/axiaoge2/Apple-Hig-Designer), MIT — `src/vendor/`) 위에 만듦.

- 개발: `npm install && npm run dev` (API 는 127.0.0.1:8090 으로 넘김)
- 빌드: `npm run build` → `escrow/admin/web/` (커밋함 — 서버엔 node 가 없어서 빌드 결과를 그대로 올림)
- 화면: 대시보드 · 확인 대기 · 보낼 것 · 거래(상세·분쟁 결정·취소) · 입금 확인 · 분쟁 · 사용자 · 거래방 · 설정 · 기록
- 데스크톱은 왼쪽 사이드바, 폰(744px 미만)은 위쪽 가로 탭. 밝게/어둡게는 시스템 설정 따라감.
