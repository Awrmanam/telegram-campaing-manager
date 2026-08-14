from pathlib import Path
from uuid import uuid4

from app.database.models import CampaignMessage, MessageType


def remove_managed_media(path: Path | None, media_directory: Path) -> None:
    if path is None:
        return
    root = media_directory.resolve()
    candidate = path.resolve()
    if candidate.parent == root:
        candidate.unlink(missing_ok=True)


async def replace_message_content(
    message: CampaignMessage, replacement: dict[str, str | None], bot, media_directory: Path
) -> None:
    """Replace message content atomically and remove media that is no longer referenced."""
    old_path = Path(message.media_path) if message.media_path else None
    message.text = replacement["text"]
    if replacement["message_type"] == "TEXT":
        message.message_type = MessageType.TEXT
        message.media_path = None
        remove_managed_media(old_path, media_directory)
        return

    media_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    final_path = media_directory / f"message_{message.id}.jpg"
    temporary = media_directory / f".{final_path.name}.{uuid4().hex}.tmp"
    try:
        await bot.download(replacement["file_id"], destination=temporary)
        temporary.replace(final_path)
    finally:
        temporary.unlink(missing_ok=True)
    if old_path and old_path.resolve() != final_path.resolve():
        remove_managed_media(old_path, media_directory)
    message.message_type = MessageType.PHOTO
    message.media_path = str(final_path)
