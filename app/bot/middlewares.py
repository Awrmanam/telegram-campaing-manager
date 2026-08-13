from aiogram import BaseMiddleware
from aiogram.types import TelegramObject


def is_admin(user_id: int | None, admin_ids: frozenset[int]) -> bool:
    return user_id is not None and user_id in admin_ids


class AdminMiddleware(BaseMiddleware):
    def __init__(self, admin_ids: frozenset[int]):
        self.admin_ids = admin_ids

    async def __call__(self, handler, event: TelegramObject, data: dict):  # type: ignore[no-untyped-def]
        user = data.get("event_from_user")
        if not is_admin(getattr(user, "id", None), self.admin_ids):
            if message := getattr(event, "message", event):
                if hasattr(message, "answer"):
                    await message.answer("⛔ دسترسی مجاز نیست.")
            return None
        return await handler(event, data)
