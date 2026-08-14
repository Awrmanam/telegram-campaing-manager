from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📢 کمپین‌ها"), KeyboardButton(text="➕ کمپین جدید")],
            [KeyboardButton(text="👤 حساب‌های ارسال"), KeyboardButton(text="👥 گروه‌ها")],
            [KeyboardButton(text="📨 ارسال آزمایشی"), KeyboardButton(text="📊 گزارش‌ها")],
            [KeyboardButton(text="⚙️ تنظیمات")],
        ],
        resize_keyboard=True,
    )


def rows_keyboard(rows: list[tuple[str, str]], *, back: str | None = None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for label, data in rows:
        builder.button(text=label, callback_data=data)
    builder.adjust(1)
    if back:
        builder.row(InlineKeyboardButton(text="🔙 بازگشت", callback_data=back))
    return builder.as_markup()


def confirm_keyboard(confirm: str, cancel: str = "cancel") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ تأیید", callback_data=confirm),
                InlineKeyboardButton(text="❌ لغو", callback_data=cancel),
            ]
        ]
    )
