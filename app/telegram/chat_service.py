from dataclasses import dataclass

from app.database.models import SenderAccount
from app.telegram.client_manager import ClientManager


@dataclass(frozen=True, slots=True)
class AccessibleChat:
    telegram_chat_id: int
    title: str
    username: str | None
    chat_type: str


class ChatService:
    def __init__(self, clients: ClientManager):
        self.clients = clients

    async def list_accessible(self, account: SenderAccount) -> list[AccessibleChat]:
        client = await self.clients.get(account)
        result: list[AccessibleChat] = []
        async for dialog in client.iter_dialogs():
            if dialog.is_group or dialog.is_channel:
                entity = dialog.entity
                result.append(
                    AccessibleChat(
                        dialog.id,
                        dialog.name or "بدون نام",
                        getattr(entity, "username", None),
                        "channel" if getattr(entity, "broadcast", False) else "group",
                    )
                )
        return result
