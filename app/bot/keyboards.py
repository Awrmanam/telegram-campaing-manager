from aiogram.types import KeyboardButton, ReplyKeyboardMarkup


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📢 کمپین‌ها"), KeyboardButton(text="➕ کمپین جدید")],
        [KeyboardButton(text="👤 حساب‌های ارسال"), KeyboardButton(text="👥 گروه‌ها")],
        [KeyboardButton(text="📨 ارسال آزمایشی"), KeyboardButton(text="📊 گزارش‌ها")],
        [KeyboardButton(text="⚙️ تنظیمات")],
    ], resize_keyboard=True)
