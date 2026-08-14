from app.database.models import CampaignMessage


def select_message(
    messages: list[CampaignMessage], rotation_index: int
) -> tuple[CampaignMessage, int]:
    enabled = sorted(
        (message for message in messages if message.enabled), key=lambda item: item.position
    )
    if not enabled:
        raise ValueError("campaign has no enabled messages")
    index = rotation_index % len(enabled)
    return enabled[index], (index + 1) % len(enabled)
