"""Бизнес-правила заказов: кухня, скидки, оплата. Вью и демо-данные используют только эти функции."""
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone
from django.utils.translation import gettext as _

from core import audit
from core.models import CafeSettings
from inventory.services import write_off_order

from . import loyalty
from .models import CashMovement, CashShift, Customer, DebtEntry, Deposit, Order, OrderItem, Payment

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

def service_percent_for(order_type, table=None):
    """Обслуживание (сервисный сбор) начисляется только в зале: по ставке зала стола,
    а если у зала она не задана — по общей ставке заведения."""
    if order_type != Order.OrderType.DINE_IN:
        return Decimal(0)
    if table is not None and table.hall.service_percent is not None:
        return table.hall.service_percent
    return CafeSettings.get().service_percent


def create_order(user, order_type, table=None):
    return Order.objects.create(
        order_type=order_type, table=table, waiter=user, service_percent=service_percent_for(order_type, table)
    )


def set_order_type(order, order_type, table=None):
    order.order_type = order_type
    order.table = table if order_type == Order.OrderType.DINE_IN else None
    order.service_percent = service_percent_for(order_type, order.table)
    if order_type != Order.OrderType.DELIVERY:
        order.delivery_address = ""
    order.save(update_fields=["order_type", "table", "service_percent", "delivery_address"])


# ---------- кухня ----------

def send_to_kitchen(order, item_ids=None):
    """Неотправленные позиции уходят в очередь кухни и на кухонные принтеры. Возвращает число позиций.
    item_ids ограничивает отправку выбранными позициями (заказ гостя не трогает черновик официанта)."""
    from printing import services as printing

    new_items = order.items.filter(status=OrderItem.Status.NEW)
    if item_ids is not None:
        new_items = new_items.filter(id__in=item_ids)
    ids = list(new_items.values_list("id", flat=True))
    if not ids:
        return 0
    count = order.items.filter(id__in=ids, status=OrderItem.Status.NEW).update(
        status=OrderItem.Status.QUEUED, sent_at=timezone.now()
    )
    printing.after_commit(printing.queue_kitchen, order, ids)
    return count


def _guest_order_waiter():
    """Кто ведёт заказ гостя, если за столом ещё нет заказа: самый свободный из активных официантов."""
    from accounts.models import User

    active = User.objects.filter(is_active=True)
    waiter = (
        active.filter(role=User.Role.WAITER)
        .annotate(busy=Count("orders", filter=Q(orders__status=Order.Status.OPEN)))
        .order_by("busy", "id")
        .first()
    )
    # Нет официантов (например, кафе из одного человека): заказ берёт тот, кто может им управлять
    return waiter or active.exclude(role=User.Role.COOK).order_by("id").first()


@transaction.atomic
def place_guest_order(table, lines):
    """Заказ гостя со стола сразу попадает в заказ стола и на кухню.
    lines — список (блюдо, количество). Возвращает (заказ, созданные позиции) или None, если принять нельзя."""
    order = (
        Order.objects.filter(table=table, status=Order.Status.OPEN, order_type=Order.OrderType.DINE_IN)
        .annotate(n_items=Count("items"))
        .order_by("-n_items", "id")
        .first()
    )
    if order is None:
        waiter = _guest_order_waiter()
        if waiter is None:
            return None
        order = create_order(waiter, Order.OrderType.DINE_IN, table)
        order.note = "QR"
        order.save(update_fields=["note"])
    created = []
    for dish, quantity in lines:
        if not dish.can_order:
            continue
        # Вес гость не знает: каждая порция отдельной позицией, вес укажет официант
        parts = [1] * quantity if dish.sold_by_weight else [quantity]
        for part in parts:
            created.append(OrderItem.objects.create(order=order, dish=dish, quantity=part))
    if not created:
        return None
    if table.is_reserved:
        table.reserved_for, table.reserved_at = "", None
        table.save(update_fields=["reserved_for", "reserved_at"])
    if order.bill_requested_at:
        # Состав изменился: прежний пречек больше не актуален
        order.bill_requested_at = None
        order.save(update_fields=["bill_requested_at"])
    send_to_kitchen(order, [item.id for item in created])
    return order, created


def start_cooking(order):
    return order.items.filter(status=OrderItem.Status.QUEUED).update(status=OrderItem.Status.COOKING)


def mark_items_ready(items_qs):
    return items_qs.filter(status__in=[OrderItem.Status.QUEUED, OrderItem.Status.COOKING]).update(
        status=OrderItem.Status.READY, ready_at=timezone.now()
    )


def serve_ready(order):
    return order.items.filter(status=OrderItem.Status.READY).update(status=OrderItem.Status.SERVED)


# ---------- скидка ----------

def apply_discount(order, kind, raw_value, reason="", max_percent=None):
    """Ставит или снимает скидку. Возвращает текст ошибки или пустую строку.
    max_percent — предел для кассира (сумма пересчитывается в процент от заказа)."""
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
    if max_percent is not None:
        percent = value if kind == Order.DiscountKind.PERCENT else value * 100 / order.subtotal
        if percent > max_percent:
            return _("Скидка больше %(n)s%% — её может дать только владелец или администратор.") % {"n": max_percent}
    order.discount_kind, order.discount_value, order.discount_reason = kind, value, reason.strip()[:100]
    order.save(update_fields=["discount_kind", "discount_value", "discount_reason"])
    return ""


# ---------- оплата ----------

def check_payment(due, method, received_raw, card_raw, allowed):
    """Проверяет ввод кассира. Возвращает (получено наличными, оплачено картой) или бросает PaymentError."""
    if method not in allowed:
        raise PaymentError(_("Выберите способ оплаты."))
    received = parse_money(received_raw)
    if method in (Order.PaymentMethod.CARD, Order.PaymentMethod.DEBT):
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

def no_shift_message():
    return _("Кассовая смена не открыта. Откройте смену и укажите, сколько денег в кассе.")


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
                by_weight=item.by_weight, weight_grams=item.weight_grams, comment=item.comment, status=item.status, sent_at=item.sent_at, ready_at=item.ready_at,
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
        if any(item.needs_weight for item in locked.items.all()):
            raise PaymentError(_("Укажите вес у блюд на вес, потом принимайте оплату."))

        shift = get_current_shift()
        if not shift:
            # Смену открывают явно, с пересчитанным остатком в кассе, иначе наличные не с чем сверять
            raise PaymentError(no_shift_message())

        debt_phone = ""
        if method == Order.PaymentMethod.DEBT:
            # В долг записываем только весь заказ и только на гостя с именем и телефоном
            from .loyalty import normalize_phone
            debt_phone = normalize_phone(locked.customer_phone)
            if selected:
                raise PaymentError(_("В долг можно записать только весь заказ, без деления счёта."))
            if not debt_phone or not (locked.customer_name or "").strip():
                raise PaymentError(_("Для записи в долг укажите имя и телефон гостя."))

        send_to_kitchen(locked)
        if selected:
            items = [i for i in locked.items.select_related("dish").prefetch_related("modifiers")]
            target = _split_off(locked, items, selected, method, received, card, cashier, shift=shift)
        else:
            target = locked
            # Аренда кабины фиксируется на момент оплаты
            target.room_minutes, target.room_charge = locked.room_live()
            if not target.room_minutes:
                target.room_minutes = target.room_charge = None
            target.payment_method = method
            target.cash_received = received
            target.card_amount = card
            target.cashier = cashier
            target.shift = shift
            target.status = Order.Status.PAID
            target.closed_at = timezone.now()
            target.save()

        # Доля официанта фиксируется сейчас: потом смена процента старые заказы не пересчитывает
        target.fix_waiter_earning()
        target.save(update_fields=["waiter_percent", "waiter_earning"])

        # Расчёт сумм платежа
        total = target.total
        # Депозит брони уже оплачен гостем: платится только остаток. Лишний депозит возвращается
        deposit = target.deposit_applied or Decimal(0)
        if deposit > total:
            leftover = deposit - total
            target.deposit_applied = deposit = total
            target.save(update_fields=["deposit_applied"])
            last = target.deposits.filter(status=Deposit.Status.APPLIED).order_by("-id").first()
            if last:
                last.refund_amount += leftover
                last.refund_shift = shift
                last.save(update_fields=["refund_amount", "refund_shift"])
        collect = total - deposit
        if method == Order.PaymentMethod.CASH:
            cash_amount = collect
            card_amount = Decimal(0)
            change = (received - collect) if received is not None else Decimal(0)
        elif method == Order.PaymentMethod.CARD:
            cash_amount = Decimal(0)
            card_amount = collect
            change = Decimal(0)
        elif method == Order.PaymentMethod.DEBT:
            # Денег в кассу не поступило: сумма уходит в долг гостя
            cash_amount = card_amount = change = Decimal(0)
        else:  # MIXED
            card_amount = card or Decimal(0)
            cash_amount = collect - card_amount
            change = (received - cash_amount) if received is not None else Decimal(0)

        Payment.objects.create(
            order=target,
            shift=shift,
            payment_method=method,
            amount=collect,
            cash_amount=cash_amount,
            card_amount=card_amount,
            cash_received=received,
            change=max(Decimal(0), change),
            cashier=cashier,
            idempotency_key=idempotency_key or None,
            is_refund=False,
        )

        if debt_phone:
            customer, _created = Customer.objects.select_for_update().get_or_create(
                phone=debt_phone, defaults={"name": target.customer_name.strip()[:100]}
            )
            DebtEntry.objects.create(
                customer=customer, order=target, amount=collect, shift=shift, cashier=cashier,
                comment=_("Заказ №%(id)s в долг") % {"id": target.id},
            )

        write_off_order(target)
        loyalty.earn(target)

        from printing import services as printing
        printing.after_commit(printing.queue_receipt, target, auto=True)
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
            raise PaymentError(no_shift_message())

        refund_reason = (reason or "").strip()[:200]
        if not refund_reason:
            raise PaymentError(_("Укажите причину возврата."))

        cash_refund = locked.cash_amount
        card_refund = locked.card_paid

        Payment.objects.create(
            order=locked,
            shift=shift,
            payment_method=locked.payment_method,
            amount=locked.total - (locked.deposit_applied or 0),
            cash_amount=cash_refund,
            card_amount=card_refund,
            cashier=user,
            is_refund=True,
            refund_reason=refund_reason,
        )

        locked.status = Order.Status.CANCELLED
        locked.cancelled_by = user
        locked.cancel_reason = _("Возврат: %(r)s") % {"r": refund_reason}
        locked.save(update_fields=["status", "cancelled_by", "cancel_reason"])

        # Заказ был в долг: деньги не возвращаем, а снимаем долг с гостя
        for entry in DebtEntry.objects.filter(order=locked, amount__gt=0):
            DebtEntry.objects.create(
                customer=entry.customer, order=locked, amount=-entry.amount, shift=shift, cashier=user,
                comment=_("Возврат по заказу №%(id)s: %(r)s") % {"id": locked.id, "r": refund_reason},
            )

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
                        quantity=recipe.quantity * item.quantity * item.weight_factor * factor,
                        order=locked,
                        comment=_("Возврат по чеку №%(id)s: %(r)s") % {"id": locked.id, "r": refund_reason},
                        created_by=user,
                    )

    loyalty.reverse(locked)
    audit.record(user, audit.A.REFUND, order=locked, amount=locked.total, detail=refund_reason)
    return locked


# ---------- долги гостей ----------

def repay_debt(customer, amount, method, cashier, comment=""):
    """Гость гасит долг наличными или картой. Деньги идут в текущую смену, поэтому без смены нельзя.
    Больше остатка долга принять нельзя: переплата запутала бы кассу."""
    if method not in DebtEntry.Method.values:
        raise PaymentError(_("Выберите способ оплаты."))
    with transaction.atomic():
        locked = Customer.objects.select_for_update().get(pk=customer.pk)
        shift = get_current_shift()
        if not shift:
            raise PaymentError(no_shift_message())
        if amount is None or amount <= 0:
            raise PaymentError(_("Введите сумму числом больше нуля."))
        debt = locked.debt
        if amount > debt:
            raise PaymentError(_("Сумма больше долга гостя (%(d)s сум).") % {"d": f"{debt:,.0f}".replace(",", " ")})
        return DebtEntry.objects.create(
            customer=locked, amount=-amount, method=method, shift=shift, cashier=cashier,
            comment=(comment or "").strip()[:200] or _("Погашение долга"),
        )


# ---------- перенос блюд и объединение столов ----------

def move_items(source, target, selection):
    """Переносит позиции из одного открытого заказа в другой. selection: {id позиции: сколько перенести}.
    Часть количества переносится копией позиции с той же ценой, модификаторами и стадией кухни.
    Если в исходном заказе ничего не осталось, он отменяется как объединённый. Возвращает число перенесённых штук."""
    if source.pk == target.pk:
        raise PaymentError(_("Выберите другой заказ."))
    with transaction.atomic():
        locked = {o.pk: o for o in Order.objects.select_for_update().filter(pk__in=[source.pk, target.pk])}
        src, dst = locked.get(source.pk), locked.get(target.pk)
        if not src or not dst or src.status != Order.Status.OPEN or dst.status != Order.Status.OPEN:
            raise PaymentError(_("Оба заказа должны быть открыты."))
        moved = 0
        for item in src.items.prefetch_related("modifiers").filter(id__in=selection.keys()):
            qty = min(int(selection[item.id]), item.quantity)
            if qty <= 0:
                continue
            if qty == item.quantity:
                item.order = dst
                item.save(update_fields=["order"])
            else:
                item.quantity -= qty
                item.save(update_fields=["quantity"])
                copy = OrderItem.objects.create(
                    order=dst, dish=item.dish, quantity=qty, price=item.price, by_weight=item.by_weight,
                    weight_grams=item.weight_grams, comment=item.comment, promo_label=item.promo_label,
                    status=item.status, sent_at=item.sent_at, ready_at=item.ready_at,
                )
                copy.modifiers.set(item.modifiers.all())
            moved += qty
        if not moved:
            raise PaymentError(_("Выберите хотя бы одну позицию."))
        for order in (src, dst):
            if order.bill_requested_at:
                order.bill_requested_at = None
                order.save(update_fields=["bill_requested_at"])
        if not src.items.exists():
            src.status = Order.Status.CANCELLED
            src.closed_at = timezone.now()
            src.cancel_reason = _("Объединён с заказом №%(id)s") % {"id": dst.id}
            src.save(update_fields=["status", "closed_at", "cancel_reason"])
    return moved


# ---------- депозиты броней ----------

MAX_DEPOSIT = Decimal(100_000_000)


def held_deposits(table):
    return list(Deposit.objects.filter(table=table, status=Deposit.Status.HELD))


def take_deposit(table, guest_name, amount, method, cashier):
    """Принимает депозит при брони. Деньги попадают в текущую смену: без открытой смены принять нельзя."""
    if method not in Deposit.Method.values:
        raise PaymentError(_("Выберите способ оплаты."))
    if amount is None or amount <= 0 or amount > MAX_DEPOSIT:
        raise PaymentError(_("Введите депозит числом больше нуля."))
    shift = get_current_shift()
    if not shift:
        raise PaymentError(no_shift_message())
    return Deposit.objects.create(
        table=table, guest_name=(guest_name or "")[:100], amount=amount, method=method, shift=shift, cashier=cashier,
    )


def settle_deposits(table, action, cashier):
    """Бронь снята без прихода гостя: депозит возвращается из кассы (refund) или остаётся заведению (keep)."""
    held = held_deposits(table)
    if not held:
        return 0
    with transaction.atomic():
        shift = get_current_shift()
        if action == "refund" and not shift:
            raise PaymentError(no_shift_message())
        for deposit in held:
            if action == "refund":
                deposit.status = Deposit.Status.REFUNDED
                deposit.refund_amount = deposit.amount
                deposit.refund_shift = shift
            elif action == "keep":
                deposit.status = Deposit.Status.KEPT
            else:
                raise PaymentError(_("Выберите, что сделать с депозитом."))
            deposit.settled_at = timezone.now()
            deposit.save(update_fields=["status", "refund_amount", "refund_shift", "settled_at"])
    return len(held)


def attach_deposits(table, order):
    """Гость пришёл: депозиты стола зачитываются в его заказ и уменьшают сумму к оплате."""
    held = held_deposits(table)
    if not held:
        return Decimal(0)
    with transaction.atomic():
        total = sum((d.amount for d in held), Decimal(0))
        for deposit in held:
            deposit.status = Deposit.Status.APPLIED
            deposit.order = order
            deposit.settled_at = timezone.now()
            deposit.save(update_fields=["status", "order", "settled_at"])
        order.deposit_applied = (order.deposit_applied or Decimal(0)) + total
        order.save(update_fields=["deposit_applied"])
    return total
