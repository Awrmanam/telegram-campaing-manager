"""Construct campaigns with collections initialized before the first async flush.

SQLAlchemy async ORM must not implicitly lazy-load an uninitialized relationship.
Appending to campaign.messages after a flush of a previously empty relationship
can otherwise trigger MissingGreenlet in the Telegram callback handler.
"""

from app.database.models import Campaign, CampaignMessage, MessageType


def prepare_campaign(
    *,
    name: str,
    sender_account_id: int,
    interval_seconds: int,
    targets: list,
    message: dict,
) -> tuple[Campaign, CampaignMessage]:
    first_message = CampaignMessage(
        position=1,
        message_type=MessageType(message["message_type"]),
        text=message["text"],
        forward_chat_ref=message.get("forward_chat_ref"),
        forward_message_id=message.get("forward_message_id"),
    )
    campaign = Campaign(
        name=name,
        sender_account_id=sender_account_id,
        interval_seconds=interval_seconds,
        timezone="Asia/Tehran",
        enabled=False,
        targets=targets,
        messages=[first_message],
    )
    return campaign, first_message
