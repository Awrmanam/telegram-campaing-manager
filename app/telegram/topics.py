"""Discover selectable forum topics through the connected user's MTProto account."""
from dataclasses import dataclass
from datetime import datetime

from telethon import functions

from app.telegram.transport import resolve_peer


@dataclass(frozen=True, slots=True)
class ForumTopic:
    id: int
    title: str


def _timestamp(date) -> int:
    if isinstance(date, datetime):
        return int(date.timestamp())
    return int(date or 0)


async def list_forum_topics(client, chat_id: int, *, max_topics: int = 500) -> list[ForumTopic]:
    """Fetch open/visible topics with API pagination; no auto-join or topic creation."""
    peer = await resolve_peer(client, chat_id)
    result: list[ForumTopic] = []
    seen: set[int] = set()
    offset = (0, 0, 0)
    for _ in range(20):
        response = await client(
            functions.messages.GetForumTopicsRequest(
                peer=peer, q="", offset_date=offset[0], offset_id=offset[1],
                offset_topic=offset[2], limit=min(100, max_topics - len(seen)),
            )
        )
        page = response.topics
        if not page:
            break
        for topic in page:
            ident = getattr(topic, "id", None)
            if ident is None or ident in seen:
                continue
            seen.add(ident)
            if getattr(topic, "closed", False) or getattr(topic, "hidden", False):
                continue
            result.append(ForumTopic(id=ident, title=getattr(topic, "title", None) or "عمومی"))
        last = page[-1]
        next_offset = (
            _timestamp(getattr(last, "date", 0)),
            int(getattr(last, "top_message", 0)),
            int(last.id),
        )
        if next_offset == offset or len(seen) >= max_topics:
            break
        offset = next_offset
    return result


async def select_forum_topic(client, chat_id: int, topic_id: int) -> ForumTopic:
    """Reject stale, hidden, closed or fabricated topic IDs."""
    topics = await list_forum_topics(client, chat_id)
    match = next((topic for topic in topics if topic.id == topic_id), None)
    if match is None:
        raise ValueError("تاپیک انتخاب‌شده بسته، حذف یا غیرقابل‌دسترسی است.")
    return match
