"""Выплаты сотрудникам и остаток к выплате.

Начислено за всё время = часы по табелю × ставка в час + доля официанта со всех его оплаченных заказов.
Остаток = начислено − все выдачи (выплаты и авансы). Аванс может увести остаток в минус: это долг сотрудника,
он погашается следующими заказами.
"""
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from django.utils.translation import gettext as _

MAX_AMOUNT = Decimal("1000000000")


class PayoutError(Exception):
    """Текст готов для показа."""


def earned_total(user, now=None):
    """Начислено за всё время: часы × ставка + доля с заказов."""
    from pos.models import Order

    from .models import WorkShift

    now = now or timezone.now()
    hours = sum((s.hours_within(s.started_at, now, now) for s in WorkShift.objects.filter(user=user)), 0.0)
    by_hours = (Decimal(str(round(hours, 2))) * (user.hourly_rate or 0)).quantize(Decimal("1"))
    paid = Order.objects.filter(waiter=user, status=Order.Status.PAID)
    fixed = paid.exclude(waiter_earning=None).aggregate(s=Sum("waiter_earning"))["s"] or Decimal(0)
    # Заказы, оплаченные до фиксации доли, считаются по текущему проценту
    legacy = Decimal(0)
    if user.sales_percent:
        for order in paid.filter(waiter_earning=None).select_related("waiter").prefetch_related("items__modifiers"):
            legacy += order.earned_by_waiter
    return by_hours + fixed + legacy


def paid_total(user, start=None, end=None):
    from .models import SalaryPayment

    qs = SalaryPayment.objects.filter(user=user)
    if start is not None:
        qs = qs.filter(created_at__gte=start)
    if end is not None:
        qs = qs.filter(created_at__lt=end)
    return qs.aggregate(s=Sum("amount"))["s"] or Decimal(0)


def balance(user):
    """Сколько заведение должно сотруднику сейчас. Меньше нуля — сотрудник получил больше, чем заработал."""
    return earned_total(user) - paid_total(user)


def parse_amount(raw):
    text = str(raw or "").replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise PayoutError(_("Введите сумму числом.")) from None
    if not value.is_finite() or value <= 0 or value > MAX_AMOUNT:
        raise PayoutError(_("Введите сумму больше нуля."))
    return value.quantize(Decimal("1"))


def pay(user, amount_raw, kind, by, from_cash=False, comment=""):
    """Записывает выдачу денег: расход «Зарплата» и, если из кассы, изъятие в открытой смене."""
    from core import audit
    from pos import services as pos_services
    from pos.models import CashMovement
    from reports.models import Expense

    from .models import SalaryPayment

    if kind not in SalaryPayment.Kind.values:
        raise PayoutError(_("Выберите: выплата или аванс."))
    amount = parse_amount(amount_raw)
    if kind == SalaryPayment.Kind.PAYOUT and amount > max(balance(user), Decimal(0)):
        raise PayoutError(_("Выплата больше остатка. Если даёте деньги вперёд, выберите «Аванс»."))
    name = user.get_full_name() or user.username
    label = SalaryPayment.Kind(kind).label
    text = f"{label}: {name}" + (f" · {comment.strip()}" if comment and comment.strip() else "")
    with transaction.atomic():
        movement = None
        if from_cash:
            shift = pos_services.get_current_shift()
            if not shift:
                raise PayoutError(_("Кассовая смена не открыта: выдайте не из кассы или откройте смену."))
            try:
                movement = pos_services.add_cash_movement(shift, CashMovement.Kind.WITHDRAWAL, str(amount), text, by)
            except pos_services.PaymentError as exc:
                raise PayoutError(str(exc)) from None
        expense = Expense.objects.create(
            date=timezone.localdate(), category=Expense.Category.SALARY, amount=amount, comment=text[:200], created_by=by,
        )
        payment = SalaryPayment.objects.create(
            user=user, kind=kind, amount=amount, comment=(comment or "").strip()[:200], from_cash=bool(movement),
            cash_movement=movement, expense=expense, created_by=by,
        )
    audit.record(by, audit.A.SALARY_PAYOUT, amount=amount, detail=text[:200])
    return payment
