"""환경 설정 (.env). 비밀값은 여기 기본값으로 두지 않는다.

아직 오너에게 받지 않은 값(관리자 ID·받는 주소·봇 이름·고객센터)은 STUB 기본값 — 운영 전에 .env 로 채울 것.
STUB 상태에서는 봇이 켜질 때 경고를 남기고, 받는 주소가 STUB 이면 입금 안내를 막는다.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

STUB_ADDRESS = "TSTUBxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"


def _load_env(path: Path) -> None:
    """KEY=VALUE 줄만 (따옴표 벗김). 이미 있는 환경변수는 덮지 않음."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip().strip('"').strip("'")
        os.environ.setdefault(k.strip(), v)


def _ids(raw: str) -> frozenset[int]:
    out = set()
    for part in raw.replace(" ", "").split(","):
        if part.lstrip("-").isdecimal():
            out.add(int(part))
    return frozenset(out)


@dataclass(frozen=True)
class Config:
    bot_token: str = ""
    database_url: str = "postgresql://escrow@127.0.0.1:5432/escrow"
    admin_ids: frozenset[int] = field(default_factory=frozenset)
    pay_address: str = STUB_ADDRESS
    bot_name: str = "에스크로"
    bot_username: str = ""          # @ 없이. 딥링크(t.me/<봇>?start=…)에 씀 — 비면 getMe 로 채움
    support: str = "@support"
    trongrid_url: str = "https://api.trongrid.io"
    trongrid_key: str = ""
    admin_secret: str = ""          # 관리 페이지 세션 서명용 (32자 이상 무작위)
    admin_base_url: str = ""        # 예: https://admin.example.com — 텔레그램 알림의 [관리 페이지] 버튼
    tz: str = "Asia/Seoul"

    @property
    def stub_fields(self) -> list[str]:
        out = []
        if not self.admin_ids:
            out.append("ADMIN_IDS")
        if self.pay_address == STUB_ADDRESS:
            out.append("PAY_ADDRESS")
        if self.bot_name == "에스크로":
            out.append("BOT_NAME")
        if self.support == "@support":
            out.append("SUPPORT")
        return out

    @property
    def can_take_deposits(self) -> bool:
        return self.pay_address != STUB_ADDRESS


def load(env_file: str | None = None) -> Config:
    _load_env(Path(env_file or os.environ.get("ESCROW_ENV", ".env")))
    e = os.environ.get
    return Config(
        bot_token=e("BOT_TOKEN", ""),
        database_url=e("DATABASE_URL", Config.database_url),
        admin_ids=_ids(e("ADMIN_IDS", "")),
        pay_address=e("PAY_ADDRESS", "").strip() or STUB_ADDRESS,
        bot_name=e("BOT_NAME", "").strip() or "에스크로",
        bot_username=e("BOT_USERNAME", "").strip().lstrip("@"),
        support=e("SUPPORT", "").strip() or "@support",
        trongrid_url=e("TRONGRID_URL", Config.trongrid_url).rstrip("/"),
        trongrid_key=e("TRONGRID_KEY", ""),
        admin_secret=e("ADMIN_SECRET", ""),
        admin_base_url=e("ADMIN_BASE_URL", "").rstrip("/"),
        tz=e("TZ_NAME", "Asia/Seoul"),
    )
