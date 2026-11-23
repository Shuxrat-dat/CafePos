"""Заработок официанта: сумма блюд его оплаченных заказов и его доля (процент фиксируется при оплате)."""
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone


def day_start(day):
    return timezone.make_aware(datetime.combine(day, time.min))


def periods(today=None):
    """Начала периодов «сегодня», «неделя» (с понедельника) и «месяц»."""
    today = today or timezone.localdate()
    return {
        "today": day_start(today),
        "week": day_start(today - timedelta(days=today.weekday())),
        "month": day_start(today.replace(day=1)),
    }


def paid_orders(user, start, end=None):
    from pos.models import Order

    qs = Order.objects.filter(waiter=user, status=Order.Status.PAID, closed_at__gte=start)
    if end is not None:
        qs = qs.filter(closed_at__lt=end)
    return qs.select_related("waiter", "table__hall").prefetch_related("items__modifiers").order_by("-closed_at")


def summarize(orders):
    result = {"count": 0, "base": Decimal(0), "earned": Decimal(0)}
    for order in orders:
        result["count"] += 1
        result["base"] += order.waiter_base
        result["earned"] += order.earned_by_waiter
    return result


def today_earned(user):
    """Быстрая сумма за сегодня для шапки официанта: одним запросом по зафиксированным суммам."""
    from pos.models import Order

    value = Order.objects.filter(
        waiter=user, status=Order.Status.PAID, closed_at__gte=periods()["today"]
    ).aggregate(s=Sum("waiter_earning"))["s"]
    return value or Decimal(0)
