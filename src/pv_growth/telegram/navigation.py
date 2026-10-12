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


def menu_keyboard(*, weekly=False):
    buttons = [
        {"text": label, "style": "success" if command in {"/free", "/buy"} else "primary"}
        for label, command in MENU_ACTIONS.items()
    ]
    if weekly:
        buttons[1]["text"] = "🎁 هدیهٔ هفتگی ۵۰–۱۰۰ مگ"
        buttons[1]["style"] = "success"
    return ReplyKeyboard([buttons[i : i + 2] for i in range(0, len(buttons), 2)])


def menu_command(text):
    if text == "🎁 هدیهٔ هفتگی ۵۰–۱۰۰ مگ":
        return "/gift"
    return MENU_ACTIONS.get(text, text.split(" ", 1)[0].split("@", 1)[0])


def help_text(*, weekly=False):
    if weekly:
        return (
            "ℹ️ راهنمای PV Network\n\n"
            "💎 تست کیفیت سرویس اصلی: کانفیگ‌های ۱ گیگ مشترک مستقیم در @pvnetwork0.\n"
            "🌐 کانفیگ وب: مستقیم و قابل کپی در @pvnetwork_freeconfig.\n"
            "🎁 هدیهٔ ۵۰ تا ۱۰۰ مگی از سرور اصلی برای همهٔ اعضای هر دو کانال، حتی خریداران.\n"
            "⏳ هدیه هر هفت روز یک‌بار؛ حداکثر ۲۴ ساعت یا پایان حجم.\n"
            "📦 تا پایان بودجهٔ روزانهٔ ۵۰ گیگ؛ قرعه و انتظار ساعت ۲۳ حذف شده است.\n"
            "🛒 برای حجم بیشتر یا ادامهٔ استفاده: @pvnetwork_bot.\n"
            "📨 معرفی و اعتبار طبق قانون بومی Mirza در ربات خرید."
        )
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
