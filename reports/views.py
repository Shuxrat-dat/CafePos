"""Панель владельца. Все цифры считаются из оплаченных заказов и списаний склада за выбранный период."""
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db.models import F
from django.shortcuts import render
from django.utils import timezone
from django.utils.translation import gettext as _

from accounts.permissions import REPORT_ROLES, role_required
from inventory.models import Product, StockMovement
from pos.models import CashShift, Order

reports_only = role_required(REPORT_ROLES)

MAX_DAYS = 366


def _parse(value, default):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return default


def _bounds(date_from, date_to):
    tz = timezone.get_current_timezone()
    return (
        datetime.combine(date_from, time.min, tzinfo=tz),
        datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=tz),
    )


def _paid_orders(start, end):
    return list(
        Order.objects.filter(status=Order.Status.PAID, closed_at__gte=start, closed_at__lt=end)
        .select_related("waiter", "table", "cashier")
        .prefetch_related("items__dish__category", "items__modifiers")
        .order_by("closed_at")
    )


def _change(current, previous):
    """Изменение к прошлому периоду в процентах или None, если сравнивать не с чем."""
    if not previous:
        return None
    return round((Decimal(current) - Decimal(previous)) / Decimal(previous) * 100)


def _share(rows, total, key="sum"):
    for row in rows:
        row["pct"] = round(row[key] / total * 100) if total else 0
    return rows


@reports_only
def dashboard(request):
    today = timezone.localdate()
    date_from = _parse(request.GET.get("from"), today)
    date_to = _parse(request.GET.get("to"), today)
    if date_to < date_from:
        date_from, date_to = date_to, date_from
    if (date_to - date_from).days >= MAX_DAYS:
        date_from = date_to - timedelta(days=MAX_DAYS - 1)
    days = (date_to - date_from).days + 1
    start, end = _bounds(date_from, date_to)

    orders = _paid_orders(start, end)
    prev_from, prev_to = date_from - timedelta(days=days), date_from - timedelta(days=1)
    prev_orders = _paid_orders(*_bounds(prev_from, prev_to))

    revenue = Decimal(0)
    discounts = Decimal(0)
    service = Decimal(0)
    by_method = defaultdict(lambda: {"count": 0, "sum": Decimal(0)})
    by_type = defaultdict(lambda: {"count": 0, "sum": Decimal(0)})
    by_waiter = defaultdict(lambda: {"orders": 0, "revenue": Decimal(0)})
    dishes = defaultdict(lambda: {"qty": 0, "revenue": Decimal(0)})
    categories = defaultdict(lambda: {"qty": 0, "sum": Decimal(0)})
    cash_total = card_total = Decimal(0)
    single_day = days == 1
    buckets = defaultdict(lambda: {"sum": Decimal(0), "count": 0})

    for order in orders:
        totals = order.totals
        total = totals["total"]
        revenue += total
        discounts += totals["discount"]
        service += totals["service"]
        by_method[order.payment_method]["count"] += 1
        by_method[order.payment_method]["sum"] += total
        by_type[order.order_type]["count"] += 1
        by_type[order.order_type]["sum"] += total
        cash_total += order.cash_amount
        card_total += order.card_paid
        waiter = by_waiter[order.waiter.get_full_name() or order.waiter.username]
        waiter["orders"] += 1
        waiter["revenue"] += total
        closed = timezone.localtime(order.closed_at)
        bucket = buckets[closed.hour if single_day else closed.date()]
        bucket["sum"] += total
        bucket["count"] += 1
        for item in order.items.all():
            row = dishes[item.dish]
            row["qty"] += item.quantity
            row["revenue"] += item.line_total
            category = categories[item.dish.category]
            category["qty"] += item.quantity
            category["sum"] += item.line_total

    prev_revenue = sum((o.total for o in prev_orders), Decimal(0))
    orders_count = len(orders)
    avg_check = revenue / orders_count if orders_count else Decimal(0)
    prev_avg = prev_revenue / len(prev_orders) if prev_orders else Decimal(0)

    # Себестоимость берём из реальных списаний склада за эти заказы.
    # Цена фиксируется в момент списания (unit_cost); у старых движений её нет,
    # для них берём текущую цену продукта.
    movements = StockMovement.objects.filter(
        kind=StockMovement.Kind.SALE,
        order__status=Order.Status.PAID,
        order__closed_at__gte=start,
        order__closed_at__lt=end,
    ).select_related("product")
    cost = sum(
        (-m.quantity * (m.unit_cost if m.unit_cost is not None else m.product.cost_per_unit) for m in movements),
        Decimal(0),
    )
    margin = revenue - cost

    # Динамика: по часам за один день или по дням за период
    if single_day:
        hours = [h for h in range(24) if buckets.get(h)] or [12]
        keys = list(range(min(hours), max(hours) + 1))
        chart = [{"label": f"{h:02d}:00", "sum": buckets[h]["sum"] if h in buckets else 0} for h in keys]
    else:
        chart = []
        for offset in range(days):
            day = date_from + timedelta(days=offset)
            chart.append({"label": day.strftime("%d.%m"), "sum": buckets[day]["sum"] if day in buckets else 0,
                          "weekend": day.weekday() >= 5})
    chart_max = max((row["sum"] for row in chart), default=0) or 1
    for row in chart:
        row["height"] = round(row["sum"] / chart_max * 100) if row["sum"] else 0

    cancelled = Order.objects.filter(status=Order.Status.CANCELLED, closed_at__gte=start, closed_at__lt=end)
    cancelled = [o for o in cancelled.prefetch_related("items__modifiers") if o.items.all()]

    method_labels = dict(Order.PaymentMethod.choices)
    type_labels = dict(Order.OrderType.choices)
    context = {
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "days": days,
        "single_day": single_day,
        "prev_from": prev_from,
        "prev_to": prev_to,
        "ranges": [
            {"label": _("Сегодня"), "start": today.isoformat(), "end": today.isoformat()},
            {
                "label": _("Вчера"),
                "start": (today - timedelta(days=1)).isoformat(),
                "end": (today - timedelta(days=1)).isoformat(),
            },
            {"label": _("7 дней"), "start": (today - timedelta(days=6)).isoformat(), "end": today.isoformat()},
            {"label": _("30 дней"), "start": (today - timedelta(days=29)).isoformat(), "end": today.isoformat()},
        ],
        "revenue": revenue,
        "revenue_change": _change(revenue, prev_revenue),
        "prev_revenue": prev_revenue,
        "orders_count": orders_count,
        "orders_change": _change(orders_count, len(prev_orders)),
        "avg_check": avg_check,
        "avg_change": _change(avg_check, prev_avg),
        "discounts": discounts,
        "service": service,
        "cost": cost,
        "margin": margin,
        "margin_pct": round(margin / revenue * 100) if revenue else 0,
        "has_cost": cost > 0,
        "cash_total": cash_total,
        "card_total": card_total,
        "methods": _share(
            [{"label": method_labels.get(k, k), "key": k, **v} for k, v in sorted(by_method.items(), key=lambda x: -x[1]["sum"])],
            revenue,
        ),
        "types": _share(
            [{"label": type_labels.get(k, k), "key": k, **v} for k, v in sorted(by_type.items(), key=lambda x: -x[1]["sum"])],
            revenue,
        ),
        "categories": _share(
            sorted(({"obj": obj, **data} for obj, data in categories.items()), key=lambda r: -r["sum"]),
            sum((c["sum"] for c in categories.values()), Decimal(0)),
        ),
        "top_dishes": sorted(
            ({"obj": obj, **data} for obj, data in dishes.items()), key=lambda r: r["qty"], reverse=True
        )[:10],
        "waiters": sorted(
            ({"name": name, **data} for name, data in by_waiter.items()), key=lambda r: r["revenue"], reverse=True
        ),
        "chart": chart,
        "chart_max": chart_max,
        "recent": list(reversed(orders))[:8],
        "cancelled_count": len(cancelled),
        "cancelled_sum": sum((o.subtotal for o in cancelled), Decimal(0)),
        "low_stock": Product.objects.filter(stock__lte=F("min_stock")).order_by("stock"),
        "shifts": CashShift.objects.filter(opened_at__gte=start, opened_at__lt=end).select_related("cashier").order_by("-opened_at")[:10],
    }
    return render(request, "reports/dashboard.html", context)
