import argparse
import asyncio

from sqlalchemy import select

from app.config import get_settings
from app.database.models import SenderAccount
from app.database.session import create_database
from app.telegram.client_manager import ClientManager


async def run(label: str) -> None:
    settings = get_settings()
    engine, sessions = create_database(settings.database_url)
    manager = ClientManager(
        settings.telegram_api_id, settings.telegram_api_hash, settings.session_directory
    )
    try:
        async with sessions() as session:
            account = await session.scalar(
                select(SenderAccount).where(SenderAccount.label == label)
            )
            if not account:
                raise SystemExit("Account not found")
            client = await manager.get(account)
            async for dialog in client.iter_dialogs():
                if dialog.is_group or dialog.is_channel:
                    entity = dialog.entity
                    kind = "channel" if getattr(entity, "broadcast", False) else "group"
                    print(
                        f"{dialog.id}\t{dialog.name}\t@{getattr(entity, 'username', None) or '-'}\t{kind}"
                    )
    finally:
        await manager.close()
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="List groups already accessible to a sender; does not register them"
    )
    parser.add_argument("--account", required=True)
    asyncio.run(run(parser.parse_args().account))
