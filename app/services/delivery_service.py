import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker
from telethon.errors import (
    ChannelPrivateError,
    ChatWriteForbiddenError,
    FloodWaitError,
    RPCError,
    SlowModeWaitError,
    UserBannedInChannelError,
)

from app.campaigns.rotation import select_message
from app.campaigns.service import (
    CampaignValidationError,
    calculate_next_run,
    validate_campaign_ready,
)
from app.database.models import Campaign, DeliveryLog, DeliveryStatus, MessageType, SenderAccount
from app.telegram.client_manager import ClientManager

logger = logging.getLogger(__name__)


class DeliveryService:
    def __init__(
        self,
        sessions: async_sessionmaker,
        clients: ClientManager,
        min_delay: float = 3,
        max_delay: float = 8,
        alerts=None,
    ):
        self.sessions, self.clients = sessions, clients
        self.min_delay, self.max_delay = min_delay, max_delay
        self.alerts = alerts

    async def _claim(self, campaign_id: int, *, require_enabled: bool = True) -> str | None:
        token, stale = uuid4().hex, datetime.now(timezone.utc) - timedelta(hours=1)
        async with self.sessions() as session:
            conditions = [Campaign.id == campaign_id]
            if require_enabled:
                conditions.append(Campaign.enabled.is_(True))
            result = await session.execute(
                update(Campaign)
                .where(
                    *conditions,
                    (Campaign.execution_token.is_(None)) | (Campaign.execution_started_at < stale),
                )
                .values(execution_token=token, execution_started_at=datetime.now(timezone.utc))
            )
            await session.commit()
            return token if result.rowcount == 1 else None

    async def execute(self, campaign_id: int, *, manual: bool = False) -> bool:
        token = await self._claim(campaign_id, require_enabled=not manual)
        if not token:
            return False
        async with self.sessions() as session:
            campaign = await session.scalar(select(Campaign).where(Campaign.id == campaign_id))
            try:
                await validate_campaign_ready(session, campaign)
            except CampaignValidationError as exc:
                logger.warning("campaign_not_ready campaign_id=%s reason=%s", campaign_id, exc)
                campaign.execution_token = campaign.execution_started_at = None
                campaign.failure_count += 1
                await session.commit()
                if self.alerts:
                    await self.alerts.send(f"campaign:{campaign_id}:not_ready", str(exc))
                return False
            await session.refresh(campaign, ["messages", "targets"])
            account = await session.get(SenderAccount, campaign.sender_account_id)
            now = datetime.now(timezone.utc)
            if manual and campaign.manual_retry_at:
                retry_at = campaign.manual_retry_at
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                if retry_at > now:
                    campaign.execution_token = campaign.execution_started_at = None
                    await session.commit()
                    return False
            if not account:
                campaign.execution_token = campaign.execution_started_at = None
                await session.commit()
                return False
            message, next_index = select_message(campaign.messages, campaign.rotation_index)
            targets = [
                t for t in campaign.targets if t.enabled and t.sender_account_id == account.id
            ]
            targets.sort(key=lambda target: target.id)
            stored_cursor = campaign.manual_delivery_cursor if manual else campaign.delivery_cursor
            cursor = min(stored_cursor, len(targets))
            try:
                client = await self.clients.get(account)
            except (PermissionError, OSError, RPCError) as exc:
                if self.alerts:
                    await self.alerts.send(
                        f"sender:{account.id}:connection",
                        f"اتصال حساب {account.label} برای کمپین {campaign.name} برقرار نشد ({type(exc).__name__}).",
                    )
                await self._finish(session, campaign, token, failed=True, manual=manual)
                return False
            except Exception as exc:
                logger.exception("unexpected_sender_connection_error campaign_id=%s", campaign_id)
                if self.alerts:
                    await self.alerts.send(
                        f"sender:{account.id}:unexpected_connection",
                        f"خطای پیش‌بینی‌نشده اتصال حساب {account.label}: {type(exc).__name__}",
                    )
                campaign.execution_token = campaign.execution_started_at = None
                await session.commit()
                return False
            had_failure = False
            rate_limited_until = None
            remaining_targets = targets[cursor:]
            for position, target in enumerate(remaining_targets, start=cursor):
                log = DeliveryLog(
                    campaign_id=campaign.id,
                    campaign_message_id=message.id,
                    sender_account_id=account.id,
                    target_chat_id=target.id,
                    status=DeliveryStatus.FAILED,
                )
                session.add(log)
                try:
                    if message.message_type == MessageType.PHOTO:
                        sent = await client.send_file(
                            target.telegram_chat_id,
                            message.media_path,
                            caption=message.text,
                            parse_mode=message.parse_mode,
                        )
                    else:
                        sent = await client.send_message(
                            target.telegram_chat_id,
                            message.text or "",
                            parse_mode=message.parse_mode,
                        )
                    log.status, log.telegram_message_id = DeliveryStatus.SUCCESS, sent.id
                except (FloodWaitError, SlowModeWaitError) as exc:
                    log.status, log.error_type, log.error_message = (
                        DeliveryStatus.RETRY,
                        type(exc).__name__,
                        str(exc)[:1000],
                    )
                    had_failure = True
                    rate_limited_until = datetime.now(timezone.utc) + timedelta(seconds=exc.seconds)
                    if manual:
                        campaign.manual_delivery_cursor = position
                    else:
                        campaign.delivery_cursor = position
                    log.completed_at = datetime.now(timezone.utc)
                    await session.commit()
                    if self.alerts:
                        await self.alerts.send(
                            f"sender:{account.id}:rate_limit",
                            f"تلگرام برای حساب {account.label} توقف {exc.seconds} ثانیه‌ای اعلام کرد. کمپین زودتر از پایان آن اجرا نمی‌شود.",
                        )
                    break
                except (
                    ChatWriteForbiddenError,
                    UserBannedInChannelError,
                    ChannelPrivateError,
                    RPCError,
                    OSError,
                ) as exc:
                    log.error_type, log.error_message, had_failure = (
                        type(exc).__name__,
                        str(exc)[:1000],
                        True,
                    )
                log.completed_at = datetime.now(timezone.utc)
                await session.commit()
                if position + 1 < len(targets):
                    await asyncio.sleep(random.uniform(self.min_delay, self.max_delay))
            # A rate-limited run is incomplete: retain the message and schedule one clear retry.
            # Successful/ordinary-failure runs advance normally and never retry forever.
            if rate_limited_until:
                if manual:
                    campaign.manual_retry_at = rate_limited_until
                else:
                    campaign.next_run_at = rate_limited_until
                campaign.execution_token = campaign.execution_started_at = None
                campaign.failure_count += 1
                await session.commit()
                return False
            if had_failure and self.alerts:
                await self.alerts.send(
                    f"campaign:{campaign.id}:delivery_failures",
                    f"یک یا چند ارسال کمپین {campaign.name} ناموفق بود. گزارش تحویل را بررسی کنید.",
                )
            campaign.rotation_index = next_index
            if manual:
                campaign.manual_delivery_cursor = 0
                campaign.manual_retry_at = None
            else:
                campaign.delivery_cursor = 0
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
