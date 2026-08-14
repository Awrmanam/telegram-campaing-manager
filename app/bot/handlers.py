from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ErrorEvent, Message
from sqlalchemy import delete, func, select
from sqlalchemy.orm import selectinload
from telethon.errors import RPCError

from app.bot.keyboards import confirm_keyboard, main_menu, rows_keyboard
from app.bot.states import CampaignWizard, TextInput
from app.campaigns.service import (
    CampaignValidationError,
    activate_campaign,
    calculate_next_run,
    disable_sender_account,
    move_campaign_message,
    normalize_message_positions,
    parse_interval,
    pause_campaign,
    pause_if_no_enabled_targets,
    retry_wait_seconds,
    validate_campaign_ready,
    validate_targets,
)
from app.database.models import (
    Campaign,
    CampaignMessage,
    CampaignTarget,
    DeliveryLog,
    DeliveryStatus,
    MessageType,
    SenderAccount,
    TargetChat,
)
from app.services.media_service import remove_managed_media, replace_message_content


def _interval(seconds: int) -> str:
    return f"{seconds // 3600} ساعت" if seconds % 3600 == 0 else f"{seconds // 60} دقیقه"


def _message_data(message: Message) -> dict[str, str | None]:
    if message.photo:
        return {
            "message_type": "PHOTO",
            "text": message.caption or "",
            "file_id": message.photo[-1].file_id,
        }
    if message.text:
        return {"message_type": "TEXT", "text": message.text, "file_id": None}
    raise ValueError("فقط متن یا تصویر همراه کپشن پذیرفته می‌شود.")


def build_router(
    sessions,
    *,
    chat_service=None,
    delivery=None,
    scheduler=None,
    alerts=None,
    timezone_name: str = "Asia/Tehran",
    media_directory: Path = Path("media"),
) -> Router:
    router = Router()
    display_zone = ZoneInfo(timezone_name)

    def display_time(value: datetime | None) -> str:
        if value is None:
            return "—"
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(display_zone).strftime("%Y-%m-%d %H:%M")

    @router.errors()
    async def operational_error(event: ErrorEvent) -> bool:
        if alerts:
            await alerts.send(
                f"bot:{type(event.exception).__name__}",
                f"خطای عملیاتی پنل: {type(event.exception).__name__}",
            )
        return True

    async def enabled_accounts():
        async with sessions() as session:
            return list(
                (
                    await session.scalars(
                        select(SenderAccount)
                        .where(SenderAccount.enabled.is_(True))
                        .order_by(SenderAccount.label)
                    )
                ).all()
            )

    async def show_campaign(call: CallbackQuery, campaign_id: int) -> None:
        async with sessions() as session:
            campaign = await session.scalar(
                select(Campaign)
                .where(Campaign.id == campaign_id)
                .options(selectinload(Campaign.messages), selectinload(Campaign.targets))
            )
            if not campaign:
                await call.answer("کمپین پیدا نشد", show_alert=True)
                return
            account = await session.get(SenderAccount, campaign.sender_account_id)
            success = await session.scalar(
                select(func.count())
                .select_from(DeliveryLog)
                .where(
                    DeliveryLog.campaign_id == campaign.id,
                    DeliveryLog.status == DeliveryStatus.SUCCESS,
                )
            )
            failed = await session.scalar(
                select(func.count())
                .select_from(DeliveryLog)
                .where(
                    DeliveryLog.campaign_id == campaign.id,
                    DeliveryLog.status == DeliveryStatus.FAILED,
                )
            )
        text = (
            f"📢 کمپین {campaign.name}\n\nوضعیت: {'🟢 فعال' if campaign.enabled else '⏸ متوقف'}\n"
            f"👤 حساب: {account.label}\n👥 گروه‌ها: {len(campaign.targets)}\n📝 پیام‌ها: {len(campaign.messages)}\n"
            f"⏱ فاصله: {_interval(campaign.interval_seconds)}\nآخرین اجرا: {display_time(campaign.last_run_at)}\n"
            f"اجرای بعدی: {display_time(campaign.next_run_at)}\n✅ موفق: {success} | ❌ ناموفق: {failed}"
        )
        keyboard = rows_keyboard(
            [
                ("▶️ شروع", f"camp:start:{campaign.id}"),
                ("⏸ توقف", f"camp:pause:{campaign.id}"),
                ("✏️ تغییر نام", f"camp:rename:{campaign.id}"),
                ("👤 تغییر حساب", f"camp:sender:{campaign.id}"),
                ("⏱ تغییر فاصله", f"camp:interval:{campaign.id}"),
                ("📝 پیام‌ها", f"msg:list:{campaign.id}"),
                ("👥 گروه‌ها", f"ct:list:{campaign.id}"),
                ("📨 ارسال الآن", f"camp:sendask:{campaign.id}"),
                ("🗑 حذف", f"camp:delask:{campaign.id}"),
            ],
            back="camp:list",
        )
        await call.message.edit_text(text, reply_markup=keyboard)

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("به سامانه مدیریت کمپین خوش آمدید.", reply_markup=main_menu())

    @router.callback_query(F.data == "cancel")
    async def cancel(call: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await call.message.edit_text("عملیات لغو شد.")
        await call.answer()

    # Campaign creation wizard
    @router.message(F.text == "➕ کمپین جدید")
    async def campaign_new(message: Message, state: FSMContext) -> None:
        await state.clear()
        await state.set_state(CampaignWizard.name)
        await message.answer("نام کمپین را وارد کنید:")

    @router.message(CampaignWizard.name)
    async def campaign_name(message: Message, state: FSMContext) -> None:
        name = (message.text or "").strip()
        if not 2 <= len(name) <= 150:
            await message.answer("نام باید بین ۲ تا ۱۵۰ نویسه باشد.")
            return
        await state.update_data(name=name)
        await state.set_state(CampaignWizard.sender)
        accounts = await enabled_accounts()
        await message.answer(
            "حساب ارسال‌کننده را انتخاب کنید:",
            reply_markup=rows_keyboard(
                [(x.label, f"wiz:sender:{x.id}") for x in accounts], back="cancel"
            ),
        )

    @router.callback_query(CampaignWizard.sender, F.data.startswith("wiz:sender:"))
    async def campaign_sender(call: CallbackQuery, state: FSMContext) -> None:
        sender_id = int(call.data.rsplit(":", 1)[1])
        await state.update_data(sender_id=sender_id)
        await state.set_state(CampaignWizard.interval)
        values = [
            ("۳۰ دقیقه", 1800),
            ("۱ ساعت", 3600),
            ("۲ ساعت", 7200),
            ("۳ ساعت", 10800),
            ("۶ ساعت", 21600),
            ("۱۲ ساعت", 43200),
            ("۲۴ ساعت", 86400),
        ]
        await call.message.edit_text(
            "فاصله ارسال را انتخاب کنید:",
            reply_markup=rows_keyboard(
                [(label, f"wiz:interval:{seconds}") for label, seconds in values]
                + [("مقدار دلخواه", "wiz:custom")],
                back="cancel",
            ),
        )

    @router.callback_query(CampaignWizard.interval, F.data == "wiz:custom")
    async def custom_prompt(call: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(CampaignWizard.custom_interval)
        await call.message.edit_text("فاصله را مانند 30m، 2h، ۳۰ دقیقه یا ۲ ساعت وارد کنید:")

    @router.message(CampaignWizard.custom_interval)
    async def custom_value(message: Message, state: FSMContext) -> None:
        try:
            seconds = parse_interval(message.text or "")
        except ValueError as exc:
            await message.answer(str(exc))
            return
        await choose_targets(message, state, seconds)

    @router.callback_query(CampaignWizard.interval, F.data.startswith("wiz:interval:"))
    async def fixed_interval(call: CallbackQuery, state: FSMContext) -> None:
        await choose_targets(call.message, state, int(call.data.rsplit(":", 1)[1]), edit=True)

    async def choose_targets(
        message: Message, state: FSMContext, seconds: int, edit: bool = False
    ) -> None:
        data = await state.get_data()
        sender_id = data["sender_id"]
        await state.update_data(interval_seconds=seconds, target_ids=[])
        await state.set_state(CampaignWizard.targets)
        async with sessions() as session:
            targets = list(
                (
                    await session.scalars(
                        select(TargetChat)
                        .where(
                            TargetChat.sender_account_id == sender_id, TargetChat.enabled.is_(True)
                        )
                        .order_by(TargetChat.title)
                    )
                ).all()
            )
        kb = rows_keyboard(
            [(f"⬜ {x.title}", f"wiz:target:{x.id}") for x in targets]
            + [("✅ ادامه", "wiz:targets_done")],
            back="cancel",
        )
        action = message.edit_text if edit else message.answer
        await action("گروه‌های مقصد را انتخاب کنید (انتخاب چندگانه):", reply_markup=kb)

    @router.callback_query(CampaignWizard.targets, F.data.startswith("wiz:target:"))
    async def toggle_target(call: CallbackQuery, state: FSMContext) -> None:
        target_id = int(call.data.rsplit(":", 1)[1])
        data = await state.get_data()
        selected = set(data.get("target_ids", []))
        selected.symmetric_difference_update({target_id})
        await state.update_data(target_ids=list(selected))
        for row in call.message.reply_markup.inline_keyboard:
            for button in row:
                if button.callback_data == call.data:
                    button.text = ("✅ " if target_id in selected else "⬜ ") + button.text[2:]
        await call.message.edit_reply_markup(reply_markup=call.message.reply_markup)
        await call.answer()

    @router.callback_query(CampaignWizard.targets, F.data == "wiz:targets_done")
    async def targets_done(call: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        if not data.get("target_ids"):
            await call.answer("حداقل یک گروه انتخاب کنید", show_alert=True)
            return
        await state.set_state(CampaignWizard.message)
        await call.message.edit_text("پیام اول کمپین را بفرستید (متن یا تصویر همراه کپشن HTML):")

    @router.message(CampaignWizard.message)
    async def wizard_message(message: Message, state: FSMContext) -> None:
        try:
            item = _message_data(message)
        except ValueError as exc:
            await message.answer(str(exc))
            return
        await state.update_data(message=item)
        data = await state.get_data()
        await state.set_state(CampaignWizard.preview)
        summary = (
            f"📢 نام کمپین: {data['name']}\n👤 شناسه حساب: {data['sender_id']}\n👥 تعداد گروه‌ها: {len(data['target_ids'])}\n"
            f"📝 تعداد پیام‌ها: ۱\n⏱ فاصله: {_interval(data['interval_seconds'])}\n\nپیش‌نمایش:"
        )
        if item["message_type"] == "PHOTO":
            await message.answer(summary)
            await message.answer_photo(
                item["file_id"],
                caption=item["text"] or None,
                parse_mode=None,
                reply_markup=confirm_keyboard("wiz:create"),
            )
        else:
            await message.answer(
                summary + "\n\n" + (item["text"] or ""),
                parse_mode=None,
                reply_markup=confirm_keyboard("wiz:create"),
            )

    @router.callback_query(CampaignWizard.preview, F.data == "wiz:create")
    async def wizard_create(call: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        item = data["message"]
        async with sessions() as session:
            targets = await validate_targets(session, data["sender_id"], set(data["target_ids"]))
            campaign = Campaign(
                name=data["name"],
                sender_account_id=data["sender_id"],
                interval_seconds=data["interval_seconds"],
                timezone="Asia/Tehran",
                enabled=False,
                targets=targets,
            )
            session.add(campaign)
            await session.flush()
            media_path = None
            if item["message_type"] == "PHOTO":
                media_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                media_path = str(media_directory / f"campaign_{campaign.id}_1.jpg")
                await call.bot.download(item["file_id"], destination=media_path)
            campaign.messages.append(
                CampaignMessage(
                    position=1,
                    message_type=MessageType(item["message_type"]),
                    text=item["text"],
                    media_path=media_path,
                )
            )
            await session.commit()
            campaign_id = campaign.id
        await state.clear()
        await call.message.edit_reply_markup()
        await call.message.answer(f"✅ کمپین #{campaign_id} ساخته شد و فعلاً متوقف است.")
        await call.answer()

    # Campaign management
    @router.message(F.text == "📢 کمپین‌ها")
    @router.callback_query(F.data == "camp:list")
    async def campaigns(event: Message | CallbackQuery) -> None:
        async with sessions() as session:
            rows = list(
                (await session.scalars(select(Campaign).order_by(Campaign.id.desc()))).all()
            )
        kb = rows_keyboard(
            [(f"{'🟢' if x.enabled else '⏸'} {x.name}", f"camp:view:{x.id}") for x in rows],
            back=None,
        )
        if isinstance(event, CallbackQuery):
            await event.message.edit_text("📢 کمپین‌ها", reply_markup=kb)
            await event.answer()
        else:
            await event.answer("📢 کمپین‌ها", reply_markup=kb)

    @router.callback_query(F.data.startswith("camp:view:"))
    async def campaign_view(call: CallbackQuery) -> None:
        await show_campaign(call, int(call.data.rsplit(":", 1)[1]))
        await call.answer()

    @router.callback_query(F.data.startswith("camp:start:"))
    async def campaign_start(call: CallbackQuery) -> None:
        cid = int(call.data.rsplit(":", 1)[1])
        async with sessions() as session:
            campaign = await session.get(Campaign, cid)
            try:
                await activate_campaign(session, campaign)
            except CampaignValidationError as exc:
                await call.answer(str(exc), show_alert=True)
                return
            await session.commit()
        if scheduler:
            scheduler.schedule(cid, campaign.next_run_at)
        await show_campaign(call, cid)
        await call.answer("فعال شد")

    @router.callback_query(F.data.startswith("camp:pause:"))
    async def campaign_pause(call: CallbackQuery) -> None:
        cid = int(call.data.rsplit(":", 1)[1])
        async with sessions() as session:
            campaign = await session.get(Campaign, cid)
            pause_campaign(campaign)
            await session.commit()
        if scheduler:
            scheduler.cancel(cid)
        await show_campaign(call, cid)
        await call.answer("متوقف شد")

    @router.callback_query(F.data.startswith("camp:rename:"))
    async def rename_prompt(call: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(TextInput.campaign_rename)
        await state.update_data(campaign_id=int(call.data.rsplit(":", 1)[1]))
        await call.message.answer("نام جدید را وارد کنید:")

    @router.message(TextInput.campaign_rename)
    async def rename_save(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        name = (message.text or "").strip()
        if not 2 <= len(name) <= 150:
            await message.answer("نام نامعتبر است.")
            return
        async with sessions() as session:
            campaign = await session.get(Campaign, data["campaign_id"])
            campaign.name = name
            await session.commit()
        await state.clear()
        await message.answer("✅ نام تغییر کرد.")

    @router.callback_query(F.data.startswith("camp:sender:"))
    async def sender_change_prompt(call: CallbackQuery) -> None:
        cid = int(call.data.rsplit(":", 1)[1])
        accounts = await enabled_accounts()
        await call.message.answer(
            "حساب جدید را انتخاب کنید. اگر گروهی متصل باشد تغییر رد می‌شود؛ گروه‌ها هرگز منتقل نمی‌شوند.",
            reply_markup=rows_keyboard(
                [(x.label, f"camp:setsender:{cid}:{x.id}") for x in accounts],
                back=f"camp:view:{cid}",
            ),
        )

    @router.callback_query(F.data.startswith("camp:setsender:"))
    async def sender_change(call: CallbackQuery) -> None:
        _, _, cid, aid = call.data.split(":")
        async with sessions() as session:
            target_count = await session.scalar(
                select(func.count())
                .select_from(CampaignTarget)
                .where(CampaignTarget.campaign_id == int(cid))
            )
            if target_count:
                await call.answer(
                    "ابتدا همه گروه‌های کمپین را حذف کنید؛ انتقال خودکار ممنوع است.", show_alert=True
                )
                return
            campaign = await session.get(Campaign, int(cid))
            campaign.sender_account_id = int(aid)
            await session.commit()
        await call.answer("حساب تغییر کرد", show_alert=True)

    @router.callback_query(F.data.startswith("camp:interval:"))
    async def interval_prompt(call: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(TextInput.campaign_interval)
        await state.update_data(campaign_id=int(call.data.rsplit(":", 1)[1]))
        await call.message.answer("فاصله جدید (مثلاً 30m یا 2h):")

    @router.message(TextInput.campaign_interval)
    async def interval_save(message: Message, state: FSMContext) -> None:
        try:
            seconds = parse_interval(message.text or "")
        except ValueError as exc:
            await message.answer(str(exc))
            return
        data = await state.get_data()
        async with sessions() as session:
            campaign = await session.get(Campaign, data["campaign_id"])
            campaign.interval_seconds = seconds
            if campaign.enabled:
                campaign.next_run_at = calculate_next_run(datetime.now(UTC), seconds)
            await session.commit()
        if campaign.enabled and scheduler:
            scheduler.schedule(campaign.id, campaign.next_run_at)
        await state.clear()
        await message.answer("✅ فاصله تغییر کرد.")

    @router.callback_query(F.data.startswith("camp:delask:"))
    async def delete_ask(call: CallbackQuery) -> None:
        cid = call.data.rsplit(":", 1)[1]
        await call.message.answer(
            "حذف کمپین قطعی است. تأیید می‌کنید؟", reply_markup=confirm_keyboard(f"camp:delete:{cid}")
        )

    @router.callback_query(F.data.startswith("camp:delete:"))
    async def delete_campaign(call: CallbackQuery) -> None:
        cid = int(call.data.rsplit(":", 1)[1])
        async with sessions() as session:
            media_paths = list(
                (
                    await session.scalars(
                        select(CampaignMessage.media_path).where(
                            CampaignMessage.campaign_id == cid,
                            CampaignMessage.media_path.is_not(None),
                        )
                    )
                ).all()
            )
            await session.execute(delete(Campaign).where(Campaign.id == cid))
            await session.commit()
        if scheduler:
            scheduler.cancel(cid)
        for media_path in media_paths:
            remove_managed_media(Path(media_path), media_directory)
        await call.message.edit_text("✅ کمپین حذف شد.")

    @router.callback_query(F.data.startswith("camp:sendask:"))
    async def send_ask(call: CallbackQuery) -> None:
        cid = call.data.rsplit(":", 1)[1]
        await call.message.answer(
            "ارسال دستی rotation را جلو می‌برد اما زمان‌بندی را تغییر نمی‌دهد.",
            reply_markup=confirm_keyboard(f"camp:send:{cid}"),
        )

    @router.callback_query(F.data.startswith("camp:send:"))
    async def send_now(call: CallbackQuery) -> None:
        if delivery is None:
            await call.answer("سرویس ارسال آماده نیست", show_alert=True)
            return
        cid = int(call.data.rsplit(":", 1)[1])
        async with sessions() as session:
            campaign = await session.get(Campaign, cid)
            remaining = retry_wait_seconds(campaign.manual_retry_at)
            if remaining:
                await call.answer(
                    f"تلگرام توقف خواسته است؛ {remaining} ثانیه دیگر، در {display_time(campaign.manual_retry_at)} دوباره تلاش کنید.",
                    show_alert=True,
                )
                return
            try:
                await validate_campaign_ready(session, campaign)
            except CampaignValidationError as exc:
                await call.answer(str(exc), show_alert=True)
                return
        ok = await delivery.execute(cid, manual=True)
        await call.message.edit_text("✅ اجرا انجام شد." if ok else "❌ اجرا ممکن نبود.")

    # Campaign message CRUD and ordering
    @router.callback_query(F.data.startswith("msg:list:"))
    async def message_list(call: CallbackQuery) -> None:
        cid = int(call.data.rsplit(":", 1)[1])
        async with sessions() as session:
            rows = list(
                (
                    await session.scalars(
                        select(CampaignMessage)
                        .where(CampaignMessage.campaign_id == cid)
                        .order_by(CampaignMessage.position)
                    )
                ).all()
            )
        items = [
            (f"{x.position}. {x.message_type.value} — {(x.text or '')[:25]}", f"msg:view:{x.id}")
            for x in rows
        ]
        await call.message.edit_text(
            "📝 پیام‌ها",
            reply_markup=rows_keyboard(
                items + [("➕ افزودن پیام", f"msg:add:{cid}")], back=f"camp:view:{cid}"
            ),
        )

    @router.callback_query(F.data.startswith("msg:view:"))
    async def message_view(call: CallbackQuery) -> None:
        mid = int(call.data.rsplit(":", 1)[1])
        async with sessions() as session:
            item = await session.get(CampaignMessage, mid)
        await call.message.edit_text(
            f"پیام {item.position}\n\n{item.text or '[تصویر بدون کپشن]'}",
            reply_markup=rows_keyboard(
                [
                    ("✏️ ویرایش متن/کپشن", f"msg:edit:{mid}"),
                    ("⬆️ بالاتر", f"msg:move:{mid}:-1"),
                    ("⬇️ پایین‌تر", f"msg:move:{mid}:1"),
                    ("🗑 حذف", f"msg:delask:{mid}"),
                ],
                back=f"msg:list:{item.campaign_id}",
            ),
        )

    @router.callback_query(F.data.startswith("msg:add:"))
    async def message_add_prompt(call: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(TextInput.message_add)
        await state.update_data(campaign_id=int(call.data.rsplit(":", 1)[1]))
        await call.message.answer("پیام جدید (متن یا تصویر+کپشن) را ارسال کنید:")

    @router.message(TextInput.message_add)
    async def message_add_save(message: Message, state: FSMContext) -> None:
        try:
            item = _message_data(message)
        except ValueError as exc:
            await message.answer(str(exc))
            return
        data = await state.get_data()
        async with sessions() as session:
            position = (
                await session.scalar(
                    select(func.max(CampaignMessage.position)).where(
                        CampaignMessage.campaign_id == data["campaign_id"]
                    )
                )
            ) or 0
            row = CampaignMessage(
                campaign_id=data["campaign_id"],
                position=position + 1,
                message_type=MessageType(item["message_type"]),
                text=item["text"],
            )
            session.add(row)
            await session.flush()
            if item["message_type"] == "PHOTO":
                media_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                row.media_path = str(media_directory / f"message_{row.id}.jpg")
                await message.bot.download(item["file_id"], destination=row.media_path)
            await session.commit()
        await state.clear()
        await message.answer("✅ پیام افزوده شد.")

    @router.callback_query(F.data.startswith("msg:edit:"))
    async def message_edit_prompt(call: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(TextInput.message_edit)
        await state.update_data(message_id=int(call.data.rsplit(":", 1)[1]))
        await call.message.answer("متن/کپشن جدید را وارد کنید:")

    @router.message(TextInput.message_edit)
    async def message_edit_save(message: Message, state: FSMContext) -> None:
        try:
            replacement = _message_data(message)
        except ValueError as exc:
            await message.answer(str(exc))
            return
        data = await state.get_data()
        async with sessions() as session:
            item = await session.get(CampaignMessage, data["message_id"])
            await replace_message_content(item, replacement, message.bot, media_directory)
            await session.commit()
        await state.clear()
        await message.answer("✅ ویرایش شد.")

    @router.callback_query(F.data.startswith("msg:move:"))
    async def message_move(call: CallbackQuery) -> None:
        _, _, mid, delta = call.data.split(":")
        async with sessions() as session:
            item = await session.get(CampaignMessage, int(mid))
            moved = await move_campaign_message(session, item, int(delta))
            if moved:
                await session.commit()
        await call.answer("جابجا شد" if moved else "امکان جابجایی نیست", show_alert=not moved)

    @router.callback_query(F.data.startswith("msg:delask:"))
    async def message_delete_ask(call: CallbackQuery) -> None:
        mid = call.data.rsplit(":", 1)[1]
        await call.message.answer(
            "پیام حذف شود؟", reply_markup=confirm_keyboard(f"msg:delete:{mid}")
        )

    @router.callback_query(F.data.startswith("msg:delete:"))
    async def message_delete(call: CallbackQuery) -> None:
        mid = int(call.data.rsplit(":", 1)[1])
        async with sessions() as session:
            item = await session.get(CampaignMessage, mid)
            cid = item.campaign_id
            count = await session.scalar(
                select(func.count())
                .select_from(CampaignMessage)
                .where(CampaignMessage.campaign_id == cid)
            )
            if count <= 1:
                await call.answer("کمپین باید حداقل یک پیام داشته باشد", show_alert=True)
                return
            obsolete_media = Path(item.media_path) if item.media_path else None
            await session.delete(item)
            await session.flush()
            await normalize_message_positions(session, cid)
            await session.commit()
            remove_managed_media(obsolete_media, media_directory)
        await call.message.edit_text("✅ پیام حذف شد.")

    # Campaign target membership
    @router.callback_query(F.data.startswith("ct:list:"))
    async def campaign_targets(call: CallbackQuery) -> None:
        cid = int(call.data.rsplit(":", 1)[1])
        async with sessions() as session:
            campaign = await session.scalar(
                select(Campaign).where(Campaign.id == cid).options(selectinload(Campaign.targets))
            )
            all_targets = list(
                (
                    await session.scalars(
                        select(TargetChat).where(
                            TargetChat.sender_account_id == campaign.sender_account_id,
                            TargetChat.enabled.is_(True),
                        )
                    )
                ).all()
            )
        selected = {x.id for x in campaign.targets}
        rows = [
            (f"{'✅' if x.id in selected else '⬜'} {x.title}", f"ct:toggle:{cid}:{x.id}")
            for x in all_targets
        ]
        await call.message.edit_text(
            "گروه‌ها را اضافه/حذف کنید:", reply_markup=rows_keyboard(rows, back=f"camp:view:{cid}")
        )

    @router.callback_query(F.data.startswith("ct:toggle:"))
    async def campaign_target_toggle(call: CallbackQuery) -> None:
        _, _, cid, tid = call.data.split(":")
        async with sessions() as session:
            campaign, target = (
                await session.get(Campaign, int(cid)),
                await session.get(TargetChat, int(tid)),
            )
            if target.sender_account_id != campaign.sender_account_id:
                await call.answer("حساب گروه ناسازگار است", show_alert=True)
                return
            row = await session.get(
                CampaignTarget, {"campaign_id": int(cid), "target_chat_id": int(tid)}
            )
            if row:
                await session.delete(row)
                await session.flush()
                paused = await pause_if_no_enabled_targets(session, campaign)
            else:
                session.add(CampaignTarget(campaign_id=int(cid), target_chat_id=int(tid)))
                paused = False
            await session.commit()
        if paused and scheduler:
            scheduler.cancel(int(cid))
        await call.answer(
            "آخرین گروه حذف و کمپین خودکار متوقف شد." if paused else "ذخیره شد",
            show_alert=paused,
        )

    # Account management
    @router.message(F.text == "👤 حساب‌های ارسال")
    async def accounts(message: Message) -> None:
        async with sessions() as session:
            rows = list(
                (await session.scalars(select(SenderAccount).order_by(SenderAccount.label))).all()
            )
        await message.answer(
            "👤 حساب‌ها\nاحراز هویت فقط با: <code>python -m app.cli.login_account</code>",
            parse_mode="HTML",
            reply_markup=rows_keyboard(
                [(f"{'🟢' if x.enabled else '🔴'} {x.label}", f"acct:view:{x.id}") for x in rows]
            ),
        )

    @router.callback_query(F.data.startswith("acct:view:"))
    async def account_view(call: CallbackQuery) -> None:
        aid = int(call.data.rsplit(":", 1)[1])
        async with sessions() as session:
            account = await session.get(SenderAccount, aid)
        await call.message.edit_text(
            f"👤 {account.label}\n{account.display_name}\nوضعیت: {account.connection_status}\nSession در پنل نمایش داده نمی‌شود.",
            reply_markup=rows_keyboard(
                [
                    ("🔄 بررسی اتصال", f"acct:check:{aid}"),
                    ("🔴 غیرفعال/🟢 فعال", f"acct:toggle:{aid}"),
                    ("🗑 حذف ثبت", f"acct:delask:{aid}"),
                ]
            ),
        )

    @router.callback_query(F.data.startswith("acct:check:"))
    async def account_check(call: CallbackQuery) -> None:
        aid = int(call.data.rsplit(":", 1)[1])
        async with sessions() as session:
            account = await session.get(SenderAccount, aid)
            try:
                await chat_service.clients.get(account)
                account.connection_status = "CONNECTED"
                account.last_connected_at = datetime.now(UTC)
                text = "✅ اتصال برقرار است"
            except (PermissionError, OSError, RPCError):
                account.connection_status = "DISCONNECTED"
                text = "❌ اتصال/مجوز معتبر نیست"
            await session.commit()
        await call.answer(text, show_alert=True)

    @router.callback_query(F.data.startswith("acct:toggle:"))
    async def account_toggle(call: CallbackQuery) -> None:
        aid = int(call.data.rsplit(":", 1)[1])
        async with sessions() as session:
            account = await session.get(SenderAccount, aid)
            paused_campaigns = []
            if account.enabled:
                campaigns = await disable_sender_account(session, account, scheduler)
                paused_campaigns = [(campaign.id, campaign.name) for campaign in campaigns]
            else:
                account.enabled = True
            await session.commit()
        text = "حساب فعال شد؛ کمپین‌ها باید صریحاً شروع شوند."
        if not account.enabled:
            names = "، ".join(name for _, name in paused_campaigns)
            text = "حساب غیرفعال شد."
            if names:
                text += " کمپین‌های متوقف‌شده: " + names
        await call.answer(text, show_alert=True)

    @router.callback_query(F.data.startswith("acct:delask:"))
    async def account_delete_ask(call: CallbackQuery) -> None:
        aid = call.data.rsplit(":", 1)[1]
        await call.message.answer(
            "فقط ثبت DB حذف می‌شود؛ session حذف نخواهد شد. تأیید؟",
            reply_markup=confirm_keyboard(f"acct:delete:{aid}"),
        )

    @router.callback_query(F.data.startswith("acct:delete:"))
    async def account_delete(call: CallbackQuery) -> None:
        aid = int(call.data.rsplit(":", 1)[1])
        async with sessions() as session:
            used = await session.scalar(
                select(func.count()).select_from(Campaign).where(Campaign.sender_account_id == aid)
            )
            if used:
                await call.answer("ابتدا کمپین‌های این حساب را حذف کنید", show_alert=True)
                return
            history = await session.scalar(
                select(func.count())
                .select_from(DeliveryLog)
                .where(DeliveryLog.sender_account_id == aid)
            )
            if history:
                await call.answer(
                    "به‌دلیل سابقه تحویل قابل ممیزی، ثبت حذف نمی‌شود؛ حساب را غیرفعال کنید.",
                    show_alert=True,
                )
                return
            await session.execute(delete(TargetChat).where(TargetChat.sender_account_id == aid))
            await session.execute(delete(SenderAccount).where(SenderAccount.id == aid))
            await session.commit()
        await call.message.edit_text("✅ ثبت حساب حذف شد؛ فایل session دست‌نخورده باقی ماند.")

    # Explicit target discovery/registration, paginated in pages of 8
    @router.message(F.text == "👥 گروه‌ها")
    async def groups(message: Message) -> None:
        rows = await enabled_accounts()
        await message.answer(
            "حساب را برای مدیریت گروه‌ها انتخاب کنید:",
            reply_markup=rows_keyboard([(x.label, f"groups:account:{x.id}") for x in rows]),
        )

    @router.callback_query(F.data.startswith("groups:account:"))
    async def groups_account(call: CallbackQuery) -> None:
        aid = int(call.data.rsplit(":", 1)[1])
        await call.message.edit_text(
            "گروه‌های ثبت‌شده یا دریافت گفتگوهای موجود:",
            reply_markup=rows_keyboard(
                [
                    ("📋 ثبت‌شده‌ها", f"groups:registered:{aid}"),
                    ("🔄 دریافت گروه‌های اکانت", f"groups:fetch:{aid}:0"),
                ]
            ),
        )

    @router.callback_query(F.data.startswith("groups:registered:"))
    async def registered(call: CallbackQuery) -> None:
        aid = int(call.data.rsplit(":", 1)[1])
        async with sessions() as session:
            rows = list(
                (
                    await session.scalars(
                        select(TargetChat).where(TargetChat.sender_account_id == aid)
                    )
                ).all()
            )
        await call.message.edit_text(
            "برای فعال/غیرفعال یا لغو ثبت انتخاب کنید:",
            reply_markup=rows_keyboard(
                [(f"{'✅' if x.enabled else '❌'} {x.title}", f"target:view:{x.id}") for x in rows]
            ),
        )

    @router.callback_query(F.data.startswith("groups:fetch:"))
    async def fetch_groups(call: CallbackQuery, state: FSMContext) -> None:
        _, _, aid, page = call.data.split(":")
        aid_i, page_i = int(aid), int(page)
        data = await state.get_data()
        dialogs = data.get(f"dialogs_{aid}")
        if dialogs is None:
            if chat_service is None:
                await call.answer("سرویس تلگرام آماده نیست", show_alert=True)
                return
            async with sessions() as session:
                account = await session.get(SenderAccount, aid_i)
            try:
                found = await chat_service.list_accessible(account)
            except (PermissionError, OSError, RPCError):
                await call.answer("دریافت گفتگوها ناموفق بود", show_alert=True)
                return
            dialogs = [
                x.__dict__
                if hasattr(x, "__dict__")
                else {
                    "telegram_chat_id": x.telegram_chat_id,
                    "title": x.title,
                    "username": x.username,
                    "chat_type": x.chat_type,
                }
                for x in found
            ]
            await state.update_data({f"dialogs_{aid}": dialogs})
        page_rows = dialogs[page_i * 8 : (page_i + 1) * 8]
        buttons = [
            (x["title"], f"groups:register:{aid}:{page}:{i + page_i * 8}")
            for i, x in enumerate(page_rows)
        ]
        if page_i:
            buttons.append(("⬅️ صفحه قبل", f"groups:fetch:{aid}:{page_i - 1}"))
        if (page_i + 1) * 8 < len(dialogs):
            buttons.append(("صفحه بعد ➡️", f"groups:fetch:{aid}:{page_i + 1}"))
        await call.message.edit_text(
            "فقط گروه مجاز را برای ثبت صریح انتخاب کنید:",
            reply_markup=rows_keyboard(buttons, back=f"groups:account:{aid}"),
        )

    @router.callback_query(F.data.startswith("groups:register:"))
    async def register_group(call: CallbackQuery, state: FSMContext) -> None:
        _, _, aid, page, index = call.data.split(":")
        data = await state.get_data()
        item = data[f"dialogs_{aid}"][int(index)]
        await state.update_data(pending_registration={"aid": int(aid), **item})
        await call.message.answer(
            f"نام: {item['title']}\nChat ID: <code>{item['telegram_chat_id']}</code>\nثبت شود؟",
            parse_mode="HTML",
            reply_markup=confirm_keyboard(f"groups:confirm:{page}"),
        )

    @router.callback_query(F.data.startswith("groups:confirm:"))
    async def register_confirm(call: CallbackQuery, state: FSMContext) -> None:
        item = (await state.get_data())["pending_registration"]
        async with sessions() as session:
            existing = await session.scalar(
                select(TargetChat).where(
                    TargetChat.sender_account_id == item["aid"],
                    TargetChat.telegram_chat_id == item["telegram_chat_id"],
                )
            )
            if existing:
                existing.enabled = True
                existing.last_verified_at = datetime.now(UTC)
            else:
                session.add(
                    TargetChat(
                        sender_account_id=item["aid"],
                        telegram_chat_id=item["telegram_chat_id"],
                        title=item["title"],
                        username=item["username"],
                        chat_type=item["chat_type"],
                        enabled=True,
                        last_verified_at=datetime.now(UTC),
                    )
                )
            await session.commit()
        await call.message.edit_text("✅ گروه صریحاً در allowlist ثبت شد.")

    @router.callback_query(F.data.startswith("target:view:"))
    async def target_view(call: CallbackQuery) -> None:
        tid = int(call.data.rsplit(":", 1)[1])
        async with sessions() as session:
            target = await session.get(TargetChat, tid)
        await call.message.edit_text(
            f"{target.title}\n<code>{target.telegram_chat_id}</code>",
            parse_mode="HTML",
            reply_markup=rows_keyboard(
                [("فعال/غیرفعال", f"target:toggle:{tid}"), ("لغو ثبت", f"target:delask:{tid}")]
            ),
        )

    @router.callback_query(F.data.startswith("target:toggle:"))
    async def target_toggle(call: CallbackQuery) -> None:
        tid = int(call.data.rsplit(":", 1)[1])
        async with sessions() as session:
            target = await session.get(TargetChat, tid)
            target.enabled = not target.enabled
            paused_campaigns = []
            if not target.enabled:
                await session.flush()
                campaign_ids = list(
                    (
                        await session.scalars(
                            select(CampaignTarget.campaign_id).where(
                                CampaignTarget.target_chat_id == tid
                            )
                        )
                    ).all()
                )
                for campaign_id in campaign_ids:
                    campaign = await session.get(Campaign, campaign_id)
                    if await pause_if_no_enabled_targets(session, campaign):
                        paused_campaigns.append((campaign.id, campaign.name))
            await session.commit()
        if scheduler:
            for campaign_id, _ in paused_campaigns:
                scheduler.cancel(campaign_id)
        text = "ذخیره شد"
        if paused_campaigns:
            text += "؛ کمپین متوقف شد: " + "، ".join(
                name for _, name in paused_campaigns
            )
        await call.answer(text, show_alert=True)

    @router.callback_query(F.data.startswith("target:delask:"))
    async def target_delask(call: CallbackQuery) -> None:
        tid = call.data.rsplit(":", 1)[1]
        await call.message.answer(
            "گروه از allowlist خارج شود؟", reply_markup=confirm_keyboard(f"target:delete:{tid}")
        )

    @router.callback_query(F.data.startswith("target:delete:"))
    async def target_delete(call: CallbackQuery) -> None:
        tid = int(call.data.rsplit(":", 1)[1])
        async with sessions() as session:
            campaign_ids = list(
                (
                    await session.scalars(
                        select(CampaignTarget.campaign_id).where(
                            CampaignTarget.target_chat_id == tid
                        )
                    )
                ).all()
            )
            await session.execute(
                delete(CampaignTarget).where(CampaignTarget.target_chat_id == tid)
            )
            await session.flush()
            paused_campaigns = []
            for campaign_id in campaign_ids:
                campaign = await session.get(Campaign, campaign_id)
                if await pause_if_no_enabled_targets(session, campaign):
                    paused_campaigns.append((campaign.id, campaign.name))
            await session.execute(delete(TargetChat).where(TargetChat.id == tid))
            await session.commit()
        if scheduler:
            for campaign_id, _ in paused_campaigns:
                scheduler.cancel(campaign_id)
        suffix = (
            "\n⏸ کمپین‌های متوقف‌شده: "
            + "، ".join(name for _, name in paused_campaigns)
            if paused_campaigns
            else ""
        )
        await call.message.edit_text("✅ ثبت گروه لغو شد." + suffix)

    # Test send: sender -> registered target -> message/photo -> preview -> confirmation
    @router.message(F.text == "📨 ارسال آزمایشی")
    async def test_start(message: Message, state: FSMContext) -> None:
        await state.clear()
        rows = await enabled_accounts()
        await message.answer(
            "حساب ارسال آزمایشی:",
            reply_markup=rows_keyboard([(x.label, f"test:sender:{x.id}") for x in rows]),
        )

    @router.callback_query(F.data.startswith("test:sender:"))
    async def test_sender(call: CallbackQuery, state: FSMContext) -> None:
        aid = int(call.data.rsplit(":", 1)[1])
        await state.update_data(test_sender_id=aid)
        async with sessions() as session:
            targets = list(
                (
                    await session.scalars(
                        select(TargetChat).where(
                            TargetChat.sender_account_id == aid, TargetChat.enabled.is_(True)
                        )
                    )
                ).all()
            )
        await call.message.edit_text(
            "گروه ثبت‌شده را انتخاب کنید:",
            reply_markup=rows_keyboard(
                [(x.title, f"test:target:{x.id}") for x in targets], back="cancel"
            ),
        )

    @router.callback_query(F.data.startswith("test:target:"))
    async def test_target(call: CallbackQuery, state: FSMContext) -> None:
        await state.update_data(test_target_id=int(call.data.rsplit(":", 1)[1]))
        await state.set_state(TextInput.test_message)
        await call.message.edit_text("پیام آزمایشی (متن یا تصویر+کپشن):")

    @router.message(TextInput.test_message)
    async def test_preview(message: Message, state: FSMContext) -> None:
        try:
            item = _message_data(message)
        except ValueError as exc:
            await message.answer(str(exc))
            return
        await state.update_data(test_item=item)
        await state.set_state(TextInput.test_preview)
        if item["message_type"] == "PHOTO":
            await message.answer_photo(
                item["file_id"],
                caption=item["text"] or None,
                parse_mode=None,
                reply_markup=confirm_keyboard("test:send"),
            )
        else:
            await message.answer(
                "پیش‌نمایش:\n\n" + (item["text"] or ""),
                parse_mode=None,
                reply_markup=confirm_keyboard("test:send"),
            )

    @router.callback_query(TextInput.test_preview, F.data == "test:send")
    async def test_send(call: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        async with sessions() as session:
            account, target = (
                await session.get(SenderAccount, data["test_sender_id"]),
                await session.get(TargetChat, data["test_target_id"]),
            )
            if not account.enabled or not target.enabled or target.sender_account_id != account.id:
                await call.answer("حساب یا گروه دیگر مجاز نیست", show_alert=True)
                return
            client = await chat_service.clients.get(account)
            item = data["test_item"]
            if item["message_type"] == "PHOTO":
                media_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                temporary = media_directory / f"test_{call.from_user.id}_{call.id}.jpg"
                try:
                    await call.bot.download(item["file_id"], destination=temporary)
                    await client.send_file(
                        target.telegram_chat_id,
                        str(temporary),
                        caption=item["text"],
                        parse_mode="HTML",
                    )
                finally:
                    temporary.unlink(missing_ok=True)
            else:
                await client.send_message(target.telegram_chat_id, item["text"], parse_mode="HTML")
        await state.clear()
        await call.message.edit_reply_markup()
        await call.message.answer("✅ ارسال آزمایشی انجام شد؛ زمان‌بندی و rotation تغییر نکرد.")

    @router.message(F.text == "📊 گزارش‌ها")
    async def reports(message: Message) -> None:
        async with sessions() as session:
            success = await session.scalar(
                select(func.count())
                .select_from(DeliveryLog)
                .where(DeliveryLog.status == DeliveryStatus.SUCCESS)
            )
            failed = await session.scalar(
                select(func.count())
                .select_from(DeliveryLog)
                .where(DeliveryLog.status == DeliveryStatus.FAILED)
            )
            accounts_count = await session.scalar(
                select(func.count())
                .select_from(SenderAccount)
                .where(SenderAccount.enabled.is_(True))
            )
        await message.answer(
            f"📊 آمار کلی\n✅ موفق: {success}\n❌ ناموفق: {failed}\n👤 حساب فعال: {accounts_count}",
            reply_markup=rows_keyboard(
                [
                    ("📊 آمار امروز", "report:today"),
                    ("❌ خطاهای اخیر", "report:errors"),
                    ("👤 وضعیت حساب‌ها", "report:accounts"),
                ]
            ),
        )

    @router.callback_query(F.data == "report:today")
    async def report_today(call: CallbackQuery) -> None:
        today = (
            datetime.now(display_zone)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .astimezone(UTC)
        )
        async with sessions() as session:
            success = await session.scalar(
                select(func.count())
                .select_from(DeliveryLog)
                .where(
                    DeliveryLog.attempted_at >= today, DeliveryLog.status == DeliveryStatus.SUCCESS
                )
            )
            failed = await session.scalar(
                select(func.count())
                .select_from(DeliveryLog)
                .where(
                    DeliveryLog.attempted_at >= today, DeliveryLog.status == DeliveryStatus.FAILED
                )
            )
        await call.message.edit_text(
            f"📊 آمار امروز ({timezone_name})\n✅ موفق: {success}\n❌ ناموفق: {failed}"
        )

    @router.callback_query(F.data == "report:errors")
    async def report_errors(call: CallbackQuery) -> None:
        async with sessions() as session:
            rows = list(
                (
                    await session.scalars(
                        select(DeliveryLog)
                        .where(
                            DeliveryLog.status.in_([DeliveryStatus.FAILED, DeliveryStatus.RETRY])
                        )
                        .order_by(DeliveryLog.attempted_at.desc())
                        .limit(10)
                    )
                ).all()
            )
        text = "\n".join(
            f"#{x.id} {x.error_type or x.status.value}: {(x.error_message or '—')[:80]}"
            for x in rows
        )
        await call.message.edit_text("❌ خطاهای اخیر\n\n" + (text or "خطایی ثبت نشده است."))

    @router.callback_query(F.data == "report:accounts")
    async def report_accounts(call: CallbackQuery) -> None:
        async with sessions() as session:
            rows = list(
                (await session.scalars(select(SenderAccount).order_by(SenderAccount.label))).all()
            )
        await call.message.edit_text(
            "👤 وضعیت حساب‌ها\n\n"
            + "\n".join(
                f"{'🟢' if x.enabled else '🔴'} {x.label}: {x.connection_status}" for x in rows
            )
        )

    @router.message(F.text == "⚙️ تنظیمات")
    async def settings_view(message: Message) -> None:
        await message.answer(
            "⚙️ تنظیمات از متغیرهای محیطی مدیریت می‌شوند تا تغییرات امنیتی قابل ممیزی باشند."
        )

    @router.message()
    async def fallback(message: Message) -> None:
        await message.answer("گزینه‌ای از منو انتخاب کنید.", reply_markup=main_menu())

    return router
