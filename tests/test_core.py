from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from telethon.errors import FloodWaitError

from app.bot.handlers import build_router
from app.bot.middlewares import is_admin
from app.campaigns.rotation import select_message
from app.campaigns.scheduler import CampaignScheduler
from app.campaigns.service import (
    CampaignValidationError,
    activate_campaign,
    calculate_next_run,
    disable_sender_account,
    move_campaign_message,
    normalize_message_positions,
    parse_interval,
    pause_campaign,
    pause_if_no_enabled_targets,
    retry_wait_seconds,
    validate_campaign_ready,
    validate_targets,
)
from app.database.models import (
    Base,
    Campaign,
    CampaignMessage,
    MessageType,
    SenderAccount,
    TargetChat,
)
from app.services.alert_service import AlertService
from app.services.delivery_service import DeliveryService
from app.services.media_service import replace_message_content


@pytest.fixture
async def sessions():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def test_admin_authorization():
    assert is_admin(2, frozenset({1, 2}))
    assert not is_admin(3, frozenset({1, 2}))
    assert not is_admin(None, frozenset({1}))


def test_complete_ui_routes_are_registered():
    names = {
        handler.callback.__name__
        for observer in build_router(None).observers.values()
        for handler in observer.handlers
    }
    assert {
        "campaign_new",
        "wizard_create",
        "campaign_start",
        "campaign_pause",
        "message_add_save",
        "campaign_target_toggle",
        "register_confirm",
        "account_check",
        "test_send",
        "report_today",
    } <= names


def test_rotation_persists_index_and_wraps():
    messages = [
        CampaignMessage(
            id=i,
            campaign_id=1,
            position=i,
            message_type=MessageType.TEXT,
            text=str(i),
            enabled=True,
        )
        for i in range(1, 5)
    ]
    selected, index = select_message(messages, 3)
    assert selected.text == "4" and index == 0
    assert select_message(messages, index)[0].text == "1"


def test_custom_intervals():
    assert parse_interval("30 دقیقه") == 1800
    assert parse_interval("2h") == 7200
    with pytest.raises(ValueError):
        parse_interval("2 seconds")


def test_next_run_skips_missed_intervals():
    base = datetime(2025, 1, 1, tzinfo=UTC)
    now = base + timedelta(hours=3, minutes=10)
    assert calculate_next_run(base, 3600, now) == base + timedelta(hours=4)


def test_manual_retry_wait_reports_remaining_seconds():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert retry_wait_seconds(now + timedelta(seconds=61), now) == 61
    assert retry_wait_seconds(now - timedelta(seconds=1), now) == 0


async def seed(sessions, *, account_enabled=True, target_enabled=True):
    async with sessions() as session:
        account = SenderAccount(
            label="a",
            telegram_user_id=1,
            display_name="A",
            session_name="a",
            enabled=account_enabled,
        )
        session.add(account)
        await session.flush()
        target = TargetChat(
            sender_account_id=account.id,
            telegram_chat_id=-1,
            title="T",
            chat_type="group",
            enabled=target_enabled,
        )
        session.add(target)
        await session.flush()
        campaign = Campaign(
            name="C",
            sender_account_id=account.id,
            enabled=True,
            interval_seconds=3600,
            targets=[target],
        )
        campaign.messages.append(
            CampaignMessage(position=1, message_type=MessageType.TEXT, text="hello")
        )
        session.add(campaign)
        await session.commit()
        return account.id, target.id, campaign.id


@pytest.mark.asyncio
async def test_cross_account_target_validation(sessions):
    account_id, target_id, _ = await seed(sessions)
    async with sessions() as session:
        with pytest.raises(ValueError):
            await validate_targets(session, account_id + 999, {target_id})


class FakeClients:
    calls = 0

    async def get(self, account):
        self.calls += 1

        class Client:
            async def send_message(self, *args, **kwargs):
                class Sent:
                    id = 42

                return Sent()

        return Client()


class FloodClients:
    async def get(self, account):
        class Client:
            async def send_message(self, *args, **kwargs):
                raise FloodWaitError(request=None, capture=60)

        return Client()


class SecondTargetFloodClients:
    async def get(self, account):
        class Client:
            calls = 0

            async def send_message(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 2:
                    raise FloodWaitError(request=None, capture=60)

                class Sent:
                    id = 42

                return Sent()

        return Client()


@pytest.mark.asyncio
async def test_disabled_sender_excluded(sessions):
    _, _, campaign_id = await seed(sessions, account_enabled=False)
    clients = FakeClients()
    assert not await DeliveryService(sessions, clients, 0, 0).execute(campaign_id)
    assert clients.calls == 0


@pytest.mark.asyncio
async def test_disabled_group_excluded(sessions):
    _, _, campaign_id = await seed(sessions, target_enabled=False)
    clients = FakeClients()
    assert not await DeliveryService(sessions, clients, 0, 0).execute(campaign_id)
    assert clients.calls == 0


@pytest.mark.asyncio
async def test_zero_target_campaign_fails_without_rotation(sessions):
    _, _, campaign_id = await seed(sessions)
    async with sessions() as session:
        campaign = await session.get(Campaign, campaign_id)
        campaign.targets.clear()
        await session.commit()
    assert not await DeliveryService(sessions, FakeClients(), 0, 0).execute(campaign_id)
    async with sessions() as session:
        campaign = await session.get(Campaign, campaign_id)
        assert campaign.rotation_index == 0


@pytest.mark.asyncio
async def test_campaign_ready_validation(sessions):
    _, _, campaign_id = await seed(sessions, account_enabled=False)
    async with sessions() as session:
        campaign = await session.get(Campaign, campaign_id)
        with pytest.raises(CampaignValidationError):
            await validate_campaign_ready(session, campaign)


@pytest.mark.asyncio
async def test_campaign_start_validation_and_activation(sessions):
    _, _, campaign_id = await seed(sessions, target_enabled=False)
    async with sessions() as session:
        campaign = await session.get(Campaign, campaign_id)
        campaign.enabled = False
        with pytest.raises(CampaignValidationError):
            await activate_campaign(session, campaign)
        assert not campaign.enabled and campaign.next_run_at is None


@pytest.mark.asyncio
async def test_final_target_removal_pauses_active_campaign(sessions):
    _, _, campaign_id = await seed(sessions)
    async with sessions() as session:
        campaign = await session.get(Campaign, campaign_id)
        await session.refresh(campaign, ["targets"])
        campaign.targets.clear()
        await session.flush()
        assert await pause_if_no_enabled_targets(session, campaign)
        assert not campaign.enabled


@pytest.mark.asyncio
async def test_duplicate_execution_claim(sessions):
    _, _, campaign_id = await seed(sessions)
    service = DeliveryService(sessions, FakeClients(), 0, 0)
    assert await service._claim(campaign_id)
    assert await service._claim(campaign_id) is None


@pytest.mark.asyncio
async def test_manual_claim_allows_paused_campaign(sessions):
    _, _, campaign_id = await seed(sessions)
    async with sessions() as session:
        campaign = await session.get(Campaign, campaign_id)
        campaign.enabled = False
        await session.commit()
    service = DeliveryService(sessions, FakeClients(), 0, 0)
    assert await service._claim(campaign_id) is None
    assert await service._claim(campaign_id, require_enabled=False)


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, admin_id, message):
        self.sent.append((admin_id, message))


@pytest.mark.asyncio
async def test_alert_cooldown_is_persistent(sessions):
    bot = FakeBot()
    alerts = AlertService(sessions, bot, frozenset({10, 20}), 3600)
    assert await alerts.send("same-error", "first")
    assert not await alerts.send("same-error", "duplicate")
    assert len(bot.sent) == 2


@pytest.mark.asyncio
async def test_restart_scheduler_restores_future_job(sessions):
    _, _, campaign_id = await seed(sessions)
    future = datetime.now(UTC) + timedelta(hours=1)
    async with sessions() as session:
        campaign = await session.get(Campaign, campaign_id)
        campaign.next_run_at = future
        await session.commit()
    scheduler = CampaignScheduler(sessions, object(), "UTC", catch_up=True)
    await scheduler.start()
    try:
        job = scheduler.scheduler.get_job(f"campaign-{campaign_id}")
        assert job is not None
        assert abs((job.next_run_time - future).total_seconds()) < 1
    finally:
        scheduler.stop()


class DownloadBot:
    async def download(self, _file_id, destination):
        destination.write_bytes(b"new-photo")


@pytest.mark.asyncio
async def test_photo_to_text_clears_obsolete_media(tmp_path):
    old = tmp_path / "old.jpg"
    old.write_bytes(b"old")
    message = CampaignMessage(
        id=7,
        campaign_id=1,
        position=1,
        message_type=MessageType.PHOTO,
        text="caption",
        media_path=str(old),
    )
    await replace_message_content(
        message, {"message_type": "TEXT", "text": "text", "file_id": None}, DownloadBot(), tmp_path
    )
    assert message.message_type == MessageType.TEXT and message.media_path is None
    assert not old.exists()


@pytest.mark.asyncio
async def test_text_to_photo_and_photo_replacement(tmp_path):
    message = CampaignMessage(
        id=8, campaign_id=1, position=1, message_type=MessageType.TEXT, text="text"
    )
    replacement = {"message_type": "PHOTO", "text": "caption", "file_id": "file"}
    await replace_message_content(message, replacement, DownloadBot(), tmp_path)
    assert message.message_type == MessageType.PHOTO
    assert Path(message.media_path).read_bytes() == b"new-photo"
    Path(message.media_path).write_bytes(b"stale")
    await replace_message_content(message, replacement, DownloadBot(), tmp_path)
    assert Path(message.media_path).read_bytes() == b"new-photo"


@pytest.mark.asyncio
async def test_message_positions_normalized_after_delete(sessions):
    _, _, campaign_id = await seed(sessions)
    async with sessions() as session:
        session.add_all(
            [
                CampaignMessage(
                    campaign_id=campaign_id, position=2, message_type=MessageType.TEXT, text="2"
                ),
                CampaignMessage(
                    campaign_id=campaign_id, position=3, message_type=MessageType.TEXT, text="3"
                ),
            ]
        )
        await session.commit()
        middle = await session.scalar(
            select(CampaignMessage).where(
                CampaignMessage.campaign_id == campaign_id, CampaignMessage.position == 2
            )
        )
        await session.delete(middle)
        await session.flush()
        await normalize_message_positions(session, campaign_id)
        await session.commit()
        positions = list(
            (
                await session.scalars(
                    select(CampaignMessage.position)
                    .where(CampaignMessage.campaign_id == campaign_id)
                    .order_by(CampaignMessage.position)
                )
            ).all()
        )
        assert positions == [1, 2]


@pytest.mark.asyncio
async def test_message_reorder_keeps_contiguous_positions(sessions):
    _, _, campaign_id = await seed(sessions)
    async with sessions() as session:
        second = CampaignMessage(
            campaign_id=campaign_id,
            position=2,
            message_type=MessageType.TEXT,
            text="second",
        )
        session.add(second)
        await session.commit()
        assert await move_campaign_message(session, second, -1)
        await session.commit()
        messages = list(
            (
                await session.scalars(
                    select(CampaignMessage)
                    .where(CampaignMessage.campaign_id == campaign_id)
                    .order_by(CampaignMessage.position)
                )
            ).all()
        )
        assert [message.position for message in messages] == [1, 2]
        assert [message.text for message in messages] == ["second", "hello"]


@pytest.mark.asyncio
async def test_manual_flood_wait_preserves_recurring_schedule(sessions):
    _, _, campaign_id = await seed(sessions)
    recurring = datetime.now(UTC) + timedelta(hours=2)
    async with sessions() as session:
        campaign = await session.get(Campaign, campaign_id)
        campaign.next_run_at = recurring
        await session.commit()
    assert not await DeliveryService(sessions, FloodClients(), 0, 0).execute(
        campaign_id, manual=True
    )
    async with sessions() as session:
        campaign = await session.get(Campaign, campaign_id)
        next_run = campaign.next_run_at.replace(tzinfo=UTC)
        assert next_run == recurring
        assert campaign.manual_retry_at is not None
        assert campaign.manual_delivery_cursor == 0
        assert campaign.rotation_index == 0


@pytest.mark.asyncio
async def test_scheduled_flood_wait_retains_cursor_and_message(sessions):
    account_id, _, campaign_id = await seed(sessions)
    old_next = datetime.now(UTC)
    async with sessions() as session:
        campaign = await session.get(Campaign, campaign_id)
        second = TargetChat(
            sender_account_id=account_id,
            telegram_chat_id=-2,
            title="T2",
            chat_type="group",
            enabled=True,
        )
        session.add(second)
        campaign.targets.append(second)
        campaign.next_run_at = old_next
        await session.commit()
    assert not await DeliveryService(sessions, SecondTargetFloodClients(), 0, 0).execute(
        campaign_id
    )
    async with sessions() as session:
        campaign = await session.get(Campaign, campaign_id)
        next_run = campaign.next_run_at.replace(tzinfo=UTC)
        assert next_run > old_next
        assert campaign.delivery_cursor == 1
        assert campaign.rotation_index == 0


@pytest.mark.asyncio
async def test_pause_long_delay_resume_uses_fresh_schedule(sessions):
    _, _, campaign_id = await seed(sessions)
    paused_at = datetime(2026, 1, 1, tzinfo=UTC)
    resumed_at = paused_at + timedelta(days=10)
    async with sessions() as session:
        campaign = await session.get(Campaign, campaign_id)
        campaign.next_run_at = paused_at + timedelta(hours=1)
        pause_campaign(campaign)
        assert not campaign.enabled and campaign.next_run_at is None
        await activate_campaign(session, campaign, resumed_at)
        assert campaign.enabled
        assert campaign.next_run_at == resumed_at + timedelta(seconds=campaign.interval_seconds)


@pytest.mark.asyncio
async def test_active_interval_reschedule_replaces_job_time(sessions):
    _, _, campaign_id = await seed(sessions)
    scheduler = CampaignScheduler(sessions, object(), "UTC")
    scheduler.scheduler.start(paused=True)
    try:
        old_time = datetime.now(UTC) + timedelta(hours=1)
        new_time = datetime.now(UTC) + timedelta(hours=6)
        scheduler.schedule(campaign_id, old_time)
        scheduler.schedule(campaign_id, new_time)
        jobs = scheduler.scheduler.get_jobs()
        assert len([job for job in jobs if job.id == f"campaign-{campaign_id}"]) == 1
        assert abs((scheduler.get_scheduled_time(campaign_id) - new_time).total_seconds()) < 1
    finally:
        scheduler.stop()


@pytest.mark.asyncio
async def test_disabling_sender_pauses_campaigns_without_auto_restart(sessions):
    account_id, _, campaign_id = await seed(sessions)

    class SchedulerSpy:
        def __init__(self):
            self.canceled = []

        def cancel(self, value):
            self.canceled.append(value)

    scheduler = SchedulerSpy()
    async with sessions() as session:
        account = await session.get(SenderAccount, account_id)
        paused = await disable_sender_account(session, account, scheduler)
        assert [campaign.id for campaign in paused] == [campaign_id]
        assert scheduler.canceled == [campaign_id]
        await session.commit()
        campaign = await session.get(Campaign, campaign_id)
        assert not campaign.enabled and campaign.next_run_at is None
        account.enabled = True
        await session.commit()
        assert not campaign.enabled


@pytest.mark.asyncio
async def test_scheduler_invariants_for_pause_and_single_job(sessions):
    _, _, campaign_id = await seed(sessions)
    scheduler = CampaignScheduler(sessions, object(), "UTC")
    scheduler.scheduler.start(paused=True)
    try:
        first = datetime.now(UTC) + timedelta(hours=1)
        second = datetime.now(UTC) + timedelta(hours=2)
        scheduler.schedule(campaign_id, first)
        scheduler.schedule(campaign_id, second)
        assert len(scheduler.scheduler.get_jobs()) == 1
        assert scheduler.cancel(campaign_id)
        assert scheduler.get_scheduled_time(campaign_id) is None
        assert not scheduler.cancel(campaign_id)
    finally:
        scheduler.stop()
