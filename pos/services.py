"""Бизнес-правила заказов: кухня, скидки, оплата. Вью и демо-данные используют только эти функции."""
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from core.models import CafeSettings
from inventory.services import write_off_order

from .models import Order, OrderItem

MAX_SUM = Decimal(1_000_000_000)


class PaymentError(Exception):
    pass


def parse_money(raw):
    """Сумма из поля ввода: «60 000», «60000», «60 000 сум» -> Decimal. Пусто -> None, мусор -> ошибка."""
    text = (raw or "").strip()
    if not text:
        return None
    cleaned = "".join(ch for ch in text if ch.isdigit() or ch in ".,").replace(",", ".")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        raise PaymentError(_("Введите сумму числом.")) from None
    if not value.is_finite() or value < 0 or value > MAX_SUM:
        raise PaymentError(_("Введите сумму числом."))
    return value.quantize(Decimal("1"))


# ---------- создание заказа ----------

def service_percent_for(order_type):
    """Обслуживание (сервисный сбор) начисляется только в зале."""
    return CafeSettings.get().service_percent if order_type == Order.OrderType.DINE_IN else Decimal(0)


def create_order(user, order_type, table=None):
    return Order.objects.create(
        order_type=order_type, table=table, waiter=user, service_percent=service_percent_for(order_type)
    )


def set_order_type(order, order_type, table=None):
    order.order_type = order_type
    order.table = table if order_type == Order.OrderType.DINE_IN else None
    order.service_percent = service_percent_for(order_type)
    if order_type != Order.OrderType.DELIVERY:
        order.delivery_address = ""
    order.save(update_fields=["order_type", "table", "service_percent", "delivery_address"])


# ---------- кухня ----------

def send_to_kitchen(order):
    """Неотправленные позиции уходят в очередь кухни. Возвращает число позиций."""
    return order.items.filter(status=OrderItem.Status.NEW).update(
        status=OrderItem.Status.QUEUED, sent_at=timezone.now()
    )


def start_cooking(order):
    return order.items.filter(status=OrderItem.Status.QUEUED).update(status=OrderItem.Status.COOKING)


def mark_items_ready(items_qs):
    return items_qs.filter(status__in=[OrderItem.Status.QUEUED, OrderItem.Status.COOKING]).update(
        status=OrderItem.Status.READY, ready_at=timezone.now()
    )


def serve_ready(order):
    return order.items.filter(status=OrderItem.Status.READY).update(status=OrderItem.Status.SERVED)


# ---------- скидка ----------

def apply_discount(order, kind, raw_value, reason=""):
    """Ставит или снимает скидку. Возвращает текст ошибки или пустую строку."""
    if kind == "":
        order.discount_kind, order.discount_value, order.discount_reason = "", 0, ""
        order.save(update_fields=["discount_kind", "discount_value", "discount_reason"])
        return ""
    if kind not in Order.DiscountKind.values:
        return _("Выберите вид скидки.")
    try:
        value = parse_money(raw_value)
    except PaymentError:
        value = None
    if value is None or value <= 0:
        return _("Введите размер скидки числом больше нуля.")
    if kind == Order.DiscountKind.PERCENT and value > 100:
        return _("Скидка не может быть больше 100%.")
    if kind == Order.DiscountKind.AMOUNT and value > order.subtotal:
        return _("Скидка больше суммы заказа.")
    order.discount_kind, order.discount_value, order.discount_reason = kind, value, reason.strip()[:100]
    order.save(update_fields=["discount_kind", "discount_value", "discount_reason"])
    return ""


# ---------- оплата ----------

def check_payment(due, method, received_raw, card_raw, allowed):
    """Проверяет ввод кассира. Возвращает (получено наличными, оплачено картой) или бросает PaymentError."""
    if method not in allowed:
        raise PaymentError(_("Выберите способ оплаты."))
    received = parse_money(received_raw)
    if method == Order.PaymentMethod.CARD:
        return None, None
    if method == Order.PaymentMethod.CASH:
        received = due if received is None else received
        if received < due:
            raise PaymentError(_("Полученная сумма меньше итога."))
        return received, None
    card = parse_money(card_raw)
    if card is None or card <= 0:
        raise PaymentError(_("Укажите, сколько оплачено картой."))
    if card >= due:
        raise PaymentError(_("Сумма по карте должна быть меньше итога. Для полной оплаты картой выберите «Карта»."))
    cash_due = due - card
    received = cash_due if received is None else received
    if received < cash_due:
        raise PaymentError(_("Наличных меньше, чем нужно доплатить."))
    return received, card


def _split_off(order, items, selected, method, received, card, cashier):
    """Переносит выбранные позиции в новый оплаченный заказ, остальное остаётся в открытом.
    Процентная скидка и обслуживание переходят в оплачиваемую часть, скидка суммой остаётся на остатке."""
    percent = order.discount_kind == Order.DiscountKind.PERCENT
    target = Order.objects.create(
        order_type=order.order_type,
        table=order.table,
        waiter=order.waiter,
        customer_name=order.customer_name,
        customer_phone=order.customer_phone,
        delivery_address=order.delivery_address,
        discount_kind=order.discount_kind if percent else "",
        discount_value=order.discount_value if percent else 0,
        discount_reason=order.discount_reason if percent else "",
        service_percent=order.service_percent,
        status=Order.Status.PAID,
        payment_method=method,
        cash_received=received,
        card_amount=card,
        cashier=cashier,
        closed_at=timezone.now(),
    )
    for item in items:
        qty = selected.get(item.id)
        if not qty:
            continue
        if qty == item.quantity:
            item.order = target
            item.save()
        else:
            part = OrderItem.objects.create(
                order=target, dish=item.dish, quantity=qty, price=item.price,
                comment=item.comment, status=item.status, sent_at=item.sent_at, ready_at=item.ready_at,
            )
            part.modifiers.set(item.modifiers.all())
            item.quantity -= qty
            item.save()
    return target


def pay_order(order, cashier, method, received, card, items=None, selected=None):
    """Закрывает заказ (или его часть) оплатой и списывает продукты по техкартам.
    Неотправленные позиции перед оплатой уходят на кухню: оплаченное должно быть приготовлено."""
    with transaction.atomic():
        locked = Order.objects.select_for_update().get(pk=order.pk)
        if locked.status != Order.Status.OPEN:
            raise PaymentError(_("Заказ уже закрыт."))
        send_to_kitchen(locked)
        if selected:
            items = [i for i in locked.items.select_related("dish").prefetch_related("modifiers")]
            target = _split_off(locked, items, selected, method, received, card, cashier)
        else:
            target = locked
            target.payment_method = method
            target.cash_received = received
            target.card_amount = card
            target.cashier = cashier
            target.status = Order.Status.PAID
            target.closed_at = timezone.now()
            target.save()
        write_off_order(target)
    return target


def cancel_order(order, user, reason):
    order.status = Order.Status.CANCELLED
    order.closed_at = timezone.now()
    order.cancelled_by = user
    order.cancel_reason = reason.strip()[:200]
    order.save(update_fields=["status", "closed_at", "cancelled_by", "cancel_reason"])
