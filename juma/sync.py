"""Синхронизация состояния заказов Juma: события из облака и состояние кухни на кассе."""
import logging

from core import telegram
from pos.models import OrderItem

from . import actions, client
from .models import JumaOrder, JumaState

log = logging.getLogger(__name__)
S = JumaOrder.Status
ORDER_STATUSES = {s.value for s in S}


def _apply_event(link, event):
    kind, to = event["type"], event.get("to")
    if kind == "CANCELLED":
        # Отмену заведением мы уже провели сами; важны отмены покупателем, системой (таймаут) и администратором
        if link.status != S.CANCELLED:
            reason = (event.get("details") or {}).get("reason", "OTHER")
            actions.mark_cancelled(link, None, reason)
            telegram.notify_juma_cancelled(link, reason)
        return
    if kind == "PAYMENT_PAID":
        link.payment_status = JumaOrder.Payment.PAID
    elif kind == "REFUND_PENDING":
        link.payment_status = JumaOrder.Payment.REFUND_PENDING
    elif kind == "REFUNDED":
        link.payment_status = JumaOrder.Payment.REFUNDED
    elif to in ORDER_STATUSES and link.status != S.CANCELLED:
        link.status = to
    link.save(update_fields=["status", "payment_status"])


def events_once():
    """Читает новые события заказов после сохранённого курсора. True — связь с Juma есть."""
    if not client.enabled():
        return False
    state = JumaState.get()
    code, body = client.events(state.events_cursor, 0)
    if not client.ok(code):
        JumaState.objects.filter(pk=1).update(last_error=client.reason(code, body)[:255])
        return False
    for event in body.get("events", []):
        link = JumaOrder.objects.select_related("order").filter(juma_id=event["order_id"]).first()
        if link is not None:
            try:
                _apply_event(link, event)
            except Exception:
                log.exception("Juma: не удалось применить событие %s", event.get("cursor"))
    cursor = int(body.get("next_cursor", state.events_cursor))
    if cursor > state.events_cursor:
        JumaState.objects.filter(pk=1).update(events_cursor=cursor)
    return True


def kitchen_once():
    """Кухня отметила блюда: сообщаем Juma «Готовится» и «Готов» (если кассир не нажал сам)."""
    if not client.enabled():
        return False
    for link in JumaOrder.objects.select_related("order").filter(status__in=[S.ACCEPTED, S.PREPARING]):
        statuses = set(link.order.items.values_list("status", flat=True))
        if not statuses:
            continue
        if statuses <= {OrderItem.Status.READY, OrderItem.Status.SERVED}:
            actions.mark_ready(link)
        elif link.status == S.ACCEPTED and statuses & {OrderItem.Status.COOKING, OrderItem.Status.READY}:
            code, _body = client.set_status(link.juma_id, {"status": "PREPARING"})
            if client.ok(code):
                link.status = S.PREPARING
                link.save(update_fields=["status"])
    return True
