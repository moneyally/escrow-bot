"""도우미 계정(사람 텔레그램 계정)으로 거래방(슈퍼그룹) 자동 만들기 — 봇은 규칙상 그룹을 못 만들어서.

- 로그인: 관리 페이지 [설정 → 도우미 계정]에서 api_id·api_hash·전화번호 → 텔레그램이 보낸 코드(+2단계 비밀번호) 입력.
  세션 문자열은 DB kv 'helper' 에만 (관리 페이지 API 는 절대 돌려주지 않음).
- 방 만들기: 새 슈퍼그룹 → '새 멤버에게 이전 대화 숨기기' → 봇 초대 + 관리자(초대·차단·메시지 삭제) → 운영 관리자 초대 시도.
  만든 방은 거래가 끝나면 비우고 다음 거래에 다시 씀 (계정이 방을 너무 많이 만들면 텔레그램이 제한함).
"""
from __future__ import annotations

import logging

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession
from telethon.tl.functions.channels import (CreateChannelRequest, EditAdminRequest, InviteToChannelRequest,
                                            TogglePreHistoryHiddenRequest)
from telethon.tl.types import ChatAdminRights

log = logging.getLogger("escrow.helper")
KV_KEY = "helper"


async def load(db) -> dict | None:
    v = await db.fetchval("SELECT value FROM kv WHERE key=$1", KV_KEY)
    return v if v and v.get("session") else None


async def save(db, data: dict) -> None:
    await db.execute("INSERT INTO kv(key, value) VALUES($1,$2) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", KV_KEY, data)


def public(data: dict | None) -> dict:
    """관리 페이지에 보여 줄 것만 (세션·api_hash 빼고)."""
    if not data:
        return {"connected": False}
    return {"connected": True, "user_id": data.get("user_id"), "name": data.get("name"), "username": data.get("username"),
            "rooms_made": data.get("rooms_made", 0)}


class Login:
    """관리 페이지 로그인 단계 (코드 요청 → 코드 입력). 관리 페이지 프로세스 메모리에만 잠깐."""

    def __init__(self, api_id: int, api_hash: str, phone: str):
        self.api_id, self.api_hash, self.phone = api_id, api_hash, phone
        self.client = TelegramClient(StringSession(), api_id, api_hash)
        self.code_hash = ""

    async def send_code(self) -> None:
        await self.client.connect()
        r = await self.client.send_code_request(self.phone)
        self.code_hash = r.phone_code_hash

    async def finish(self, code: str, password: str = "") -> dict:
        """성공하면 저장할 dict. 2단계 비밀번호가 필요한데 없으면 PasswordNeeded."""
        try:
            await self.client.sign_in(self.phone, code, phone_code_hash=self.code_hash)
        except SessionPasswordNeededError:
            if not password:
                raise PasswordNeeded()
            await self.client.sign_in(password=password)
        me = await self.client.get_me()
        data = {"api_id": self.api_id, "api_hash": self.api_hash, "session": self.client.session.save(),
                "user_id": me.id, "name": " ".join(x for x in (me.first_name, me.last_name) if x), "username": me.username,
                "rooms_made": 0}
        await self.client.disconnect()
        return data


class PasswordNeeded(Exception):
    pass


class Helper:
    """봇 프로세스에서 쓰는 도우미 연결 (필요할 때 연결, 세션이 바뀌면 다시)."""

    def __init__(self):
        self.client: TelegramClient | None = None
        self.session = ""

    async def _client(self, data: dict) -> TelegramClient:
        if self.client and self.session == data["session"] and self.client.is_connected():
            return self.client
        if self.client:
            await self.client.disconnect()
        self.client = TelegramClient(StringSession(data["session"]), int(data["api_id"]), data["api_hash"])
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise RuntimeError("도우미 계정 로그인이 풀렸어요. 관리 페이지에서 다시 연결해주세요.")
        self.session = data["session"]
        return self.client

    async def create_room(self, data: dict, title: str, bot_username: str, admin_ids: list[int]) -> int:
        """새 거래방을 만들고 봇을 관리자로 → 봇 API 용 chat_id (-100…)."""
        cl = await self._client(data)
        r = await cl(CreateChannelRequest(title=title[:128], about="거래 당사자 두 분과 중개봇만 있는 방이에요.", megagroup=True))
        ch = r.chats[0]
        await cl(TogglePreHistoryHiddenRequest(ch, True))
        await cl(InviteToChannelRequest(ch, [bot_username]))
        rights = ChatAdminRights(invite_users=True, ban_users=True, delete_messages=True, pin_messages=True, change_info=False,
                                 add_admins=False, manage_call=False, other=True)
        await cl(EditAdminRequest(ch, bot_username, rights, rank="중개봇"))
        for a in admin_ids:          # 운영 관리자도 방에 (상대 개인정보 설정에 따라 안 될 수 있음 — 그땐 도우미 계정이 관리자 역할)
            try:
                await cl(InviteToChannelRequest(ch, [a]))
            except Exception as e:   # noqa: BLE001
                log.info("관리자 %s 초대 안 됨: %r", a, e)
        return int(f"-100{ch.id}")

    async def close(self):
        if self.client:
            await self.client.disconnect()
