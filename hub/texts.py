"""Тексты бота на узбекском (латиница) и русском. Сводку присылает касса, здесь она превращается в сообщение.

Формат сводки описан в core/hub_snapshot.py. Все функции только собирают строки и ничего не отправляют.
"""

LANGS = ("uz", "ru")
DEFAULT_LANG = "uz"

# Типы уведомлений, которые получатель включает и выключает в /sozlamalar
CATEGORIES = ("shift", "cancel", "discount", "stock", "review", "orders", "morning", "shopping", "error", "backup")
# Эти типы приходят из кассы как события; остальные бот формирует сам
EVENT_CATEGORIES = ("shift", "cancel", "discount", "stock", "review", "orders", "error")
# Второму и следующему получателю заведения эти типы по умолчанию выключены (в копии базы данные гостей)
SENSITIVE_DEFAULT_OFF = ("backup", "error")

S = {
    "uz": {
        "today": "Bugun", "yesterday": "Kecha", "week": "Oxirgi 7 kun",
        "no_sales": "Hali savdo yoʻq.",
        "revenue": "💰 Tushum: {sum}",
        "orders_avg": "🧾 Buyurtmalar: {n} · oʻrtacha chek {avg}",
        "profit": "📈 Foyda: {sum} ({pct}%)",
        "cash_card": "💵 Naqd {cash} · 💳 Karta {card}",
        "cancelled": "❌ Bekor qilingan: {n} ta, {sum}",
        "vs_week": "Oʻtgan haftaning shu kuniga nisbatan: {sign}{pct}%",
        "vs_prev_week": "Oldingi 7 kunga nisbatan: {sign}{pct}%",
        "best_day": "🏆 Eng yaxshi kun: {date}, {sum}",
        "asof": "🕐 {time} holatiga",
        "no_data": "Hali maʼlumot kelmagan. Kassa ulangandan keyin bir necha daqiqa kuting.",
        "top_title": "🍽 Eng koʻp sotilganlar · {venue}",
        "top_today": "Bugun",
        "top_week": "Hafta",
        "top_empty": "Hali sotuv yoʻq.",
        "top_row": "{i}. {name} — {qty} ta ({sum})",
        "shift_open": "🟢 Smena ochiq · {venue}",
        "shift_cashier": "Kassir: {name}, {time} dan",
        "shift_stats": "Tushum: {sum}, buyurtmalar: {n}",
        "shift_none": "⚪ Hozir ochiq smena yoʻq · {venue}",
        "shop_title": "🛒 Sotib olish kerak · {venue}",
        "shop_ok": "✅ Hammasi yetarli · {venue}",
        "shop_row": "{icon} {name} — olish: {qty}{left}",
        "shop_more": "…va yana {n} ta",
        "shop_cost": "Taxminiy narx: {sum}",
        "left_out": " · tugagan",
        "left_today": " · bugun tugaydi",
        "left_days": " · ~{n} kunga yetadi",
        "morning": "☀️ Xayrli tong! Kecha qanday oʻtdi:",
        "unit_g": "g", "unit_kg": "kg", "unit_ml": "ml", "unit_l": "l", "unit_pcs": "dona",
        "sum": "soʻm",
        "welcome": "✅ Ulandi! Endi «{venue}» boʻyicha xabarlar shu yerga keladi.\n\nBuyruqlar roʻyxati: /yordam",
        "welcome_again": "✅ Bu chat allaqachon «{venue}» ga ulangan.\n\nBuyruqlar roʻyxati: /yordam",
        "start_plain": "Salom! Men CafePOS botiman. Ulanish uchun kassadagi «Telegram» sahifasidan havolani oching yoki QR-kodni skanerlang.",
        "link_bad": "Havola eskirgan yoki ishlatilgan. Kassadagi «Telegram» sahifasida yangi havola oling.",
        "not_linked": "Bu chat hali ulanmagan. Kassadagi «Telegram» sahifasidan havolani oching.",
        "help": (
            "📋 Buyruqlar:\n"
            "/bugun — bugungi tushum va foyda\n"
            "/kecha — kechagi natija\n"
            "/hafta — oxirgi 7 kun\n"
            "/ombor — nima sotib olish kerak\n"
            "/smena — joriy smena\n"
            "/top — eng koʻp sotilgan taomlar\n"
            "/sozlamalar — qaysi xabarlar kelsin\n"
            "/til — tilni oʻzgartirish (UZ/RU)"
        ),
        "settings_title": "⚙️ Qaysi xabarlar kelsin? Bosib yoqing yoki oʻchiring.",
        "lang_set": "✅ Til: Oʻzbekcha",
        "test": "✅ Sinov xabari: «{venue}» ulangan, bildirishnomalar ishlayapti.",
        "btn_today": "📊 Bugun", "btn_yesterday": "Kecha", "btn_week": "Hafta",
        "btn_stock": "🛒 Ombor", "btn_shift": "Smena", "btn_top": "🍽 Top",
        "cat_shift": "Smena yopilishi", "cat_cancel": "Bekor qilish va qaytarishlar",
        "cat_discount": "Katta chegirmalar", "cat_stock": "Mahsulot tugayapti",
        "cat_review": "Yomon baholar", "cat_orders": "Juma yetkazib berish buyurtmalari",
        "cat_morning": "Ertalabki hisobot (09:00)",
        "cat_shopping": "Sotib olish roʻyxati (09:00)", "cat_error": "Server xatolari",
        "cat_backup": "Baza nusxasi",
        "unknown": "Tushunmadim. Buyruqlar roʻyxati: /yordam",
    },
    "ru": {
        "today": "Сегодня", "yesterday": "Вчера", "week": "Последние 7 дней",
        "no_sales": "Продаж пока нет.",
        "revenue": "💰 Выручка: {sum}",
        "orders_avg": "🧾 Заказов: {n} · средний чек {avg}",
        "profit": "📈 Прибыль: {sum} ({pct}%)",
        "cash_card": "💵 Наличные {cash} · 💳 Карта {card}",
        "cancelled": "❌ Отменено: {n}, {sum}",
        "vs_week": "К такому же дню прошлой недели: {sign}{pct}%",
        "vs_prev_week": "К предыдущим 7 дням: {sign}{pct}%",
        "best_day": "🏆 Лучший день: {date}, {sum}",
        "asof": "🕐 данные на {time}",
        "no_data": "Данных пока нет. Подождите несколько минут после подключения кассы.",
        "top_title": "🍽 Что чаще берут · {venue}",
        "top_today": "Сегодня",
        "top_week": "Неделя",
        "top_empty": "Продаж пока нет.",
        "top_row": "{i}. {name} — {qty} шт ({sum})",
        "shift_open": "🟢 Смена открыта · {venue}",
        "shift_cashier": "Кассир: {name}, с {time}",
        "shift_stats": "Выручка: {sum}, заказов: {n}",
        "shift_none": "⚪ Сейчас нет открытой смены · {venue}",
        "shop_title": "🛒 Нужно купить · {venue}",
        "shop_ok": "✅ Всего хватает · {venue}",
        "shop_row": "{icon} {name} — купить: {qty}{left}",
        "shop_more": "…и ещё {n}",
        "shop_cost": "Примерная стоимость: {sum}",
        "left_out": " · закончилось",
        "left_today": " · закончится сегодня",
        "left_days": " · хватит на ~{n} дн.",
        "morning": "☀️ Доброе утро! Как прошёл вчерашний день:",
        "unit_g": "г", "unit_kg": "кг", "unit_ml": "мл", "unit_l": "л", "unit_pcs": "шт",
        "sum": "сум",
        "welcome": "✅ Подключено! Теперь сообщения по «{venue}» будут приходить сюда.\n\nСписок команд: /yordam",
        "welcome_again": "✅ Этот чат уже подключён к «{venue}».\n\nСписок команд: /yordam",
        "start_plain": "Здравствуйте! Я бот CafePOS. Чтобы подключиться, откройте ссылку на странице «Telegram» в кассе или отсканируйте QR-код.",
        "link_bad": "Ссылка устарела или уже использована. Получите новую на странице «Telegram» в кассе.",
        "not_linked": "Этот чат ещё не подключён. Откройте ссылку на странице «Telegram» в кассе.",
        "help": (
            "📋 Команды:\n"
            "/bugun — выручка и прибыль за сегодня\n"
            "/kecha — итоги вчера\n"
            "/hafta — последние 7 дней\n"
            "/ombor — что нужно купить\n"
            "/smena — текущая смена\n"
            "/top — самые продаваемые блюда\n"
            "/sozlamalar — какие сообщения получать\n"
            "/til — сменить язык (UZ/RU)"
        ),
        "settings_title": "⚙️ Какие сообщения присылать? Нажмите, чтобы включить или выключить.",
        "lang_set": "✅ Язык: Русский",
        "test": "✅ Тест: «{venue}» подключено, уведомления работают.",
        "btn_today": "📊 Сегодня", "btn_yesterday": "Вчера", "btn_week": "Неделя",
        "btn_stock": "🛒 Склад", "btn_shift": "Смена", "btn_top": "🍽 Топ",
        "cat_shift": "Закрытие смены", "cat_cancel": "Отмены и возвраты",
        "cat_discount": "Крупные скидки", "cat_stock": "Заканчиваются продукты",
        "cat_review": "Плохие отзывы", "cat_orders": "Заказы доставки Juma",
        "cat_morning": "Утренняя сводка (09:00)",
        "cat_shopping": "Список закупки (09:00)", "cat_error": "Ошибки сервера",
        "cat_backup": "Копия базы",
        "unknown": "Не понял. Список команд: /yordam",
    },
}

LEVEL_ICON = {"out": "🔴", "critical": "🟠", "soon": "🟡"}


def T(lang, key, **kw):
    table = S.get(lang) or S[DEFAULT_LANG]
    text = table.get(key) or S[DEFAULT_LANG][key]
    return text.format(**kw) if kw else text


def money(value, lang):
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        number = 0
    return f"{number:,}".replace(",", " ") + " " + T(lang, "sum")


def _num(value):
    return f"{value:.1f}".rstrip("0").rstrip(".")


def qty_text(value, unit, lang):
    value = float(value or 0)
    if unit == "g":
        return f"{_num(value / 1000)} {T(lang, 'unit_kg')}" if value >= 1000 else f"{round(value)} {T(lang, 'unit_g')}"
    if unit == "ml":
        return f"{_num(value / 1000)} {T(lang, 'unit_l')}" if value >= 1000 else f"{round(value)} {T(lang, 'unit_ml')}"
    return f"{round(value)} {T(lang, 'unit_pcs')}"


def _name(row, lang):
    if lang == "uz" and row.get("name_uz"):
        return row["name_uz"]
    return row.get("name", "")


def _sign(pct):
    return ("+", pct) if pct >= 0 else ("−", abs(pct))


def asof_line(snapshot, lang):
    stamp = (snapshot or {}).get("generated_at", "")
    time = stamp[11:16] if len(stamp) >= 16 else ""
    return T(lang, "asof", time=time) if time else ""


def day_report(lang, venue, label, day, asof=""):
    lines = [f"📊 {T(lang, label)} · {venue}"]
    if not day or not day.get("orders"):
        lines.append(T(lang, "no_sales"))
    else:
        lines.append(T(lang, "revenue", sum=money(day["revenue"], lang)))
        lines.append(T(lang, "orders_avg", n=day["orders"], avg=money(day.get("avg", 0), lang)))
        if day.get("has_cost"):
            pct = round(day["profit"] / day["revenue"] * 100) if day["revenue"] else 0
            lines.append(T(lang, "profit", sum=money(day["profit"], lang), pct=pct))
        lines.append(T(lang, "cash_card", cash=money(day.get("cash", 0), lang), card=money(day.get("card", 0), lang)))
        if day.get("vs_last_week") is not None:
            sign, pct = _sign(day["vs_last_week"])
            lines.append(T(lang, "vs_week", sign=sign, pct=pct))
    if day and day.get("cancelled"):
        lines.append(T(lang, "cancelled", n=day["cancelled"], sum=money(day.get("cancelled_sum", 0), lang)))
    if asof:
        lines.append(asof)
    return "\n".join(lines)


def week_report(lang, venue, week, asof=""):
    lines = [f"📅 {T(lang, 'week')} · {venue}"]
    if not week or not week.get("orders"):
        lines.append(T(lang, "no_sales"))
    else:
        lines.append(T(lang, "revenue", sum=money(week["revenue"], lang)))
        lines.append(T(lang, "orders_avg", n=week["orders"],
                       avg=money(week["revenue"] / week["orders"], lang)))
        if week.get("has_cost"):
            pct = round(week["profit"] / week["revenue"] * 100) if week["revenue"] else 0
            lines.append(T(lang, "profit", sum=money(week["profit"], lang), pct=pct))
        if week.get("prev_revenue"):
            change = round((week["revenue"] - week["prev_revenue"]) / week["prev_revenue"] * 100)
            sign, pct = _sign(change)
            lines.append(T(lang, "vs_prev_week", sign=sign, pct=pct))
        best = week.get("best_day")
        if best:
            lines.append(T(lang, "best_day", date=best["date"], sum=money(best["revenue"], lang)))
    if asof:
        lines.append(asof)
    return "\n".join(lines)


def top_report(lang, venue, snapshot):
    lines = [T(lang, "top_title", venue=venue)]
    for key in ("top_today", "top_week"):
        rows = snapshot.get(key) or []
        lines.append("")
        lines.append(f"▸ {T(lang, key)}")
        if not rows:
            lines.append(T(lang, "top_empty"))
        for i, row in enumerate(rows[:5], 1):
            lines.append(T(lang, "top_row", i=i, name=_name(row, lang), qty=row["qty"],
                           sum=money(row["revenue"], lang)))
    return "\n".join(lines)


def shift_report(lang, venue, shift):
    if not shift or not shift.get("open"):
        return T(lang, "shift_none", venue=venue)
    return "\n".join([
        T(lang, "shift_open", venue=venue),
        T(lang, "shift_cashier", name=shift.get("cashier", "—"), time=shift.get("opened_at", "")),
        T(lang, "shift_stats", sum=money(shift.get("revenue", 0), lang), n=shift.get("orders", 0)),
    ])


def shopping_report(lang, venue, rows, limit=15):
    if not rows:
        return T(lang, "shop_ok", venue=venue)
    lines = [T(lang, "shop_title", venue=venue)]
    for row in rows[:limit]:
        days = row.get("days_left")
        if row.get("level") == "out":
            left = T(lang, "left_out")
        elif days is None:
            left = ""
        elif days < 1:
            left = T(lang, "left_today")
        else:
            left = T(lang, "left_days", n=round(days))
        lines.append(T(lang, "shop_row", icon=LEVEL_ICON.get(row.get("level"), "•"), name=_name(row, lang),
                       qty=qty_text(row.get("suggest", 0), row.get("unit", "g"), lang), left=left))
    if len(rows) > limit:
        lines.append(T(lang, "shop_more", n=len(rows) - limit))
    total = sum(float(r.get("cost") or 0) for r in rows)
    if total:
        lines.append("")
        lines.append(T(lang, "shop_cost", sum=money(total, lang)))
    return "\n".join(lines)


def main_keyboard(lang):
    def row(*keys):
        return [{"text": T(lang, f"btn_{k}"), "callback_data": f"cmd:{k}"} for k in keys]

    return {"inline_keyboard": [row("today", "yesterday", "week"), row("stock", "shift", "top")]}


def settings_keyboard(lang, off):
    rows = []
    for category in CATEGORIES:
        mark = "🚫" if category in off else "✅"
        rows.append([{"text": f"{mark} {T(lang, 'cat_' + category)}", "callback_data": f"set:{category}"}])
    return {"inline_keyboard": rows}
