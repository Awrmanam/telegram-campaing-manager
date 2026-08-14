from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from sqlalchemy import select

from app.campaigns.service import calculate_next_run
from app.database.models import Campaign


class CampaignScheduler:
    def __init__(self, sessions, delivery, timezone_name: str, catch_up: bool = True):
        self.sessions, self.delivery, self.catch_up = sessions, delivery, catch_up
        self.scheduler = AsyncIOScheduler(timezone=timezone_name)

    async def restore(self) -> None:
        now = datetime.now(timezone.utc)
        async with self.sessions() as session:
            campaigns = list(
                (await session.scalars(select(Campaign).where(Campaign.enabled.is_(True)))).all()
            )
            for campaign in campaigns:
                due = campaign.next_run_at or now
                if due.tzinfo is None:
                    due = due.replace(tzinfo=timezone.utc)
                if due <= now and not self.catch_up:
                    due = calculate_next_run(due, campaign.interval_seconds, now)
                    campaign.next_run_at = due
                self.schedule(campaign.id, max(due, now))
            await session.commit()

    def schedule(self, campaign_id: int, when: datetime) -> None:
        self.scheduler.add_job(
            self._run,
            DateTrigger(run_date=when),
            args=[campaign_id],
            id=f"campaign-{campaign_id}",
            replace_existing=True,
            misfire_grace_time=300,
        )

    async def _run(self, campaign_id: int) -> None:
        await self.delivery.execute(campaign_id)
        async with self.sessions() as session:
            campaign = await session.get(Campaign, campaign_id)
            if campaign and campaign.enabled and campaign.next_run_at:
                self.schedule(campaign.id, campaign.next_run_at)

    async def start(self) -> None:
        await self.restore()
        self.scheduler.start()

    def stop(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
