"""Regression tests for source album forwarding and independent group schedules."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.campaigns.per_target import active_links, aware
from app.campaigns.service import activate_campaign
from app.database.models import (
    Base, Campaign, CampaignMessage, MessageType, SenderAccount, TargetChat,
)
from app.telegram.transport import forward_original, resolve_peer


@pytest.mark.asyncio
async def test_forward_entire_album_and_preserve_source():
    group = SimpleNamespace(id=-10042, input_entity="input-target")
    messages = [
        SimpleNamespace(id=i, grouped_id=123) for i in (50, 51, 52)
    ]
    seen = {}

    class Client:
        async def iter_dialogs(self):
            yield group

        async def get_input_entity(self, reference):
            assert reference == "@source"
            return "input-source"

        async def get_messages(self, source, ids):
            assert source == "input-source"
            if isinstance(ids, int):
                return messages[1]
            return [m if m.id in ids else None for m in messages]

        async def forward_messages(self, destination, ids, from_peer):
            seen.update(destination=destination, ids=ids, from_peer=from_peer)
            return [SimpleNamespace(id=111)]

    client = Client()
    assert await resolve_peer(client, -10042) == "input-target"
    assert await forward_original(client, -10042, "@source", 51) == 111
    assert seen == {
        "destination": "input-target",
        "ids": [50, 51, 52],
        "from_peer": "input-source",
    }


@pytest.mark.asyncio
async def test_group_intervals_create_independent_due_dates():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 11, 23, 12, tzinfo=UTC)
    async with sessions() as session:
        account = SenderAccount(
            label="test", telegram_user_id=111, display_name="Test",
            session_name="test", enabled=True,
        )
        session.add(account)
        await session.flush()
        t1 = TargetChat(
            sender_account_id=account.id, telegram_chat_id=-1, title="A",
            chat_type="group", enabled=True,
        )
        t2 = TargetChat(
            sender_account_id=account.id, telegram_chat_id=-2, title="B",
            chat_type="group", enabled=True,
        )
        campaign = Campaign(
            name="C", sender_account_id=account.id, interval_seconds=3600,
            targets=[t1, t2],
        )
        campaign.messages.append(CampaignMessage(
            position=1, message_type=MessageType.TEXT, text="hello",
        ))
        session.add(campaign)
        await session.flush()
        links = list((await session.scalars(select(
            __import__("app.database.models", fromlist=["CampaignTarget"]).CampaignTarget
        ))).all())
        assert len(links) == 2
        links[0].interval_seconds = 3600
        links[1].interval_seconds = 7200
        await activate_campaign(session, campaign, now)
        assert aware(campaign.next_run_at) == now + timedelta(hours=1)
        upcoming = sorted(
            (aware(link.next_run_at) for link, _ in await active_links(session, campaign))
        )
        assert upcoming == [now + timedelta(hours=1), now + timedelta(hours=2)]
        await session.commit()
    await engine.dispose()
