import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from sqlalchemy import select

from app.database.models import AdminAlert

logger = logging.getLogger(__name__)


class AlertService:
    def __init__(self, sessions, bot: Bot, admin_ids: frozenset[int], cooldown_seconds: int):
        self.sessions, self.bot, self.admin_ids = sessions, bot, admin_ids
        self.cooldown = timedelta(seconds=cooldown_seconds)

    async def send(self, key: str, message: str) -> bool:
        now = datetime.now(timezone.utc)
        async with self.sessions() as session:
            alert = await session.scalar(
                select(AdminAlert).where(AdminAlert.deduplication_key == key)
            )
            if alert:
                sent_at = alert.last_sent_at
                if sent_at.tzinfo is None:
                    sent_at = sent_at.replace(tzinfo=timezone.utc)
                alert.occurrence_count += 1
                if now - sent_at < self.cooldown:
                    await session.commit()
                    return False
                alert.last_sent_at, alert.message = now, message
            else:
                session.add(AdminAlert(deduplication_key=key, message=message, last_sent_at=now))
            await session.commit()
        for admin_id in self.admin_ids:
            try:
                await self.bot.send_message(admin_id, f"⚠️ هشدار عملیاتی\n\n{message}")
            except Exception as exc:
                logger.warning(
                    "admin_alert_delivery_failed admin_id=%s error=%s", admin_id, type(exc).__name__
                )
        return True
