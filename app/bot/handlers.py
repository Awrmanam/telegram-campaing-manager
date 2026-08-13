from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import Message
from sqlalchemy import func, select

from app.bot.keyboards import main_menu
from app.database.models import Campaign, DeliveryLog, DeliveryStatus, SenderAccount, TargetChat


def build_router(sessions) -> Router:  # type: ignore[no-untyped-def]
    router = Router()

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        await message.answer("به سامانه مدیریت کمپین خوش آمدید.", reply_markup=main_menu())

    @router.message(F.text == "👤 حساب‌های ارسال")
    async def accounts(message: Message) -> None:
        async with sessions() as session:
            rows = list((await session.scalars(select(SenderAccount).order_by(SenderAccount.label))).all())
        body = "\n".join(f"{'🟢' if x.enabled else '🔴'} {x.label} — {x.connection_status}" for x in rows)
        await message.answer("👤 حساب‌های ارسال\n\n" + (body or "حسابی ثبت نشده است.") +
            "\n\nافزودن امن حساب فقط با CLI:\n<code>python -m app.cli.login_account</code>", parse_mode="HTML")

    @router.message(F.text == "📢 کمپین‌ها")
    async def campaigns(message: Message) -> None:
        async with sessions() as session:
            rows = list((await session.scalars(select(Campaign).order_by(Campaign.id.desc()))).all())
        body = "\n".join(f"{'🟢' if x.enabled else '⏸'} #{x.id} {x.name}" for x in rows)
        await message.answer("📢 کمپین‌ها\n\n" + (body or "کمپینی وجود ندارد."))

    @router.message(F.text == "👥 گروه‌ها")
    async def chats(message: Message) -> None:
        async with sessions() as session:
            rows = list((await session.scalars(select(TargetChat).order_by(TargetChat.id.desc()).limit(50))).all())
        body = "\n".join(f"{'✅' if x.enabled else '❌'} {x.title} (<code>{x.telegram_chat_id}</code>)" for x in rows)
        await message.answer("👥 گروه‌های مجاز\n\n" + (body or "گروهی ثبت نشده است.") +
            "\n\nبرای مشاهده گفتگوهای حساب از <code>python -m app.cli.list_dialogs --account LABEL</code> استفاده کنید.", parse_mode="HTML")

    @router.message(F.text == "📊 گزارش‌ها")
    async def reports(message: Message) -> None:
        async with sessions() as session:
            success = await session.scalar(select(func.count()).select_from(DeliveryLog).where(DeliveryLog.status == DeliveryStatus.SUCCESS))
            failed = await session.scalar(select(func.count()).select_from(DeliveryLog).where(DeliveryLog.status == DeliveryStatus.FAILED))
        await message.answer(f"📊 گزارش کلی\n\n✅ موفق: {success}\n❌ ناموفق: {failed}")

    @router.message(F.text.in_({"➕ کمپین جدید", "📨 ارسال آزمایشی"}))
    async def guided(message: Message) -> None:
        await message.answer("این عملیات حساس به انتخاب دقیق حساب و گروه مجاز است. در نسخه فعلی از سرویس/پایگاه‌داده انجام می‌شود؛ راهنمای README را ببینید.")

    @router.message()
    async def fallback(message: Message) -> None:
        await message.answer("گزینه‌ای از منو انتخاب کنید.", reply_markup=main_menu())
    return router
