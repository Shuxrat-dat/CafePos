"""Уведомления владельцу в Telegram.

Работает так: в настройках указываются токен бота и чат. Сообщения уходят в фоне, поэтому касса
не ждёт ответа Telegram, а если сети нет, заказ всё равно проводится. Ошибки пишутся в журнал
(без токена) и наружу не выбрасываются.
"""
import json
import logging
import threading
import urllib.error
import urllib.request

from django.conf import settings
from django.utils import translation
from django.utils.translation import gettext as _

from .models import CafeSettings

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/sendMessage"
TIMEOUT = 6


def money(value):
    """12345678 -> '12 345 678 сум'."""
    try:
        return f"{int(round(float(value))):,}".replace(",", " ") + " " + _("сум")
    except (TypeError, ValueError):
        return str(value)


def is_configured(cafe=None):
    cafe = cafe or CafeSettings.get()
    return bool(cafe.telegram_bot_token.strip() and cafe.telegram_chat_id.strip())


def _post(token, chat_id, text):
    """Одна отправка. Возвращает (успех, пояснение). Токен в пояснения не попадает."""
    payload = json.dumps({"chat_id": chat_id, "text": text, "disable_web_page_preview": True}).encode()
    request = urllib.request.Request(
        API.format(token=token), data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return 200 <= response.status < 300, ""
    except urllib.error.HTTPError as exc:
        reasons = {
            401: _("Неверный токен бота."),
            400: _("Чат не найден. Проверьте номер чата и напишите боту /start."),
            403: _("Бот не может писать в этот чат. Напишите ему /start."),
        }
        return False, reasons.get(exc.code, _("Telegram вернул ошибку %(code)s.") % {"code": exc.code})
    except (urllib.error.URLError, OSError, ValueError):
        return False, _("Нет связи с Telegram. Проверьте интернет.")


def send_now(text, cafe=None):
    """Синхронная отправка для кнопки «Отправить тест»: нужно показать результат."""
    cafe = cafe or CafeSettings.get()
    if not is_configured(cafe):
        return False, _("Укажите токен бота и номер чата.")
    return _post(cafe.telegram_bot_token.strip(), cafe.telegram_chat_id.strip(), text)


def send(text, event):
    """Ставит сообщение в очередь, если событие включено. Никогда не падает."""
    try:
        cafe = CafeSettings.get()
        if not is_configured(cafe) or not getattr(cafe, f"tg_notify_{event}", False):
            return False
        args = (cafe.telegram_bot_token.strip(), cafe.telegram_chat_id.strip(), text)
        if getattr(settings, "TELEGRAM_SYNC", False):
            _post(*args)
        else:
            threading.Thread(target=_safe_post, args=args, daemon=True).start()
        return True
    except Exception:  # уведомление не должно ломать работу кассы
        log.exception("Telegram: не удалось поставить сообщение в очередь")
        return False


def _safe_post(token, chat_id, text):
    ok, reason = _post(token, chat_id, text)
    if not ok:
        log.warning("Telegram: сообщение не отправлено: %s", reason)


def _lang(cafe):
    return translation.override(cafe.default_language)


def _person(user):
    return (user.get_full_name() or user.username) if user else "—"


# ---------- события ----------

def notify_shift_closed(shift):
    try:
        cafe = CafeSettings.get()
        with _lang(cafe):
            difference = shift.difference or 0
            lines = [
                "🔒 " + _("Смена №%(id)s закрыта · %(cafe)s") % {"id": shift.id, "cafe": cafe.name},
                _("Кассир: %(name)s") % {"name": _person(shift.cashier)},
                _("Выручка: %(sum)s") % {"sum": money(shift.total_revenue)},
                "   " + _("наличные %(cash)s, карта %(card)s") % {
                    "cash": money(shift.cash_sales), "card": money(shift.card_sales)},
                _("Заказов: %(n)s") % {"n": shift.orders.count()},
                _("В кассе должно быть: %(sum)s") % {"sum": money(shift.expected_cash)},
                _("По факту: %(sum)s") % {"sum": money(shift.actual_cash or 0)},
            ]
            if difference:
                sign = "+" if difference > 0 else "−"
                lines.append("⚠️ " + _("Расхождение: %(sign)s%(sum)s") % {"sign": sign, "sum": money(abs(difference))})
            else:
                lines.append("✅ " + _("Касса сошлась"))
            if shift.closing_comment:
                lines.append(_("Комментарий: %(text)s") % {"text": shift.closing_comment})
        return send("\n".join(lines), "shift")
    except Exception:
        log.exception("Telegram: сообщение о смене не собрано")
        return False


def notify_order_cancelled(order, user):
    try:
        cafe = CafeSettings.get()
        with _lang(cafe):
            total = order.totals["total"] if order.items.exists() else 0
            if not total:
                return False  # пустые черновики не интересны
            lines = [
                "❌ " + _("Отменён заказ №%(id)s · %(cafe)s") % {"id": order.id, "cafe": cafe.name},
                _("Сумма: %(sum)s, %(place)s") % {"sum": money(total), "place": order.place_label},
                _("Кто отменил: %(name)s") % {"name": _person(user)},
                _("Причина: %(text)s") % {"text": order.cancel_reason or _("не указана")},
            ]
        return send("\n".join(lines), "cancel")
    except Exception:
        log.exception("Telegram: сообщение об отмене не собрано")
        return False


def notify_discount(order, user):
    """Сообщает, если скидка не меньше порога из настроек."""
    try:
        cafe = CafeSettings.get()
        subtotal = order.totals["subtotal"]
        if not subtotal or not order.discount_kind:
            return False
        discount = order.discount_for(subtotal)
        percent = float(discount) / float(subtotal) * 100
        if percent < cafe.tg_discount_percent:
            return False
        with _lang(cafe):
            lines = [
                "🏷 " + _("Крупная скидка %(p)s%% · %(cafe)s") % {"p": round(percent), "cafe": cafe.name},
                _("Заказ №%(id)s, %(place)s") % {"id": order.id, "place": order.place_label},
                _("Скидка %(d)s из %(s)s") % {"d": money(discount), "s": money(subtotal)},
                _("Кто дал: %(name)s") % {"name": _person(user)},
                _("Причина: %(text)s") % {"text": order.discount_reason or _("не указана")},
            ]
        return send("\n".join(lines), "discount")
    except Exception:
        log.exception("Telegram: сообщение о скидке не собрано")
        return False


def notify_low_stock(products):
    """products: продукты, которые только что опустились до минимума или ниже."""
    if not products:
        return False
    try:
        cafe = CafeSettings.get()
        with _lang(cafe):
            lines = ["📦 " + _("Заканчиваются продукты · %(cafe)s") % {"cafe": cafe.name}]
            for product in products[:15]:
                lines.append(f"• {product.name}: {product.stock.normalize():f} {product.get_unit_display()}")
            if len(products) > 15:
                lines.append(_("…и ещё %(n)s") % {"n": len(products) - 15})
        return send("\n".join(lines), "stock")
    except Exception:
        log.exception("Telegram: сообщение о складе не собрано")
        return False
