"""Приём заказов Juma: заказ из облака становится обычным заказом «Доставка» на кассе (MERCHANT_API.md, раздел 3.1).

Цену, скидку и доплаты считает Juma, и покупатель согласился именно на неё, поэтому позиция берёт цену из заказа Juma.
Если блюда нет, его нет в продаже или сумма не сходится, заказ не создаётся и отклоняется (reject): лучше отказать сразу,
чем принять заказ, который нельзя выполнить. На кухню заказ уходит только после «Принять» (см. actions.py).
"""
import logging
from decimal import Decimal, InvalidOperation

from django.db import transaction

from core import telegram
from menu.models import Dish
from pos import services
from pos.models import Order, OrderItem

from . import client
from .models import JumaOrder, JumaState

log = logging.getLogger(__name__)


class Unfulfillable(Exception):
    """Заказ нельзя выполнить: code — причина для Juma (ITEM_UNAVAILABLE, PRICE_CHANGED), items — внешние id позиций."""

    def __init__(self, code, items=()):
        super().__init__(code)
        self.code, self.items = code, list(items)


class NoWaiter(Exception):
    """На кассе некому вести заказ (нет активных сотрудников). Заказ остаётся в Juma и будет принят позже."""


def _pk(external_id, prefix):
    head, _sep, tail = str(external_id).partition("-")
    if head != prefix or not tail.isdigit():
        raise Unfulfillable("ITEM_UNAVAILABLE", [external_id])
    return int(tail)


def _sum(value):
    """Сумма из Juma ('22000.00') -> целые сумы. Дробные суммы в CafePOS не бывают."""
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise Unfulfillable("PRICE_CHANGED") from None
    if amount != amount.to_integral_value():
        raise Unfulfillable("PRICE_CHANGED")
    return amount.quantize(Decimal(1))


def import_order(data):
    """Создаёт заказ «Доставка» из заказа Juma. Повторный вызов с тем же заказом возвращает уже созданную связку."""
    existing = JumaOrder.objects.filter(juma_id=data["id"]).first()
    if existing:
        return existing
    waiter = services._guest_order_waiter()
    if waiter is None:
        raise NoWaiter
    customer = data.get("customer") or {}
    with transaction.atomic():
        order = Order.objects.create(
            order_type=Order.OrderType.DELIVERY, waiter=waiter, service_percent=0,
            customer_name=(customer.get("name") or "")[:100], customer_phone=(customer.get("phone") or "")[:30],
            delivery_address=f"Juma {data['number']}, код {data['pickup_code']}",
            note=(data.get("comment") or "")[:255],
        )
        for line in data["items"]:
            ext = line["external_id"]
            dish = Dish.objects.filter(pk=_pk(ext, "d"), is_hidden=False).first()
            if dish is None or not dish.is_available or dish.sold_by_weight:
                raise Unfulfillable("ITEM_UNAVAILABLE", [ext])
            qty = Decimal(str(line["qty"]))
            if qty != qty.to_integral_value() or qty < 1:
                raise Unfulfillable("ITEM_UNAVAILABLE", [ext])
            item = OrderItem.objects.create(
                order=order, dish=dish, quantity=int(qty), price=_sum(line["unit_price"]), comment=(line.get("comment") or "")[:200])
            modifiers = []
            for mod in line.get("modifiers", []):
                modifier = dish.modifiers.filter(pk=_pk(mod["external_id"], "m")).first()
                if modifier is None:
                    raise Unfulfillable("ITEM_UNAVAILABLE", [ext])
                modifiers.append(modifier)
            item.modifiers.set(modifiers)
        total = _sum(data["items_total"])
        if order.totals["total"] != total:
            raise Unfulfillable("PRICE_CHANGED")
        return JumaOrder.objects.create(
            order=order, juma_id=data["id"], number=data["number"], pickup_code=data["pickup_code"],
            payment_status=(data.get("payment") or {}).get("status", JumaOrder.Payment.WAITING_TRANSFER),
            items_total=total)


def intake_once():
    """Забирает новые заказы из Juma, создаёт их на кассе и подтверждает получение (ack). True — связь с Juma есть."""
    if not client.enabled():
        return False
    code, body = client.pending_orders(0)
    if not client.ok(code):
        JumaState.objects.filter(pk=1).update(last_error=client.reason(code, body)[:255])
        return False
    for data in body.get("orders", []):
        try:
            link = import_order(data)
        except Unfulfillable as exc:
            log.warning("Juma: заказ %s отклонён (%s)", data.get("number"), exc.code)
            client.reject_order(data["id"], exc.code, exc.items)
            continue
        except NoWaiter:
            log.warning("Juma: заказ %s не принят, на кассе нет активных сотрудников", data.get("number"))
            continue
        if link.status == JumaOrder.Status.PLACED:
            ack_code, _ack_body = client.ack_order(data["id"])
            if client.ok(ack_code):
                link.status = JumaOrder.Status.SENT
                link.save(update_fields=["status"])
                # Владельцу: один раз, когда заказ принят из облака (повторный опрос после сбоя ack не дублирует).
                telegram.notify_juma_order(link)
    return True
