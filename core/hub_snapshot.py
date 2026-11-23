"""Сводка для центрального бота: цифры за сегодня, вчера и неделю, смена, список закупки.

Касса собирает её сама и присылает боту (core/hub.py). Бот только показывает владельцу эти цифры по
командам /bugun, /ombor и другим, поэтому формат ниже должен совпадать с hub/texts.py.
"""
from collections import defaultdict
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

from inventory.forecast import shopping_list
from inventory.models import StockMovement
from pos import services
from pos.models import Order

from .models import CafeSettings

HISTORY_DAYS = 15   # вчера сравниваем с тем же днём прошлой недели


def _num(value):
    return float(value or 0)


def _day_bounds(day):
    tz = timezone.get_current_timezone()
    return datetime.combine(day, time.min, tzinfo=tz), datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)


def _empty_day(day):
    return {"date": day.isoformat(), "revenue": 0, "orders": 0, "avg": 0, "cost": 0, "profit": 0,
            "has_cost": False, "cash": 0, "card": 0, "cancelled": 0, "cancelled_sum": 0, "vs_last_week": None}


def build_snapshot(now=None):
    now = now or timezone.now()
    today = timezone.localtime(now).date()
    start, _ = _day_bounds(today - timedelta(days=HISTORY_DAYS - 1))
    _, end = _day_bounds(today)

    paid = list(
        Order.objects.filter(status=Order.Status.PAID, closed_at__gte=start, closed_at__lt=end)
        .prefetch_related("items__dish", "items__modifiers")
    )
    days = {today - timedelta(days=i): _empty_day(today - timedelta(days=i)) for i in range(HISTORY_DAYS)}
    dish_rows = {"today": defaultdict(lambda: [0, Decimal(0)]), "week": defaultdict(lambda: [0, Decimal(0)])}
    dish_names = {}
    week_start = today - timedelta(days=6)

    for order in paid:
        local_day = timezone.localtime(order.closed_at).date()
        row = days.get(local_day)
        if row is None:
            continue
        total = order.total
        row["revenue"] += _num(total)
        row["orders"] += 1
        row["cash"] += _num(order.cash_amount)
        row["card"] += _num(order.card_paid + order.click_paid)  # безнал: карта и Click
        if local_day < week_start:
            continue
        for item in order.items.all():
            dish_names[item.dish_id] = (item.dish.name, item.dish.name_uz)
            for bucket in (("week",) + (("today",) if local_day == today else ())):
                dish_rows[bucket][item.dish_id][0] += item.quantity
                dish_rows[bucket][item.dish_id][1] += item.line_total

    # Себестоимость берём из реальных списаний склада, как на панели владельца
    movements = StockMovement.objects.filter(
        kind=StockMovement.Kind.SALE, order__status=Order.Status.PAID,
        order__closed_at__gte=start, order__closed_at__lt=end,
    ).select_related("product", "order")
    for move in movements:
        row = days.get(timezone.localtime(move.order.closed_at).date())
        if row is not None:
            unit_cost = move.unit_cost if move.unit_cost is not None else move.product.cost_per_unit
            row["cost"] += _num(-move.quantity * unit_cost)

    cancelled = Order.objects.filter(
        status=Order.Status.CANCELLED, closed_at__gte=_day_bounds(today - timedelta(days=1))[0], closed_at__lt=end
    ).prefetch_related("items__dish", "items__modifiers")
    for order in cancelled:
        row = days.get(timezone.localtime(order.closed_at).date())
        total = _num(order.total) if order.items.all() else 0
        if row is not None and total:
            row["cancelled"] += 1
            row["cancelled_sum"] += total

    for row in days.values():
        row["avg"] = row["revenue"] / row["orders"] if row["orders"] else 0
        row["has_cost"] = row["cost"] > 0
        row["profit"] = row["revenue"] - row["cost"] if row["has_cost"] else 0

    yesterday = today - timedelta(days=1)
    last_week = days[yesterday - timedelta(days=7)]["revenue"]
    if last_week:
        days[yesterday]["vs_last_week"] = round((days[yesterday]["revenue"] - last_week) / last_week * 100)

    week_days = [days[today - timedelta(days=i)] for i in range(7)]
    prev_days = [days[today - timedelta(days=i)] for i in range(7, 14)]
    week = {
        "revenue": sum(d["revenue"] for d in week_days),
        "orders": sum(d["orders"] for d in week_days),
        "cost": sum(d["cost"] for d in week_days),
        "prev_revenue": sum(d["revenue"] for d in prev_days),
    }
    week["has_cost"] = week["cost"] > 0
    week["profit"] = week["revenue"] - week["cost"] if week["has_cost"] else 0
    best = max(week_days, key=lambda d: d["revenue"])
    week["best_day"] = {"date": best["date"], "revenue": best["revenue"]} if best["revenue"] else None

    def top(bucket):
        ranked = sorted(dish_rows[bucket].items(), key=lambda kv: -kv[1][0])[:5]
        return [{"name": dish_names[i][0], "name_uz": dish_names[i][1], "qty": qty, "revenue": _num(rev)}
                for i, (qty, rev) in ranked]

    shift = services.get_current_shift()
    shift_data = None
    if shift:
        shift_data = {
            "open": True,
            "cashier": shift.cashier.get_full_name() or shift.cashier.username,
            "opened_at": timezone.localtime(shift.opened_at).strftime("%H:%M"),
            "revenue": _num(shift.total_revenue),
            "orders": shift.orders.filter(status=Order.Status.PAID).count(),
        }

    shopping = [
        {"name": r["product"].name, "name_uz": r["product"].name_uz, "unit": r["product"].unit,
         "stock": _num(r["product"].stock), "suggest": r["suggest"], "days_left": r["days_left"],
         "level": r["level"], "cost": _num(r["cost"])}
        for r in shopping_list(now)
    ]
    return {
        "version": 1,
        "venue": CafeSettings.get().name,
        "tz": settings.TIME_ZONE,
        "generated_at": timezone.localtime(now).isoformat(timespec="seconds"),
        "today": days[today],
        "yesterday": days[yesterday],
        "week": week,
        "top_today": top("today"),
        "top_week": top("week"),
        "shift": shift_data,
        "shopping": shopping,
    }
