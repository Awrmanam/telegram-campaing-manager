"""Independent delivery intervals for each campaign/group association."""
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.database.models import CampaignTarget, TargetChat


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def active_links(session, campaign):
    result = await session.execute(
        select(CampaignTarget, TargetChat)
        .join(TargetChat, CampaignTarget.target_chat_id == TargetChat.id)
        .where(CampaignTarget.campaign_id == campaign.id)
    )
    return [
        (link, chat) for link, chat in result.all()
        if chat.enabled and chat.sender_account_id == campaign.sender_account_id
    ]


async def sync_next_run(
    session, campaign, *, reset: bool = False,
    now: datetime | None = None, legacy_due: datetime | None = None
):
    """Recompute the next campaign wake-up from each enabled target's own timer."""
    now = aware(now or datetime.now(UTC))
    links = await active_links(session, campaign)
    next_runs = []
    for link, _ in links:
        interval = link.interval_seconds or campaign.interval_seconds
        if reset:
            link.next_run_at = now + timedelta(seconds=interval)
        elif link.next_run_at is None:
            link.next_run_at = (
                aware(legacy_due) if legacy_due else now + timedelta(seconds=interval)
            )
        next_runs.append(aware(link.next_run_at))
    campaign.next_run_at = min(next_runs) if next_runs else None
    return campaign.next_run_at
