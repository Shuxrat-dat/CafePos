from collections import defaultdict
from datetime import date, datetime, time, timedelta

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import F
from django.shortcuts import render
from django.utils import timezone

from inventory.models import Product, StockMovement
from pos.models import Order

REPORT_ROLES = {"owner", "admin"}


def reports_only(view):
    @login_required
    def wrapper(request, *args, **kwargs):
        if not (request.user.is_superuser or request.user.role in REPORT_ROLES):
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return wrapper


def _parse(value, default):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return default


@reports_only
def dashboard(request):
    today = timezone.localdate()
    date_from = _parse(request.GET.get("from"), today)
    date_to = _parse(request.GET.get("to"), today)
    if date_to < date_from:
        date_from, date_to = date_to, date_from

    tz = timezone.get_current_timezone()
    start = datetime.combine(date_from, time.min, tzinfo=tz)
    end = datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=tz)

    orders = list(
        Order.objects.filter(status=Order.Status.PAID, closed_at__gte=start, closed_at__lt=end)
        .select_related("waiter", "table")
        .prefetch_related("items__dish", "items__modifiers")
    )

    revenue = 0
    by_method = defaultdict(int)
    by_waiter = defaultdict(lambda: {"orders": 0, "revenue": 0})
    dishes = defaultdict(lambda: {"qty": 0, "revenue": 0})

    for order in orders:
        total = order.total
        revenue += total
        by_method[order.payment_method] += total
        waiter = by_waiter[order.waiter.get_full_name() or order.waiter.username]
        waiter["orders"] += 1
        waiter["revenue"] += total
        for item in order.items.all():
            row = dishes[item.dish.name]
            row["qty"] += item.quantity
            row["revenue"] += item.line_total

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
        -m.quantity * (m.unit_cost if m.unit_cost is not None else m.product.cost_per_unit)
        for m in movements
    )

    orders_count = len(orders)
    margin = revenue - cost

    context = {
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "ranges": [
            {"label": "Сегодня", "start": today.isoformat(), "end": today.isoformat()},
            {
                "label": "Вчера",
                "start": (today - timedelta(days=1)).isoformat(),
                "end": (today - timedelta(days=1)).isoformat(),
            },
            {"label": "7 дней", "start": (today - timedelta(days=6)).isoformat(), "end": today.isoformat()},
            {"label": "30 дней", "start": (today - timedelta(days=29)).isoformat(), "end": today.isoformat()},
        ],
        "revenue": revenue,
        "orders_count": orders_count,
        "avg_check": revenue / orders_count if orders_count else 0,
        "cost": cost,
        "margin": margin,
        "margin_pct": round(margin / revenue * 100) if revenue else 0,
        "methods": [{"label": label, "sum": by_method.get(value, 0)} for value, label in Order.PaymentMethod.choices],
        "top_dishes": sorted(
            ({"name": name, **data} for name, data in dishes.items()), key=lambda r: r["qty"], reverse=True
        )[:10],
        "waiters": sorted(
            ({"name": name, **data} for name, data in by_waiter.items()), key=lambda r: r["revenue"], reverse=True
        ),
        "low_stock": Product.objects.filter(stock__lte=F("min_stock")).order_by("stock"),
    }
    return render(request, "reports/dashboard.html", context)