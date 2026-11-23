"""MTProto entity resolution and native forwarding of source posts/albums."""
from __future__ import annotations

from telethon.errors import RPCError


async def resolve_peer(client, reference: str | int):
    """For numeric chat IDs, refresh from this account's own dialogs/access hashes.

    Telegram needs an InputPeer, not just the ID saved by the bot.
    Never join unknown groups or guess a peer.
    """
    if isinstance(reference, str) and reference.lstrip("-").isdigit():
        reference = int(reference)
    if isinstance(reference, int):
        async for dialog in client.iter_dialogs():
            if dialog.id == reference:
                return dialog.input_entity
        raise ValueError("گروه یا کانال در فهرست گفتگوهای این اکانت پیدا نشد؛ عضویت و دسترسی را بررسی کنید.")
    if not isinstance(reference, str) or not reference:
        raise ValueError("منبع فوروارد معتبر نیست.")
    try:
        return await client.get_input_entity(reference)
    except (ValueError, RPCError) as exc:
        raise ValueError("اکانت به کانال مبدأ دسترسی ندارد یا لینک منبع معتبر نیست.") from exc


async def forward_original(
    client, destination: str | int, source: str | int, message_id: int
) -> int:
    """Forward an entire Telegram source album when the selected post is grouped.

    Native MTProto forwarding keeps Telegram's original captions and media without
    downloading/reuploading. Sources with protected forwarding are rejected by Telegram.
    """
    dest_peer = await resolve_peer(client, destination)
    src_peer = await resolve_peer(client, source)
    original = await client.get_messages(src_peer, ids=message_id)
    if not original or getattr(original, "id", None) is None:
        raise ValueError("پست مبدأ حذف شده یا برای این اکانت قابل مشاهده نیست.")
    ids = [original.id]
    if original.grouped_id:
        lo = max(1, original.id - 20)
        hi = original.id + 21
        nearby = await client.get_messages(src_peer, ids=list(range(lo, hi)))
        ids = sorted(
            msg.id for msg in nearby
            if msg and getattr(msg, "grouped_id", None) == original.grouped_id
        )
        if not ids:
            ids = [original.id]
    sent = await client.forward_messages(dest_peer, ids, from_peer=src_peer)
    if not sent:
        raise ValueError("تلگرام فوروارد را انجام نداد.")
    return sent[0].id if isinstance(sent, list) else sent.id
