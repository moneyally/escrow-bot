"""🔌 에스크로 서버 원격 점검 (클로드 작업 환경 → 서버, 읽기 전용). 서버 쪽은 escrow/admin/diag.py.

    python tools/diag.py health                      버전·서비스 상태·감시 나이·통계·도우미 계정
    python tools/diag.py deals [state=ROOM] [limit=30]
    python tools/diag.py deal code=AB12CD            거래 하나 전부 (자리·지급·승인·기록·보낸 메시지)
    python tools/diag.py outbox                      못 보낸·오류 난 텔레그램 메시지
    python tools/diag.py events [kind=room_created]  · rooms · chain · settings · tables
    python tools/diag.py logs unit=escrow-bot lines=200 [grep=실패] [since='30 min ago']

토큰: 환경변수 ESCROW_DIAG_TOKEN (오너가 봇 1:1 /diag 로 받아 클로드 환경 비밀값에 넣음 — 출력·저장 안 함).
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

URL = os.getenv("ESCROW_DIAG_URL", "https://2-28-14-76.sslip.io").rstrip("/")


def _ctx() -> ssl.SSLContext:
    for f in (os.getenv("SSL_CERT_FILE"), os.getenv("REQUESTS_CA_BUNDLE"), "/root/.ccr/ca-bundle.crt"):
        if f and os.path.exists(f):
            return ssl.create_default_context(cafile=f)
    return ssl.create_default_context()


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    tok = os.getenv("ESCROW_DIAG_TOKEN", "").strip()
    if not tok:
        sys.exit("ESCROW_DIAG_TOKEN 없음 — 오너가 봇에 /diag 로 받은 토큰을 클로드 환경 비밀값에 넣어야 해요")
    q = urllib.parse.urlencode(dict(a.split("=", 1) for a in argv[1:] if "=" in a))
    req = urllib.request.Request(f"{URL}/diag/v1/{argv[0]}" + (f"?{q}" if q else ""), headers={"Authorization": f"Bearer {tok}"})
    try:
        with urllib.request.urlopen(req, timeout=40, context=_ctx()) as r:
            body, code = json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        code = e.code
        try:
            body = json.loads(e.read())
        except ValueError:
            body = {"error": e.reason}
    print(json.dumps(body, ensure_ascii=False, indent=1))
    return 0 if code == 200 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
