"""테스트 DB: 진짜 PostgreSQL (TEST_DATABASE_URL, 기본 로컬 55432). 테스트마다 스키마를 새로 만든다."""
import os
from datetime import datetime, timezone

import pytest

from escrow import core, db
from escrow.config import Config

DSN = os.environ.get("TEST_DATABASE_URL", "postgresql://postgres@127.0.0.1:55432/escrow_test")
ADMIN = 900
A, B, C = 101, 202, 303
ADDR = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"          # 체크섬 맞는 주소 (테스트에선 받는 주소로만 씀)
PAY = "TXYZopYRdj2D9XRtbG411XZZ3kM5VkAeBf"            # 우리 주소 (모양만)


@pytest.fixture
async def database():
    d = await db.DB.connect(DSN)
    async with d.pool.acquire() as c:
        await c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await d.migrate()
    yield d
    await d.close()


@pytest.fixture
def ctx():
    return core.Ctx(Config(admin_ids=frozenset({ADMIN}), pay_address=PAY, bot_name="테스트", support="@help"))


@pytest.fixture
async def users(database):
    async with database.tx() as c:
        for i, name in ((A, "alice"), (B, "bobby"), (C, "carol"), (ADMIN, "admin")):
            await core.upsert_user(c, i, name, name.title())
    return database


def ts():
    return datetime.now(timezone.utc)
