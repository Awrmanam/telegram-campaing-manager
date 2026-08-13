import asyncio
import getpass
import re

from sqlalchemy import or_, select
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from app.config import get_settings
from app.database.models import SenderAccount
from app.database.session import create_database, initialize_database


async def run() -> None:
    settings = get_settings()
    if not settings.telegram_api_id or not settings.telegram_api_hash:
        raise SystemExit("TELEGRAM_API_ID and TELEGRAM_API_HASH are required")
    settings.prepare_directories()
    label = input("Local account label: ").strip()
    if not re.fullmatch(r"[a-zA-Z0-9_-]{2,80}", label):
        raise SystemExit("Label must contain only letters, numbers, _ or -")
    engine, sessions = create_database(settings.database_url)
    await initialize_database(engine)
    async with sessions() as session:
        if await session.scalar(select(SenderAccount).where(SenderAccount.label == label)):
            raise SystemExit("This account label already exists")
    phone = getpass.getpass("Phone number (hidden): ")
    client = TelegramClient(str(settings.session_directory / label), settings.telegram_api_id, settings.telegram_api_hash)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            sent = await client.send_code_request(phone)
            code = getpass.getpass("Telegram login code (hidden): ")
            try:
                await client.sign_in(phone, code, phone_code_hash=sent.phone_code_hash)
            except SessionPasswordNeededError:
                await client.sign_in(password=getpass.getpass("Two-step verification password (hidden): "))
        me = await client.get_me()
        async with sessions() as session:
            duplicate = await session.scalar(select(SenderAccount).where(or_(SenderAccount.telegram_user_id == me.id, SenderAccount.session_name == label)))
            if duplicate:
                raise SystemExit("This Telegram account is already registered")
            session.add(SenderAccount(label=label, telegram_user_id=me.id, username=me.username,
                display_name=" ".join(filter(None, [me.first_name, me.last_name])), session_name=label,
                connection_status="CONNECTED"))
            await session.commit()
        print(f"Account '{label}' registered successfully (Telegram user ID: {me.id}).")
    finally:
        phone = ""  # discard sensitive input as early as practical
        await client.disconnect()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
