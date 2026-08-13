from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot.middlewares import is_admin
from app.campaigns.rotation import select_message
from app.campaigns.service import calculate_next_run, parse_interval, validate_targets
from app.database.models import Base, Campaign, CampaignMessage, MessageType, SenderAccount, TargetChat
from app.services.delivery_service import DeliveryService


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


def test_rotation_persists_index_and_wraps():
    messages = [CampaignMessage(id=i, campaign_id=1, position=i, message_type=MessageType.TEXT, text=str(i)) for i in range(1, 5)]
    selected, index = select_message(messages, 3)
    assert selected.text == "4" and index == 0
    assert select_message(messages, index)[0].text == "1"


def test_custom_intervals():
    assert parse_interval("30 دقیقه") == 1800
    assert parse_interval("2h") == 7200
    with pytest.raises(ValueError):
        parse_interval("2 seconds")


def test_next_run_skips_missed_intervals():
    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    now = base + timedelta(hours=3, minutes=10)
    assert calculate_next_run(base, 3600, now) == base + timedelta(hours=4)


async def seed(sessions, *, account_enabled=True, target_enabled=True):
    async with sessions() as session:
        account = SenderAccount(label="a", telegram_user_id=1, display_name="A", session_name="a", enabled=account_enabled)
        session.add(account)
        await session.flush()
        target = TargetChat(sender_account_id=account.id, telegram_chat_id=-1, title="T", chat_type="group", enabled=target_enabled)
        session.add(target)
        await session.flush()
        campaign = Campaign(name="C", sender_account_id=account.id, enabled=True, interval_seconds=3600, targets=[target])
        campaign.messages.append(CampaignMessage(position=1, message_type=MessageType.TEXT, text="hello"))
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
    assert await DeliveryService(sessions, clients, 0, 0).execute(campaign_id)
    assert clients.calls == 1


@pytest.mark.asyncio
async def test_duplicate_execution_claim(sessions):
    _, _, campaign_id = await seed(sessions)
    service = DeliveryService(sessions, FakeClients(), 0, 0)
    assert await service._claim(campaign_id)
    assert await service._claim(campaign_id) is None
