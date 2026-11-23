"""Уведомления владельцу в Telegram.

Работает так: в настройках указываются токен бота и чат. Сообщения уходят в фоне, поэтому касса
не ждёт ответа Telegram, а если сети нет, заказ всё равно проводится. Ошибки пишутся в журнал
(без токена) и наружу не выбрасываются.
"""
import json
import logging
import os
import threading
import urllib.error
import urllib.request
import uuid

from django.conf import settings
from django.utils import translation
from django.utils.translation import gettext as _

from . import hub
from .models import CafeSettings

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/sendMessage"
API_DOCUMENT = "https://api.telegram.org/bot{token}/sendDocument"
TIMEOUT = 6


def money(value):
    """12345678 -> '12 345 678 сум'."""
    try:
        return f"{int(round(float(value))):,}".replace(",", " ") + " " + _("сум")
    except (TypeError, ValueError):
        return str(value)


def is_configured(cafe=None):
    """Уведомления подключены: через центрального бота (быстрое подключение) или через свой токен."""
    cafe = cafe or CafeSettings.get()
    if hub.connected(cafe):
        return True
    return bool(cafe.telegram_bot_token.strip() and cafe.telegram_chat_id.strip())


def _dispatch(func, *args):
    """Отправка в фоне, чтобы касса не ждала сеть. В тестах (TELEGRAM_SYNC) выполняется сразу."""
    if getattr(settings, "TELEGRAM_SYNC", False):
        func(*args)
    else:
        threading.Thread(target=func, args=args, daemon=True).start()


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
    if hub.connected(cafe):
        return hub.send_test(cafe)
    if not is_configured(cafe):
        return False, _("Укажите токен бота и номер чата.")
    return _post(cafe.telegram_bot_token.strip(), cafe.telegram_chat_id.strip(), text)


def send(text, event):
    """Ставит сообщение в очередь, если событие включено. Никогда не падает."""
    try:
        cafe = CafeSettings.get()
        if not is_configured(cafe) or not getattr(cafe, f"tg_notify_{event}", False):
            return False
        if hub.connected(cafe):
            _dispatch(hub.send_event, cafe, event, text)
            return True
        args = (cafe.telegram_bot_token.strip(), cafe.telegram_chat_id.strip(), text)
        if getattr(settings, "TELEGRAM_SYNC", False):
            _post(*args)
        else:
            threading.Thread(target=_safe_post, args=args, daemon=True).start()
        return True
    except Exception:  # уведомление не должно ломать работу кассы
        log.exception("Telegram: не удалось поставить сообщение в очередь")
        return False


def send_document(path, caption=""):
    """Отправляет файл владельцу (например, копию базы). Синхронно: вызывается из команды, не из кассы.
    Возвращает (успех, пояснение)."""
    cafe = CafeSettings.get()
    if hub.connected(cafe):
        return hub.send_document(cafe, path, caption)
    if not is_configured(cafe):
        return False, _("Укажите токен бота и номер чата.")
    boundary = uuid.uuid4().hex
    with open(path, "rb") as fh:
        content = fh.read()
    parts = []
    for name, value in (("chat_id", cafe.telegram_chat_id.strip()), ("caption", caption)):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    filename = os.path.basename(path)
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n".encode() + content + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    request = urllib.request.Request(
        API_DOCUMENT.format(token=cafe.telegram_bot_token.strip()), data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return 200 <= response.status < 300, ""
    except urllib.error.HTTPError as exc:
        return False, _("Telegram вернул ошибку %(code)s.") % {"code": exc.code}
    except (urllib.error.URLError, OSError, ValueError):
        return False, _("Нет связи с Telegram. Проверьте интернет.")


def notify_server_error(path, error):
    """Ошибка сервера (500). Без подробностей запроса: там могут быть личные данные."""
    try:
        cafe = CafeSettings.get()
        if not is_configured(cafe):
            return False
        with _lang(cafe):
            lines = ["⚠️ " + _("Ошибка на сервере %(cafe)s") % {"cafe": cafe.name}]
            details = " · ".join(p for p in (path, error) if p)
            if details:
                lines.append(details)
            lines.append(_("Если повторяется, сообщите тому, кто обслуживает кассу."))
        if hub.connected(cafe):
            _dispatch(hub.send_event, cafe, "error", "\n".join(lines))
            return True
        args = (cafe.telegram_bot_token.strip(), cafe.telegram_chat_id.strip(), "\n".join(lines))
        if getattr(settings, "TELEGRAM_SYNC", False):
            _post(*args)
        else:
            threading.Thread(target=_safe_post, args=args, daemon=True).start()
        return True
    except Exception:  # оповещение об ошибке само не должно ничего ломать
        log.exception("Telegram: сообщение об ошибке сервера не собрано")
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

def _shift_profit(shift):
    """Выручка смены минус себестоимость проданного по складу. None, если себестоимость не ведётся."""
    from django.db.models import F, Sum
    from inventory.models import StockMovement

    rows = StockMovement.objects.filter(kind=StockMovement.Kind.SALE, order__shift=shift)
    cost = rows.aggregate(total=Sum(-F("quantity") * F("product__cost_per_unit")))["total"]
    if not cost:
        return None
    return shift.total_revenue - cost


def _waiters_block(shift):
    """Кто сколько заработал за смену и сколько ему осталось выплатить. Сбой расчёта сообщение не ломает."""
    try:
        from accounts import payouts
        from pos.models import Order

        rows = {}
        orders = shift.orders.filter(status=Order.Status.PAID).select_related("waiter").prefetch_related("items__modifiers")
        for order in orders:
            row = rows.setdefault(order.waiter_id, {"user": order.waiter, "n": 0, "earned": 0})
            row["n"] += 1
            row["earned"] += order.earned_by_waiter
        rows = [r for r in rows.values() if r["earned"]]
        if not rows:
            return []
        lines = ["", "👥 " + _("Официанты за смену:")]
        for r in sorted(rows, key=lambda r: -r["earned"]):
            lines.append(_("%(name)s: %(n)s заказов, заработал %(sum)s, к выплате всего %(left)s") % {
                "name": _person(r["user"]), "n": r["n"], "sum": money(r["earned"]),
                "left": money(payouts.balance(r["user"])),
            })
        return lines
    except Exception:
        log.exception("Telegram: блок официантов не собран")
        return []


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
            profit = _shift_profit(shift)
            if profit is not None:
                lines.append("📈 " + _("Прибыль по себестоимости: %(sum)s") % {"sum": money(profit)})
            if difference:
                sign = "+" if difference > 0 else "−"
                lines.append("⚠️ " + _("Расхождение: %(sign)s%(sum)s") % {"sign": sign, "sum": money(abs(difference))})
            else:
                lines.append("✅ " + _("Касса сошлась"))
            if shift.closing_comment:
                lines.append(_("Комментарий: %(text)s") % {"text": shift.closing_comment})
            lines += _waiters_block(shift)
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


def notify_juma_order(link):
    """Пришёл новый заказ доставки Juma: владелец видит сумму и номер, кассир принимает заказ на экране «Juma».
    Телефон и адрес покупателя в сообщение не попадают (бот общий, персональные данные не нужны)."""
    try:
        cafe = CafeSettings.get()
        count = link.order.items.count()
        with _lang(cafe):
            lines = [
                "🛵 " + _("Новый заказ Juma %(num)s · %(cafe)s") % {"num": link.number, "cafe": cafe.name},
                _("Сумма за товар: %(sum)s, позиций: %(n)s") % {"sum": money(link.items_total), "n": count},
                _("Откройте раздел «Juma» на кассе и нажмите «Принять»."),
            ]
        return send("\n".join(lines), "orders")
    except Exception:
        log.exception("Telegram: сообщение о заказе Juma не собрано")
        return False


def notify_juma_cancelled(link, reason):
    """Заказ Juma отменён не кассой (покупателем, по таймауту, администратором)."""
    try:
        cafe = CafeSettings.get()
        with _lang(cafe):
            reasons = {
                "CUSTOMER_CANCELLED": _("покупатель отменил заказ"),
                "PAYMENT_TIMEOUT": _("вышло время оплаты"),
                "MERCHANT_NO_RESPONSE": _("заведение не ответило вовремя"),
                "POS_UNREACHABLE": _("нет связи с кассой"),
                "NO_DRIVER": _("водитель не найден"),
                "ADMIN_CANCELLED": _("отменил администратор Juma"),
            }
            lines = [
                "❌ " + _("Заказ Juma %(num)s отменён · %(cafe)s") % {"num": link.number, "cafe": cafe.name},
                _("Сумма за товар: %(sum)s") % {"sum": money(link.items_total)},
                _("Причина: %(text)s") % {"text": reasons.get(reason) or _("не указана")},
            ]
            if link.payment_status == "REFUND_PENDING":
                lines.append(_("Заказ был оплачен: верните деньги покупателю и нажмите «Возврат выполнен»."))
        return send("\n".join(lines), "orders")
    except Exception:
        log.exception("Telegram: сообщение об отмене заказа Juma не собрано")
        return False


def notify_refund(order, user, reason):
    """Возврат оплаченного заказа: деньги уходят из кассы, владелец должен знать сразу."""
    try:
        cafe = CafeSettings.get()
        with _lang(cafe):
            lines = [
                "↩️ " + _("Возврат по чеку №%(id)s · %(cafe)s") % {"id": order.id, "cafe": cafe.name},
                _("Сумма: %(sum)s, %(place)s") % {"sum": money(order.total), "place": order.place_label},
                _("Кто оформил: %(name)s") % {"name": _person(user)},
                _("Причина: %(text)s") % {"text": reason or _("не указана")},
            ]
        return send("\n".join(lines), "cancel")
    except Exception:
        log.exception("Telegram: сообщение о возврате не собрано")
        return False


def notify_item_removed(order, item, user):
    """Блюдо, уже отправленное на кухню, убрали из заказа."""
    try:
        cafe = CafeSettings.get()
        with _lang(cafe):
            lines = [
                "🗑 " + _("Убрано блюдо с кухни · %(cafe)s") % {"cafe": cafe.name},
                _("Заказ №%(id)s, %(place)s") % {"id": order.id, "place": order.place_label},
                _("Блюдо: %(dish)s, %(sum)s") % {"dish": item.dish.name, "sum": money(item.line_total)},
                _("Кто убрал: %(name)s") % {"name": _person(user)},
            ]
        return send("\n".join(lines), "cancel")
    except Exception:
        log.exception("Telegram: сообщение об удалении блюда не собрано")
        return False


def notify_pin_locked(user):
    """Кто-то несколько раз подряд ввёл неверный пароль отмены."""
    try:
        cafe = CafeSettings.get()
        with _lang(cafe):
            lines = [
                "🔒 " + _("Пароль отмены заблокирован · %(cafe)s") % {"cafe": cafe.name},
                _("Сотрудник: %(name)s") % {"name": _person(user)},
                _("Несколько раз подряд введён неверный пароль. Если это не он, проверьте, кто работает на кассе."),
            ]
        return send("\n".join(lines), "cancel")
    except Exception:
        log.exception("Telegram: сообщение о блокировке не собрано")
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


def notify_review(review):
    """Низкие оценки (3 и ниже) владелец должен увидеть сразу, пока гость ещё рядом."""
    if review.rating > 3:
        return False
    try:
        cafe = CafeSettings.get()
        with _lang(cafe):
            order = review.order
            lines = [
                "⭐ " + _("Оценка %(r)s из 5 · %(cafe)s") % {"r": review.rating, "cafe": cafe.name},
                _("Заказ №%(id)s, %(place)s") % {"id": order.id, "place": order.place_label},
                _("Официант: %(name)s") % {"name": _person(order.waiter)},
            ]
            if review.comment:
                lines.append("«" + review.comment + "»")
        return send("\n".join(lines), "review")
    except Exception:
        log.exception("Telegram: сообщение об отзыве не собрано")
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


def notify_booking(booking):
    """Новая заявка на онлайн-бронь: администратор должен подтвердить её, пока гость ждёт ответа."""
    try:
        cafe = CafeSettings.get()
        with _lang(cafe):
            lines = [
                "🗓 " + _("Заявка на бронь · %(cafe)s") % {"cafe": cafe.name},
                _("%(d)s в %(t)s, гостей: %(n)s") % {"d": booking.date.strftime("%d.%m"), "t": booking.time.strftime("%H:%M"), "n": booking.guests},
                f"{booking.name}, +998 {booking.phone}",
            ]
            if booking.comment:
                lines.append("«" + booking.comment + "»")
        return send("\n".join(lines), "orders")
    except Exception:
        log.exception("Telegram: сообщение о брони не собрано")
        return False
