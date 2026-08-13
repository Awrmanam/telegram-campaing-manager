import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Campaign, TargetChat


def parse_interval(value: str) -> int:
    match = re.fullmatch(r"\s*(\d+)\s*(m|min|minute|minutes|دقیقه|h|hr|hour|hours|ساعت)\s*", value.lower())
    if not match:
        raise ValueError("فاصله را مانند 30m یا 2h وارد کنید")
    amount = int(match.group(1))
    seconds = amount * (3600 if match.group(2) in {"h", "hr", "hour", "hours", "ساعت"} else 60)
    if seconds < 300 or seconds > 30 * 86400:
        raise ValueError("فاصله باید بین ۵ دقیقه و ۳۰ روز باشد")
    return seconds


def calculate_next_run(base: datetime, interval_seconds: int, now: datetime | None = None) -> datetime:
    if interval_seconds <= 0:
        raise ValueError("interval must be positive")
    base = base.astimezone(timezone.utc)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    candidate = base + timedelta(seconds=interval_seconds)
    if candidate > now:
        return candidate
    missed = int((now - base).total_seconds() // interval_seconds) + 1
    return base + timedelta(seconds=missed * interval_seconds)


async def validate_targets(session: AsyncSession, sender_id: int, target_ids: set[int]) -> list[TargetChat]:
    targets = list((await session.scalars(select(TargetChat).where(TargetChat.id.in_(target_ids)))).all())
    if len(targets) != len(target_ids) or any(target.sender_account_id != sender_id for target in targets):
        raise ValueError("همه گروه‌ها باید به حساب انتخاب‌شده تعلق داشته باشند")
    return targets


async def eligible_campaign(session: AsyncSession, campaign_id: int) -> Campaign | None:
    campaign = await session.get(Campaign, campaign_id)
    if not campaign or not campaign.enabled:
        return None
    await session.refresh(campaign, ["targets"])
    return campaign if all(target.sender_account_id == campaign.sender_account_id for target in campaign.targets) else None
