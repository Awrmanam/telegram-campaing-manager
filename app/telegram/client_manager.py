import asyncio
from pathlib import Path

from telethon import TelegramClient

from app.database.models import SenderAccount


class ClientManager:
    def __init__(self, api_id: int, api_hash: str, directory: Path):
        self.api_id, self.api_hash, self.directory = api_id, api_hash, directory
        self._clients: dict[int, TelegramClient] = {}
        self._lock = asyncio.Lock()

    async def get(self, account: SenderAccount) -> TelegramClient:
        async with self._lock:
            client = self._clients.get(account.id)
            if client is None:
                client = TelegramClient(str(self.directory / account.session_name), self.api_id, self.api_hash)
                await client.connect()
                if not await client.is_user_authorized():
                    await client.disconnect()
                    raise PermissionError("sender account authorization has expired")
                self._clients[account.id] = client
            return client

    async def close(self) -> None:
        await asyncio.gather(*(client.disconnect() for client in self._clients.values()), return_exceptions=True)
        self._clients.clear()
