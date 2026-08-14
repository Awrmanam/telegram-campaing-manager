import asyncio
import logging

from aiogram import Bot, Dispatcher

from app.bot.handlers import build_router
from app.bot.middlewares import AdminMiddleware
from app.campaigns.scheduler import CampaignScheduler
from app.config import get_settings
from app.database.session import create_database, initialize_database
from app.services.alert_service import AlertService
from app.services.delivery_service import DeliveryService
from app.telegram.chat_service import ChatService
from app.telegram.client_manager import ClientManager


async def main() -> None:
    settings = get_settings()
    if not settings.bot_token:
        raise SystemExit("BOT_TOKEN is required")
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    settings.prepare_directories()
    engine, sessions = create_database(settings.database_url)
    await initialize_database(engine)
    clients = ClientManager(
        settings.telegram_api_id, settings.telegram_api_hash, settings.session_directory
    )
    bot, dispatcher = Bot(settings.bot_token), Dispatcher()
    alerts = AlertService(sessions, bot, settings.admin_ids, settings.alert_cooldown_seconds)
    delivery = DeliveryService(
        sessions, clients, settings.min_send_delay_seconds, settings.max_send_delay_seconds, alerts
    )
    scheduler = CampaignScheduler(
        sessions, delivery, settings.timezone, settings.catch_up_missed_runs
    )
    dispatcher.update.outer_middleware(AdminMiddleware(settings.admin_ids))
    dispatcher.include_router(
        build_router(
            sessions,
            chat_service=ChatService(clients),
            delivery=delivery,
            scheduler=scheduler,
            alerts=alerts,
            timezone_name=settings.timezone,
            media_directory=settings.media_directory,
        )
    )
    try:
        await scheduler.start()
        await dispatcher.start_polling(bot, handle_signals=True)
    finally:
        scheduler.stop()
        await clients.close()
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
