"""Regression tests for the async SQLAlchemy campaign creation wizard."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.campaigns.create import prepare_campaign
from app.database.models import (
    Base,
    Campaign,
    CampaignTarget,
    MessageType,
    SenderAccount,
    TargetChat,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"message_type": "TEXT", "text": "hello"}, MessageType.TEXT),
        (
            {"message_type": "FORWARD", "text": "forwarded post",
             "forward_chat_ref": "@example", "forward_message_id": 123},
            MessageType.FORWARD,
        ),
        ({"message_type": "PHOTO", "text": "caption"}, MessageType.PHOTO),
    ],
)
async def test_campaign_creation_does_not_implicitly_lazy_load_messages(payload, expected):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        account = SenderAccount(
            label="test", telegram_user_id=123,
            display_name="Test", session_name="test", enabled=True,
        )
        session.add(account)
        await session.flush()
        target = TargetChat(
            sender_account_id=account.id,
            telegram_chat_id=-100123,
            title="Forum", chat_type="group", is_forum=True, enabled=True,
        )
        session.add(target)
        await session.flush()
        campaign, msg = prepare_campaign(
            name="Regression",
            sender_account_id=account.id,
            interval_seconds=7200,
            targets=[target],
            message=payload,
        )
        # This is the original failing sequence: first flush the campaign and
        # association row, then set the selected topic and optional media.
        session.add(campaign)
        await session.flush()
        association = await session.get(
            CampaignTarget,
            {"campaign_id": campaign.id, "target_chat_id": target.id},
        )
        association.topic_id = 200
        association.topic_title = "Ads"
        if expected == MessageType.PHOTO:
            msg.media_path = "/data/media/test.jpg"
        assert campaign.messages == [msg]  # no implicit database IO
        assert msg.id is not None
        await session.commit()
        campaign_id = campaign.id
    async with sessions() as session:
        loaded = await session.scalar(
            select(Campaign).where(Campaign.id == campaign_id)
        )
        assert len(loaded.messages) == 1
        assert loaded.messages[0].message_type == expected
        assert loaded.targets[0].id == target.id
        link = await session.get(
            CampaignTarget,
            {"campaign_id": campaign_id, "target_chat_id": target.id},
        )
        assert link.topic_id == 200
        if expected == MessageType.FORWARD:
            assert loaded.messages[0].forward_message_id == 123
        if expected == MessageType.PHOTO:
            assert loaded.messages[0].media_path == "/data/media/test.jpg"
    await engine.dispose()


def test_campaign_collections_initialized_before_database_flush():
    campaign, first = prepare_campaign(
        name="Local", sender_account_id=1,
        interval_seconds=3600, targets=[],
        message={"message_type": "TEXT", "text": "new"},
    )
    assert campaign.messages[0] is first
    assert first.message_type is MessageType.TEXT
