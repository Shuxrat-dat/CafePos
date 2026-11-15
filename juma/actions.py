"""Действия кассира над заказами Juma (MERCHANT_API.md, разделы 3.2-3.4).

Каждое действие сначала сообщается в Juma и только если она приняла, меняется заказ на кассе: так касса и облако не
расходятся. Возвращают (успех, текст для кассира).
"""
import logging

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from pos import services
from pos.models import Order

from . import client
from .models import JumaOrder

log = logging.getLogger(__name__)
S = JumaOrder.Status


def _conflict_text(code, body):
    """Понятный текст отказа. 409: заказ уже изменился (отменён покупателем и т. п.), статус обновит синхронизация."""
    if code == 409:
        details = ((body or {}).get("error") or {}).get("details") or {}
        if details.get("reason") == "NOT_PAID":
            return _("Заказ не оплачен.")
        if details.get("reason") == "PICKUP_CODE_MISMATCH":
            return _("Неверный код выдачи.")
        return _("Состояние заказа изменилось, обновите страницу.")
    return client.reason(code, body)


def accept(link, ready_minutes=None):
    """«Принять»: заведение берёт заказ и называет время готовности. Блюда уходят на кухню и на кухонные принтеры."""
    if link.status not in (S.PLACED, S.SENT):
        return False, _("Заказ уже принят или отменён.")
    payload = {"status": "ACCEPTED"}
    if ready_minutes:
        payload["ready_in_minutes"] = int(ready_minutes)
    code, body = client.set_status(link.juma_id, payload)
    if not client.ok(code):
        return False, _conflict_text(code, body)
    with transaction.atomic():
        services.send_to_kitchen(link.order)
        link.status, link.accepted_at = S.ACCEPTED, timezone.now()
        link.save(update_fields=["status", "accepted_at"])
    return True, ""


def mark_paid(link):
    """«Оплачено»: кассир увидел перевод на карту заведения."""
    code, body = client.set_payment(link.juma_id, {"status": "PAID", "amount": str(link.items_total)})
    if not client.ok(code):
        return False, _conflict_text(code, body)
    link.payment_status = JumaOrder.Payment.PAID
    link.save(update_fields=["payment_status"])
    return True, ""


def mark_ready(link):
    """«Готов»: блюда готовы, водителя можно вызывать."""
    code, body = client.set_status(link.juma_id, {"status": "READY"})
    if not client.ok(code):
        return False, _conflict_text(code, body)
    services.mark_items_ready(link.order.items.all())
    link.status = S.READY
    link.save(update_fields=["status"])
    return True, ""


def hand_over(link, cashier, pickup_code):
    """«Передан водителю»: проверяем оплату и код, сообщаем Juma, затем проводим оплату на кассе (перевод как оплата картой)."""
    if link.payment_status != JumaOrder.Payment.PAID:
        return False, _("Заказ не оплачен.")
    if (pickup_code or "").strip() != link.pickup_code:
        return False, _("Неверный код выдачи.")
    if not services.get_current_shift():
        return False, services.no_shift_message()
    code, body = client.set_status(link.juma_id, {"status": "HANDED_OVER", "pickup_code": link.pickup_code})
    if not client.ok(code):
        return False, _conflict_text(code, body)
    link.status = S.PICKED_UP
    link.save(update_fields=["status"])
    order = link.order
    try:
        services.pay_order(order, cashier, Order.PaymentMethod.CARD, None, order.totals["total"], idempotency_key=f"juma-{link.juma_id}")
    except services.PaymentError as exc:
        # Juma уже знает о выдаче: откатить нельзя. Оплату на кассе придётся провести вручную.
        log.exception("Juma: заказ %s передан, но оплата на кассе не проведена", link.number)
        return True, _("Заказ передан, но оплату на кассе проведите вручную: %(why)s") % {"why": exc}
    return True, ""


REASONS = ("MERCHANT_CANCELLED", "OUT_OF_STOCK", "PRICE_CHANGED", "ITEM_UNAVAILABLE", "CLOSED", "OTHER")


def cancel(link, user, reason="MERCHANT_CANCELLED"):
    """«Отменить»: заведение не может выполнить заказ. Оплаченный заказ уходит на возврат (деньги возвращает заведение)."""
    if link.status in (S.PICKED_UP, S.DELIVERED):
        return False, _("Заказ уже передан водителю.")
    if link.status == S.CANCELLED:
        return True, ""
    reason = reason if reason in REASONS else "MERCHANT_CANCELLED"
    code, body = client.set_status(link.juma_id, {"status": "CANCELLED", "reason": reason})
    if not client.ok(code):
        return False, _conflict_text(code, body)
    mark_cancelled(link, user, reason)
    return True, ""


def mark_cancelled(link, user, reason):
    """Отмена заказа на кассе и в связке (после того как Juma об этом знает или сама сообщила)."""
    with transaction.atomic():
        if link.order.status == Order.Status.OPEN:
            services.cancel_order(link.order, user, f"Juma: {reason}")
        if link.payment_status == JumaOrder.Payment.PAID:
            link.payment_status = JumaOrder.Payment.REFUND_PENDING
        link.status, link.cancel_reason = S.CANCELLED, reason[:100]
        link.save(update_fields=["status", "payment_status", "cancel_reason"])


def confirm_refund(link):
    """«Возврат выполнен»: заведение вернуло перевод покупателю."""
    code, body = client.set_payment(link.juma_id, {"status": "REFUNDED"})
    if not client.ok(code):
        return False, _conflict_text(code, body)
    link.payment_status = JumaOrder.Payment.REFUNDED
    link.save(update_fields=["payment_status"])
    return True, ""
