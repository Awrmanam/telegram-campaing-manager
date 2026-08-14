import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Campaign, CampaignMessage, SenderAccount, TargetChat


class CampaignValidationError(ValueError):
    """A campaign is not safe to activate or execute."""


async def validate_campaign_ready(session: AsyncSession, campaign: Campaign) -> None:
    account = await session.get(SenderAccount, campaign.sender_account_id)
    if not account or not account.enabled:
        raise CampaignValidationError("حساب ارسال‌کننده فعال و معتبر نیست.")
    enabled_message = await session.scalar(
        select(CampaignMessage.id)
        .where(CampaignMessage.campaign_id == campaign.id, CampaignMessage.enabled.is_(True))
        .limit(1)
    )
    if enabled_message is None:
        raise CampaignValidationError("کمپین حداقل به یک پیام فعال نیاز دارد.")
    await session.refresh(campaign, ["targets"])
    if not any(
        target.enabled and target.sender_account_id == campaign.sender_account_id
        for target in campaign.targets
    ):
        raise CampaignValidationError("کمپین حداقل به یک گروه مقصد فعال نیاز دارد.")


async def activate_campaign(
    session: AsyncSession, campaign: Campaign, now: datetime | None = None
) -> None:
    await validate_campaign_ready(session, campaign)
    campaign.enabled = True
    campaign.next_run_at = campaign.next_run_at or calculate_next_run(
        now or datetime.now(timezone.utc), campaign.interval_seconds
    )


async def normalize_message_positions(session: AsyncSession, campaign_id: int) -> None:
    messages = list(
        (
            await session.scalars(
                select(CampaignMessage)
                .where(CampaignMessage.campaign_id == campaign_id)
                .order_by(CampaignMessage.position, CampaignMessage.id)
            )
        ).all()
    )
    # Move through temporary unique positions before assigning the contiguous sequence.
    for offset, message in enumerate(messages, start=1):
        message.position = -offset
    await session.flush()
    for position, message in enumerate(messages, start=1):
        message.position = position


async def move_campaign_message(
    session: AsyncSession, message: CampaignMessage, delta: int
) -> bool:
    if delta not in {-1, 1}:
        raise ValueError("delta must be -1 or 1")
    other = await session.scalar(
        select(CampaignMessage).where(
            CampaignMessage.campaign_id == message.campaign_id,
            CampaignMessage.position == message.position + delta,
        )
    )
    if other is None:
        return False
    old_position = message.position
    message.position = -1
    await session.flush()
    other.position = old_position
    await session.flush()
    message.position = old_position + delta
    return True


async def pause_if_no_enabled_targets(session: AsyncSession, campaign: Campaign) -> bool:
    await session.refresh(campaign, ["targets"])
    has_target = any(
        target.enabled and target.sender_account_id == campaign.sender_account_id
        for target in campaign.targets
    )
    if campaign.enabled and not has_target:
        campaign.enabled = False
        campaign.next_run_at = None
        return True
    return False


def parse_interval(value: str) -> int:
    match = re.fullmatch(
        r"\s*(\d+)\s*(m|min|minute|minutes|دقیقه|h|hr|hour|hours|ساعت)\s*", value.lower()
    )
    if not match:
        raise ValueError("فاصله را مانند 30m یا 2h وارد کنید")
    amount = int(match.group(1))
    seconds = amount * (3600 if match.group(2) in {"h", "hr", "hour", "hours", "ساعت"} else 60)
    if seconds < 300 or seconds > 30 * 86400:
        raise ValueError("فاصله باید بین ۵ دقیقه و ۳۰ روز باشد")
    return seconds


def calculate_next_run(
    base: datetime, interval_seconds: int, now: datetime | None = None
) -> datetime:
    if interval_seconds <= 0:
        raise ValueError("interval must be positive")
    base = base.astimezone(timezone.utc)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    candidate = base + timedelta(seconds=interval_seconds)
    if candidate > now:
        return candidate
    missed = int((now - base).total_seconds() // interval_seconds) + 1
    return base + timedelta(seconds=missed * interval_seconds)


async def validate_targets(
    session: AsyncSession, sender_id: int, target_ids: set[int]
) -> list[TargetChat]:
    targets = list(
        (await session.scalars(select(TargetChat).where(TargetChat.id.in_(target_ids)))).all()
    )
    if len(targets) != len(target_ids) or any(
        target.sender_account_id != sender_id for target in targets
    ):
        raise ValueError("همه گروه‌ها باید به حساب انتخاب‌شده تعلق داشته باشند")
    return targets


async def eligible_campaign(session: AsyncSession, campaign_id: int) -> Campaign | None:
    campaign = await session.get(Campaign, campaign_id)
    if not campaign or not campaign.enabled:
        return None
    await session.refresh(campaign, ["targets"])
    return (
        campaign
        if all(
            target.sender_account_id == campaign.sender_account_id for target in campaign.targets
        )
        else None
    )
