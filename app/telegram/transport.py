"""MTProto entity resolution and native forwarding of source posts/albums."""
from __future__ import annotations

from telethon import functions, types, utils
from telethon.errors import RPCError


async def resolve_peer(client, reference: str | int):
    """For numeric chat IDs, refresh from this account's own dialogs/access hashes.

    Telegram needs an InputPeer, not just the ID saved by the bot.
    Never join unknown groups or guess a peer.
    """
    if isinstance(reference, str) and reference.lstrip("-").isdigit():
        reference = int(reference)
    if isinstance(reference, int):
        # Lightweight delivery test doubles do not implement Telethon dialogs.
        if not hasattr(client, "iter_dialogs"):
            return reference
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
    client, destination: str | int, source: str | int, message_id: int,
    *, topic_id: int | None = None,
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
    if topic_id is not None and topic_id != 1:
        # Telethon's convenience forward_messages does not accept a forum topic.
        # Use the official MTProto top_msg_id, then verify actual destinations:
        # some Telegram servers route forwards into General despite top_msg_id.
        updates = await client(
            functions.messages.ForwardMessagesRequest(
                from_peer=src_peer,
                id=ids,
                random_id=[utils.generate_random_long() for _ in ids],
                to_peer=dest_peer,
                top_msg_id=topic_id,
            )
        )
        sent_ids = [
            update.id for update in getattr(updates, "updates", [])
            if isinstance(update, types.UpdateMessageID)
        ]
        if not sent_ids:
            raise ValueError("فوروارد ثبت شد ولی شناسه پیام ارسالی برای بررسی تاپیک دریافت نشد.")
        received = await client.get_messages(dest_peer, ids=sent_ids)
        received = received if isinstance(received, list) else [received]
        correct = len(received) == len(sent_ids) and all(
            msg and getattr(msg, "reply_to", None)
            and (
                getattr(msg.reply_to, "reply_to_top_id", None) == topic_id
                or getattr(msg.reply_to, "reply_to_msg_id", None) == topic_id
            )
            for msg in received
        )
        if not correct:
            # Fail closed: don't leave a silently misrouted post in General.
            try:
                await client.delete_messages(dest_peer, sent_ids)
            except Exception:
                pass
            raise ValueError(
                "تلگرام فوروارد را به تاپیک انتخابی هدایت نکرد؛ "
                "ارسال تأیید نشد. تاپیک را بررسی کنید."
            )
        return sent_ids[0]
    sent = await client.forward_messages(dest_peer, ids, from_peer=src_peer)
    if not sent:
        raise ValueError("تلگرام فوروارد را انجام نداد.")
    return sent[0].id if isinstance(sent, list) else sent.id
