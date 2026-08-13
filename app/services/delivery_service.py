import asyncio
import random
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker
from telethon.errors import (ChannelPrivateError, ChatWriteForbiddenError, FloodWaitError,
                             RPCError, SlowModeWaitError, UserBannedInChannelError)

from app.campaigns.rotation import select_message
from app.campaigns.service import calculate_next_run
from app.database.models import Campaign, DeliveryLog, DeliveryStatus, MessageType, SenderAccount
from app.telegram.client_manager import ClientManager


class DeliveryService:
    def __init__(self, sessions: async_sessionmaker, clients: ClientManager, min_delay: float = 3, max_delay: float = 8):
        self.sessions, self.clients = sessions, clients
        self.min_delay, self.max_delay = min_delay, max_delay

    async def _claim(self, campaign_id: int) -> str | None:
        token, stale = uuid4().hex, datetime.now(timezone.utc) - timedelta(hours=1)
        async with self.sessions() as session:
            result = await session.execute(update(Campaign).where(Campaign.id == campaign_id, Campaign.enabled.is_(True),
                (Campaign.execution_token.is_(None)) | (Campaign.execution_started_at < stale)).values(
                execution_token=token, execution_started_at=datetime.now(timezone.utc)))
            await session.commit()
            return token if result.rowcount == 1 else None

    async def execute(self, campaign_id: int, *, manual: bool = False) -> bool:
        token = await self._claim(campaign_id)
        if not token:
            return False
        async with self.sessions() as session:
            campaign = await session.scalar(select(Campaign).where(Campaign.id == campaign_id))
            await session.refresh(campaign, ["messages", "targets"])
            account = await session.get(SenderAccount, campaign.sender_account_id)
            if not account or not account.enabled:
                await self._finish(session, campaign, token, failed=True, manual=manual)
                return False
            message, next_index = select_message(campaign.messages, campaign.rotation_index)
            targets = [t for t in campaign.targets if t.enabled and t.sender_account_id == account.id]
            try:
                client = await self.clients.get(account)
            except Exception:
                await self._finish(session, campaign, token, failed=True, manual=manual)
                return False
            had_failure = False
            for position, target in enumerate(targets):
                log = DeliveryLog(campaign_id=campaign.id, campaign_message_id=message.id,
                    sender_account_id=account.id, target_chat_id=target.id, status=DeliveryStatus.FAILED)
                session.add(log)
                try:
                    if message.message_type == MessageType.PHOTO:
                        sent = await client.send_file(target.telegram_chat_id, message.media_path, caption=message.text, parse_mode=message.parse_mode)
                    else:
                        sent = await client.send_message(target.telegram_chat_id, message.text or "", parse_mode=message.parse_mode)
                    log.status, log.telegram_message_id = DeliveryStatus.SUCCESS, sent.id
                except (FloodWaitError, SlowModeWaitError) as exc:
                    log.status, log.error_type, log.error_message = DeliveryStatus.RETRY, type(exc).__name__, str(exc)[:1000]
                    had_failure = True
                    await session.commit()
                    await asyncio.sleep(exc.seconds)  # exact Telegram-mandated wait; never switches account
                    break
                except (ChatWriteForbiddenError, UserBannedInChannelError, ChannelPrivateError, RPCError, OSError) as exc:
                    log.error_type, log.error_message, had_failure = type(exc).__name__, str(exc)[:1000], True
                log.completed_at = datetime.now(timezone.utc)
                await session.commit()
                if position + 1 < len(targets):
                    await asyncio.sleep(random.uniform(self.min_delay, self.max_delay))
            campaign.rotation_index = next_index
            await self._finish(session, campaign, token, failed=had_failure, manual=manual)
            return True

    async def _finish(self, session, campaign, token: str, *, failed: bool, manual: bool) -> None:
        now = datetime.now(timezone.utc)
        campaign.failure_count = campaign.failure_count + 1 if failed else 0
        campaign.last_run_at = now
        if not manual:
            campaign.next_run_at = calculate_next_run(now, campaign.interval_seconds)
        campaign.execution_token = campaign.execution_started_at = None
        await session.commit()
