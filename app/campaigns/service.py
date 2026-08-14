import re
from datetime import UTC, datetime, timedelta

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
    base = now or datetime.now(UTC)
    campaign.next_run_at = calculate_next_run(base, campaign.interval_seconds, base)


def pause_campaign(campaign: Campaign) -> None:
    campaign.enabled = False
    campaign.next_run_at = None


async def pause_sender_campaigns(
    session: AsyncSession, sender_account_id: int
) -> list[Campaign]:
    campaigns = list(
        (
            await session.scalars(
                select(Campaign).where(
                    Campaign.sender_account_id == sender_account_id,
                    Campaign.enabled.is_(True),
                )
            )
        ).all()
    )
    for campaign in campaigns:
        pause_campaign(campaign)
    return campaigns


async def disable_sender_account(
    session: AsyncSession, account: SenderAccount, scheduler=None
) -> list[Campaign]:
    account.enabled = False
    campaigns = await pause_sender_campaigns(session, account.id)
    if scheduler:
        for campaign in campaigns:
            scheduler.cancel(campaign.id)
    return campaigns


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
        pause_campaign(campaign)
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
    base = base.astimezone(UTC)
    now = (now or datetime.now(UTC)).astimezone(UTC)
    candidate = base + timedelta(seconds=interval_seconds)
    if candidate > now:
        return candidate
    missed = int((now - base).total_seconds() // interval_seconds) + 1
    return base + timedelta(seconds=missed * interval_seconds)


def retry_wait_seconds(retry_at: datetime | None, now: datetime | None = None) -> int:
    if retry_at is None:
        return 0
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    remaining = (retry_at - (now or datetime.now(UTC))).total_seconds()
    return max(0, int(remaining + 0.999))


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
