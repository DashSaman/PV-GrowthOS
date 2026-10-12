"""Persistent private-chat actions; grant authorization remains in the handlers."""

from pv_growth.telegram.client import ReplyKeyboard

MENU_ACTIONS = {
    "🎁 تست اصلی ۱ گیگی": "/free",
    "🎲 ثبت‌نام قرعه‌کشی": "/lottery",
    "🌐 کانفیگ رایگان وب": "/web",
    "🛒 خرید سرویس اصلی": "/buy",
    "📨 معرفی به دوستان": "/share",
    "ℹ️ راهنما": "/help",
    "🏠 منوی اصلی": "/menu",
    "🔄 بررسی دوباره شرایط": "/status",
}


def menu_keyboard():
    buttons = [
        {"text": label, "style": "success" if command in {"/free", "/buy"} else "primary"}
        for label, command in MENU_ACTIONS.items()
    ]
    return ReplyKeyboard([buttons[i : i + 2] for i in range(0, len(buttons), 2)])


def menu_command(text):
    return MENU_ACTIONS.get(text, text.split(" ", 1)[0].split("@", 1)[0])


def help_text():
    return (
        "ℹ️ راهنمای منوی PV Network\n\n"
        "🎁 تست اصلی: آخرین تست ۱ گیگی مشترک؛ اعتبار حداکثر ۲۴ ساعت یا پایان حجم.\n"
        "🌐 وب: کدهای عمومی آزموده‌شده مستقیم در کانال، قابل کپی برای همه.\n"
        "🎲 قرعه: ابتدا دکمهٔ ثبت‌نام را تأیید کن؛ نتیجه ساعت ۲۳ تهران، دریافت فوری نیست.\n"
        "🔐 تست اصلی و قرعه: عضویت هر دو کانال، ۴۵ روز بدون خرید و نبود سرویس فعال/نامشخص.\n"
        "🛒 خرید: اشتراک شخصی و تعرفه‌ها در @pvnetwork_bot.\n"
        "📨 معرفی: لینک معرفی داوطلبانه؛ پورسانت طبق قانون فعلی Mirza پس از خرید واجد شرایط.\n"
        "🔄 بررسی دوباره: شرایط همین حساب دوباره خوانده می‌شود.\n\n"
        "از دکمه‌های پایین استفاده کن؛ نوشتن فرمان لازم نیست."
    )
