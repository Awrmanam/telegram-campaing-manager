"""Offline regression tests for forum detection, selection and safe topic delivery."""
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from telethon import functions, types

from app.bot.handlers import build_router
from app.campaigns.service import CampaignValidationError, validate_campaign_ready
from app.database.models import (
    Base,
    Campaign,
    CampaignMessage,
    CampaignTarget,
    MessageType,
    SenderAccount,
    TargetChat,
)
from app.database.session import initialize_database
from app.services.delivery_service import DeliveryService
from app.telegram.topics import list_forum_topics, select_forum_topic
from app.telegram.transport import forward_original


@pytest.fixture
async def sessions():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def test_forum_selection_routes_registered():
    names = {
        handler.callback.__name__
        for observer in build_router(None).observers.values()
        for handler in observer.handlers
    }
    assert {
        "wizard_topic_pick", "test_topic_pick", "campaign_topic_save",
        "campaign_topic_chooser", "groups_topic_list",
    } <= names


@pytest.mark.asyncio
async def test_forum_topics_pagination_filters_closed():
    class Client:
        async def iter_dialogs(self):
            yield SimpleNamespace(id=-10042, input_entity="forum-peer")

        async def __call__(self, request):
            assert isinstance(request, functions.messages.GetForumTopicsRequest)
            assert request.peer == "forum-peer"
            if request.offset_topic == 0:
                return SimpleNamespace(
                    topics=[
                        SimpleNamespace(
                            id=1, title="General", date=123, top_message=1,
                            closed=False, hidden=False,
                        ),
                        SimpleNamespace(
                            id=20, title="Closed", date=122, top_message=21,
                            closed=True, hidden=False,
                        ),
                    ]
                )
            return SimpleNamespace(
                topics=[SimpleNamespace(
                    id=100, title="Offers", date=121, top_message=101,
                    closed=False, hidden=False,
                )] if request.offset_topic == 20 else []
            )

    client = Client()
    assert [(x.id, x.title) for x in await list_forum_topics(client, -10042)] == [
        (1, "General"), (100, "Offers"),
    ]
    assert (await select_forum_topic(client, -10042, 100)).title == "Offers"
    with pytest.raises(ValueError):
        await select_forum_topic(client, -10042, 20)


async def forum_campaign(sessions, *, selected_topic=None):
    async with sessions() as session:
        account = SenderAccount(
            label="forum", telegram_user_id=1234,
            display_name="Forum Test", session_name="forum", enabled=True,
        )
        session.add(account)
        await session.flush()
        target = TargetChat(
            sender_account_id=account.id, telegram_chat_id=-10042,
            title="Topic group", chat_type="group", is_forum=True, enabled=True,
        )
        campaign = Campaign(
            name="C", sender_account_id=account.id, enabled=True,
            interval_seconds=3600, targets=[target],
        )
        campaign.messages.append(CampaignMessage(
            position=1, message_type=MessageType.TEXT, text="topic message",
        ))
        session.add(campaign)
        await session.flush()
        link = await session.get(CampaignTarget, {
            "campaign_id": campaign.id, "target_chat_id": target.id
        })
        link.topic_id = selected_topic
        link.topic_title = "Offers" if selected_topic is not None else None
        await session.commit()
        return campaign.id


@pytest.mark.asyncio
async def test_campaign_cannot_activate_forum_without_topic(sessions):
    campaign_id = await forum_campaign(sessions)
    async with sessions() as session:
        campaign = await session.get(Campaign, campaign_id)
        with pytest.raises(CampaignValidationError, match="تاپیک"):
            await validate_campaign_ready(session, campaign)


@pytest.mark.asyncio
async def test_delivery_goes_to_selected_topic_not_general(sessions):
    campaign_id = await forum_campaign(sessions, selected_topic=100)
    calls = []

    class Client:
        async def iter_dialogs(self):
            yield SimpleNamespace(id=-10042, input_entity="forum-peer")

        async def send_message(self, entity, text, **kwargs):
            calls.append((entity, text, kwargs.get("reply_to")))
            return SimpleNamespace(id=9001)

    class Clients:
        async def get(self, account):
            return Client()

    assert await DeliveryService(sessions, Clients(), 0, 0).execute(
        campaign_id, manual=True,
    )
    assert calls == [("forum-peer", "topic message", 100)]


@pytest.mark.asyncio
async def test_forward_to_topic_checks_actual_destination():
    calls = []

    class Client:
        async def iter_dialogs(self):
            yield SimpleNamespace(id=-10042, input_entity="forum-peer")

        async def get_input_entity(self, ref):
            return "source-peer"

        async def get_messages(self, peer, ids):
            if peer == "source-peer":
                return SimpleNamespace(id=50, grouped_id=None)
            return [
                SimpleNamespace(
                    id=700,
                    reply_to=SimpleNamespace(reply_to_top_id=100, reply_to_msg_id=100),
                )
            ]

        async def __call__(self, request):
            calls.append(request)
            return SimpleNamespace(
                updates=[types.UpdateMessageID(id=700, random_id=123)]
            )

    assert await forward_original(
        Client(), -10042, "@channel", 50, topic_id=100,
    ) == 700
    request = calls[0]
    assert isinstance(request, functions.messages.ForwardMessagesRequest)
    assert request.top_msg_id == 100
    assert request.to_peer == "forum-peer"


@pytest.mark.asyncio
async def test_additive_sqlite_migration_preserves_existing_target_rows():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.exec_driver_sql(
            "CREATE TABLE target_chats (id INTEGER PRIMARY KEY, "
            "sender_account_id INTEGER, telegram_chat_id INTEGER, title TEXT, "
            "username TEXT, chat_type TEXT, enabled BOOLEAN, created_at DATETIME, "
            "last_verified_at DATETIME)"
        )
        await connection.exec_driver_sql(
            "INSERT INTO target_chats (id, telegram_chat_id, title) "
            "VALUES (5, -10042, 'Existing')"
        )
    await initialize_database(engine)
    async with engine.connect() as connection:
        columns = (await connection.exec_driver_sql(
            "PRAGMA table_info(target_chats)"
        )).all()
        assert "is_forum" in [row[1] for row in columns]
        row = (await connection.exec_driver_sql(
            "SELECT title, is_forum FROM target_chats WHERE id=5"
        )).one()
        assert row == ("Existing", 0)
    await engine.dispose()
