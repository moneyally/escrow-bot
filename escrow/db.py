"""PostgreSQL 연결 (asyncpg). 상태를 바꾸는 일은 전부 `async with db.tx() as c:` 한 트랜잭션 안에서."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg

SCHEMA = Path(__file__).with_name("schema.sql")


async def _init_conn(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
    await conn.set_type_codec("json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


class DB:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    @classmethod
    async def connect(cls, dsn: str, min_size: int = 1, max_size: int = 10) -> "DB":
        pool = await asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size, init=_init_conn)
        return cls(pool)

    async def migrate(self) -> None:
        async with self.pool.acquire() as c:
            # 여러 프로세스가 동시에 켜져도 한 번에 하나만
            await c.execute("SELECT pg_advisory_lock(7291)")
            try:
                await c.execute(SCHEMA.read_text(encoding="utf-8"))
            finally:
                await c.execute("SELECT pg_advisory_unlock(7291)")

    @asynccontextmanager
    async def tx(self):
        async with self.pool.acquire() as c:
            async with c.transaction():
                yield c

    async def fetch(self, sql: str, *args):
        async with self.pool.acquire() as c:
            return await c.fetch(sql, *args)

    async def fetchrow(self, sql: str, *args):
        async with self.pool.acquire() as c:
            return await c.fetchrow(sql, *args)

    async def fetchval(self, sql: str, *args):
        async with self.pool.acquire() as c:
            return await c.fetchval(sql, *args)

    async def execute(self, sql: str, *args):
        async with self.pool.acquire() as c:
            return await c.execute(sql, *args)

    async def close(self) -> None:
        await self.pool.close()
