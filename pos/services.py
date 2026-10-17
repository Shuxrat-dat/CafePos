"""Бизнес-правила заказов: кухня, скидки, оплата. Вью и демо-данные используют только эти функции."""
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from core import audit
from core.models import CafeSettings
from inventory.services import write_off_order

from . import loyalty
from .models import CashMovement, CashShift, Order, OrderItem, Payment

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


# ---------- кассовые смены ----------

def get_current_shift():
    """Возвращает текущую открытую кассовую смену или None."""
    return CashShift.objects.filter(status=CashShift.Status.OPEN).select_related("cashier").first()


def open_shift(user, initial_cash=0):
    """Открывает новую кассовую смену."""
    existing = get_current_shift()
    if existing:
        raise PaymentError(_("Кассовая смена уже открыта (№%(id)s).") % {"id": existing.id})
    try:
        initial = Decimal(initial_cash or 0).quantize(Decimal("1"))
    except (InvalidOperation, TypeError):
        initial = Decimal(0)
    if initial < 0:
        raise PaymentError(_("Начальный остаток не может быть отрицательным."))
    audit.record(user, audit.A.SHIFT_OPEN, amount=initial, detail=_("Начальный остаток"))
    return CashShift.objects.create(
        cashier=user,
        initial_cash=initial,
        status=CashShift.Status.OPEN,
    )


def close_shift(shift, actual_cash, comment="", user=None):
    """Сверяет кассу, закрывает смену и фиксирует расхождение."""
    with transaction.atomic():
        locked = CashShift.objects.select_for_update().get(pk=shift.pk)
        if locked.status == CashShift.Status.CLOSED:
            return locked
        try:
            actual = Decimal(actual_cash).quantize(Decimal("1"))
        except (InvalidOperation, TypeError):
            raise PaymentError(_("Введите фактическую сумму наличных числом."))
        if actual < 0:
            raise PaymentError(_("Фактическая сумма не может быть отрицательной."))
        locked.actual_cash = actual
        locked.closing_comment = (comment or "").strip()[:255]
        locked.status = CashShift.Status.CLOSED
        locked.closed_at = timezone.now()
        locked.save()
    audit.record(
        user, audit.A.SHIFT_CLOSE, amount=locked.difference or 0,
        detail=_("Смена №%(id)s, по факту %(fact)s, расхождение %(diff)s") % {
            "id": locked.id, "fact": actual, "diff": locked.difference or 0},
    )
    return locked


def add_cash_movement(shift, kind, amount_raw, reason="", user=None):
    """Внесение или изъятие наличных в открытой смене."""
    if shift.status != CashShift.Status.OPEN:
        raise PaymentError(_("Нельзя вносить или изымать средства в закрытой смене."))
    amount = parse_money(amount_raw)
    if not amount or amount <= 0:
        raise PaymentError(_("Введите сумму операции больше нуля."))
    if kind == CashMovement.Kind.WITHDRAWAL and amount > shift.expected_cash:
        raise PaymentError(
            _("Сумма изъятия (%(amount)s сум) превышает ожидаемый остаток в кассе (%(expected)s сум).") % {
                "amount": amount,
                "expected": shift.expected_cash,
            }
        )
    movement = CashMovement.objects.create(
        shift=shift,
        kind=kind,
        amount=amount,
        reason=(reason or "").strip()[:255],
        created_by=user,
    )
    audit.record(user, audit.A.CASH_MOVE, amount=amount, detail=f"{movement.get_kind_display()}: {movement.reason}")
    return movement


def _split_off(order, items, selected, method, received, card, cashier, shift=None):
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
        shift=shift,
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


def pay_order(order, cashier, method, received, card, items=None, selected=None, idempotency_key=None):
    """Закрывает заказ (или его часть) оплатой, регистрирует Payment в текущей смене и списывает продукты."""
    # Серверная защита от повторных списаний (идемпотентность)
    if idempotency_key:
        existing_payment = Payment.objects.filter(idempotency_key=idempotency_key).select_related("order").first()
        if existing_payment:
            return existing_payment.order

    with transaction.atomic():
        locked = Order.objects.select_for_update().get(pk=order.pk)
        if locked.status != Order.Status.OPEN:
            raise PaymentError(_("Заказ уже закрыт."))

        shift = get_current_shift()
        if not shift:
            shift = open_shift(cashier, initial_cash=0)

        send_to_kitchen(locked)
        if selected:
            items = [i for i in locked.items.select_related("dish").prefetch_related("modifiers")]
            target = _split_off(locked, items, selected, method, received, card, cashier, shift=shift)
        else:
            target = locked
            target.payment_method = method
            target.cash_received = received
            target.card_amount = card
            target.cashier = cashier
            target.shift = shift
            target.status = Order.Status.PAID
            target.closed_at = timezone.now()
            target.save()

        # Расчёт сумм платежа
        total = target.total
        if method == Order.PaymentMethod.CASH:
            cash_amount = total
            card_amount = Decimal(0)
            change = (received - total) if received is not None else Decimal(0)
        elif method == Order.PaymentMethod.CARD:
            cash_amount = Decimal(0)
            card_amount = total
            change = Decimal(0)
        else:  # MIXED
            card_amount = card or Decimal(0)
            cash_amount = total - card_amount
            change = (received - cash_amount) if received is not None else Decimal(0)

        Payment.objects.create(
            order=target,
            shift=shift,
            payment_method=method,
            amount=total,
            cash_amount=cash_amount,
            card_amount=card_amount,
            cash_received=received,
            change=max(Decimal(0), change),
            cashier=cashier,
            idempotency_key=idempotency_key or None,
            is_refund=False,
        )

        write_off_order(target)
        loyalty.earn(target)
    return target


def cancel_order(order, user, reason):
    """Отменяет открытый заказ."""
    order.status = Order.Status.CANCELLED
    order.closed_at = timezone.now()
    order.cancelled_by = user
    order.cancel_reason = reason.strip()[:200]
    order.save(update_fields=["status", "closed_at", "cancelled_by", "cancel_reason"])
    if order.items.exists():  # пустые черновики в журнал не пишем
        audit.record(user, audit.A.ORDER_CANCEL, order=order, amount=order.totals["total"],
                     detail=order.cancel_reason or _("причина не указана"))


def refund_order(order, user, reason, return_to_stock=True):
    """Оформляет возврат ранее оплаченного заказа."""
    with transaction.atomic():
        locked = Order.objects.select_for_update().get(pk=order.pk)
        if locked.status != Order.Status.PAID:
            raise PaymentError(_("Возврат возможен только для оплаченных заказов."))

        shift = get_current_shift()
        if not shift:
            shift = open_shift(user, initial_cash=0)

        refund_reason = (reason or "").strip()[:200]
        if not refund_reason:
            raise PaymentError(_("Укажите причину возврата."))

        cash_refund = locked.cash_amount
        card_refund = locked.card_paid

        Payment.objects.create(
            order=locked,
            shift=shift,
            payment_method=locked.payment_method,
            amount=locked.total,
            cash_amount=cash_refund,
            card_amount=card_refund,
            cashier=user,
            is_refund=True,
            refund_reason=refund_reason,
        )

        locked.status = Order.Status.CANCELLED
        locked.cancelled_by = user
        locked.cancel_reason = f"Возврат: {refund_reason}"
        locked.save(update_fields=["status", "cancelled_by", "cancel_reason"])

        if return_to_stock:
            from inventory.models import RecipeItem, StockMovement
            for item in locked.items.select_related("dish").prefetch_related("modifiers"):
                factor = Decimal(1)
                for modifier in item.modifiers.all():
                    factor *= modifier.consumption_multiplier
                for recipe in RecipeItem.objects.filter(dish=item.dish).select_related("product"):
                    StockMovement.objects.create(
                        product=recipe.product,
                        kind=StockMovement.Kind.INCOME,
                        quantity=recipe.quantity * item.quantity * factor,
                        order=locked,
                        comment=f"Возврат по чеку #{locked.id}: {refund_reason}",
                        created_by=user,
                    )

    loyalty.reverse(locked)
    audit.record(user, audit.A.REFUND, order=locked, amount=locked.total, detail=refund_reason)
    return locked
