import logging
from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.campaigns.service import calculate_next_run, pause_campaign
from app.database.models import Campaign

logger = logging.getLogger(__name__)


class CampaignScheduler:
    def __init__(self, sessions, delivery, timezone_name: str, catch_up: bool = True):
        self.sessions, self.delivery, self.catch_up = sessions, delivery, catch_up
        self.scheduler = AsyncIOScheduler(timezone=timezone_name)

    async def restore(self) -> None:
        now = datetime.now(UTC)
        async with self.sessions() as session:
            campaigns = list(
                (await session.scalars(select(Campaign).where(Campaign.enabled.is_(True)))).all()
            )
            for campaign in campaigns:
                due = campaign.next_run_at or now
                if due.tzinfo is None:
                    due = due.replace(tzinfo=UTC)
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

    def cancel(self, campaign_id: int) -> bool:
        job = self.scheduler.get_job(f"campaign-{campaign_id}")
        if job is None:
            return False
        self.scheduler.remove_job(job.id)
        return True

    def get_scheduled_time(self, campaign_id: int) -> datetime | None:
        job = self.scheduler.get_job(f"campaign-{campaign_id}")
        return job.next_run_time if job else None

    async def _run(self, campaign_id: int) -> None:
        try:
            await self.delivery.execute(campaign_id)
        except SQLAlchemyError:
            logger.exception("campaign_scheduler_database_error campaign_id=%s", campaign_id)
            async with self.sessions() as session:
                campaign = await session.get(Campaign, campaign_id)
                if campaign:
                    pause_campaign(campaign)
                    await session.commit()
            return
        async with self.sessions() as session:
            campaign = await session.get(Campaign, campaign_id)
            if campaign and campaign.enabled and campaign.next_run_at:
                self.schedule(campaign.id, campaign.next_run_at)

    async def start(self) -> None:
        self.scheduler.start(paused=True)
        await self.restore()
        self.scheduler.resume()

    def stop(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
