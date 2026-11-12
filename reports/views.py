"""Панель владельца. Все цифры считаются из оплаченных заказов и списаний склада за выбранный период."""
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.contrib import messages
from django.db.models import F, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import gettext as _

from accounts.permissions import REPORT_ROLES, role_required
from inventory.models import Product, StockMovement
from pos.models import CashShift, Order, Review

from core.forms import localized_name

from . import insights
from .forms import ExpenseForm
from .models import Expense

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
    # Чистая прибыль: из валовой (или из выручки, если техкарт нет) вычитаем расходы заведения за период
    expenses_total = _expenses_sum(date_from, date_to)
    net_profit = (margin if cost > 0 else revenue) - expenses_total

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

    # ---------- выводы простыми словами ----------
    if single_day:
        week_ago = date_from - timedelta(days=7)
        week_revenue = sum((o.total for o in _paid_orders(*_bounds(week_ago, week_ago))), Decimal(0))
        compare_change = _change(revenue, week_revenue)
        compare_label = _("К тому же дню прошлой недели (%(wd)s)") % {"wd": date_format(week_ago, "l")}
        best_key = max(buckets, key=lambda k: buckets[k]["sum"], default=None)
        best_label = f"{best_key:02d}:00" if best_key is not None else ""
    else:
        compare_change = _change(revenue, prev_revenue)
        compare_label = _("К предыдущим %(n)s дням") % {"n": days}
        best_key = max(buckets, key=lambda k: buckets[k]["sum"], default=None)
        best_label = best_key.strftime("%d.%m") if best_key is not None else ""
    best_sum = buckets[best_key]["sum"] if best_key is not None else 0
    best_dish = max(dishes.items(), key=lambda kv: kv[1]["qty"], default=None)
    low_stock_count = Product.objects.filter(stock__lte=F("min_stock")).count()
    sold_rows = [{"obj": obj, **data} for obj, data in dishes.items()]
    margin_rows = [r for r in insights.dish_margins(sold_rows) if r["margin"] is not None]
    summary = insights.headline(
        revenue=revenue, orders_count=orders_count, avg_check=avg_check, single_day=single_day,
        compare_label=compare_label, compare_change=compare_change, best_label=best_label, best_sum=best_sum,
        top_dish={"name": localized_name(best_dish[0]), "n": best_dish[1]["qty"]} if best_dish else None,
        margin=margin, margin_pct_value=round(margin / revenue * 100) if revenue else 0, has_cost=cost > 0,
        cancelled_count=len(cancelled), cancelled_sum=sum((o.subtotal for o in cancelled), Decimal(0)),
        low_stock_count=low_stock_count,
    )

    method_labels = dict(Order.PaymentMethod.choices)
    type_labels = dict(Order.OrderType.choices)
    period_reviews = list(Review.objects.filter(created_at__gte=start, created_at__lt=end).select_related("order"))
    rating_counts = {n: sum(1 for r in period_reviews if r.rating == n) for n in range(5, 0, -1)}
    context = {
        "reviews": {
            "count": len(period_reviews),
            "avg": round(sum(r.rating for r in period_reviews) / len(period_reviews), 1) if period_reviews else None,
            "bars": [{"stars": n, "count": c, "pct": round(c / len(period_reviews) * 100) if period_reviews else 0}
                     for n, c in rating_counts.items()],
            "recent": [r for r in period_reviews if r.comment or r.rating <= 3][:6],
        },
        "summary": summary,
        "profit_rows": sorted(margin_rows, key=lambda r: -r["profit"])[:8],
        "poor_rows": sorted((r for r in margin_rows if r["margin"] < 30), key=lambda r: r["margin"])[:6],
        "advice": insights.menu_advice(),
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
        "expenses_total": expenses_total,
        "net_profit": net_profit,
        "net_pct": round(net_profit / revenue * 100) if revenue else 0,
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


def _expenses_sum(date_from, date_to):
    return Expense.objects.filter(date__gte=date_from, date__lte=date_to).aggregate(s=Sum("amount"))["s"] or Decimal(0)


@reports_only
def expenses(request):
    """Расходы заведения: список за период, итоги по статьям и форма добавления."""
    today = timezone.localdate()
    date_from = _parse(request.GET.get("from"), today.replace(day=1))
    date_to = _parse(request.GET.get("to"), today)
    if date_to < date_from:
        date_from, date_to = date_to, date_from
    form = ExpenseForm(request.POST or None, initial={"date": today})
    if request.method == "POST" and form.is_valid():
        expense = form.save(commit=False)
        expense.created_by = request.user
        expense.save()
        messages.success(request, _("Расход добавлен."))
        return redirect(f"{request.path}?from={date_from.isoformat()}&to={date_to.isoformat()}")
    rows = list(Expense.objects.filter(date__gte=date_from, date__lte=date_to).select_related("created_by"))
    total = sum((e.amount for e in rows), Decimal(0))
    labels = dict(Expense.Category.choices)
    by_cat = defaultdict(Decimal)
    for e in rows:
        by_cat[e.category] += e.amount
    categories = _share(
        [{"label": labels[k], "sum": v} for k, v in sorted(by_cat.items(), key=lambda kv: -kv[1])], total
    )
    month_start = today.replace(day=1)
    prev_month_end = month_start - timedelta(days=1)
    return render(request, "reports/expenses.html", {
        "form": form,
        "rows": rows,
        "total": total,
        "categories": categories,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "ranges": [
            {"label": _("Этот месяц"), "start": month_start.isoformat(), "end": today.isoformat()},
            {"label": _("Прошлый месяц"), "start": prev_month_end.replace(day=1).isoformat(), "end": prev_month_end.isoformat()},
            {"label": _("30 дней"), "start": (today - timedelta(days=29)).isoformat(), "end": today.isoformat()},
        ],
    })


@reports_only
@require_POST
def expense_delete(request, expense_id):
    get_object_or_404(Expense, id=expense_id).delete()
    messages.success(request, _("Расход удалён."))
    back = request.POST.get("next", "")
    return redirect(back if back.startswith("/reports/expenses/") else "expenses")


def payroll_rows(date_from, date_to, now=None):
    """Зарплата за период по каждому сотруднику: часы по табелю × ставка + процент от его оплаченных заказов."""
    from accounts.models import User, WorkShift

    now = now or timezone.now()
    start, end = _bounds(date_from, date_to)
    shifts = WorkShift.objects.filter(started_at__lt=end).filter(
        models_q_open_or_ended_after(start)
    ).select_related("user")
    hours = defaultdict(float)
    open_now = defaultdict(bool)
    for s in shifts:
        hours[s.user_id] += s.hours_within(start, end, now)
        if s.ended_at is None:
            open_now[s.user_id] = True
    sales = defaultdict(Decimal)
    for order in _paid_orders(start, end):
        sales[order.waiter_id] += order.total
    rows = []
    for user in User.objects.filter(is_active=True).order_by("first_name", "username"):
        h = round(hours.get(user.id, 0.0), 2)
        sold = sales.get(user.id, Decimal(0))
        if not (h or sold or user.hourly_rate or user.sales_percent):
            continue
        by_hours = (Decimal(str(h)) * user.hourly_rate).quantize(Decimal("1"))
        by_sales = (sold * user.sales_percent / 100).quantize(Decimal("1"))
        rows.append({
            "user": user, "hours": h, "by_hours": by_hours, "sales": sold, "by_sales": by_sales,
            "total": by_hours + by_sales, "open": open_now.get(user.id, False),
        })
    return rows


def models_q_open_or_ended_after(start):
    from django.db.models import Q

    return Q(ended_at__isnull=True) | Q(ended_at__gt=start)


def _payroll_comment(user, date_from, date_to):
    name = user.get_full_name() or user.username
    return _("Зарплата: %(name)s, %(a)s — %(b)s") % {
        "name": name, "a": date_from.strftime("%d.%m.%Y"), "b": date_to.strftime("%d.%m.%Y"),
    }


@reports_only
def payroll(request):
    """Зарплата за период. Начисленное можно одной кнопкой записать в расходы (статья «Зарплата»)."""
    from accounts.models import User, WorkShift

    today = timezone.localdate()
    date_from = _parse(request.GET.get("from") or request.POST.get("from"), today.replace(day=1))
    date_to = _parse(request.GET.get("to") or request.POST.get("to"), today)
    if date_to < date_from:
        date_from, date_to = date_to, date_from
    back = f"{request.path}?from={date_from.isoformat()}&to={date_to.isoformat()}"

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "close_shift":
            shift = get_object_or_404(WorkShift, id=request.POST.get("shift_id") or 0, ended_at=None)
            shift.ended_at = timezone.now()
            shift.closed_by = request.user
            shift.save(update_fields=["ended_at", "closed_by"])
            messages.success(request, _("Смена сотрудника закрыта."))
        elif action == "to_expense":
            user = get_object_or_404(User, id=request.POST.get("user_id") or 0)
            row = next((r for r in payroll_rows(date_from, date_to) if r["user"].id == user.id), None)
            comment = _payroll_comment(user, date_from, date_to)
            if row is None or row["total"] <= 0:
                messages.error(request, _("Начислять нечего."))
            elif Expense.objects.filter(category=Expense.Category.SALARY, comment=comment).exists():
                messages.error(request, _("Эта зарплата уже записана в расходы."))
            else:
                Expense.objects.create(
                    date=date_to, category=Expense.Category.SALARY, amount=row["total"],
                    comment=comment, created_by=request.user,
                )
                messages.success(request, _("Зарплата записана в расходы."))
        return redirect(back)

    rows = payroll_rows(date_from, date_to)
    recorded = set(
        Expense.objects.filter(category=Expense.Category.SALARY).values_list("comment", flat=True)
    )
    for row in rows:
        row["recorded"] = _payroll_comment(row["user"], date_from, date_to) in recorded
    month_start = today.replace(day=1)
    prev_month_end = month_start - timedelta(days=1)
    return render(request, "reports/payroll.html", {
        "rows": rows,
        "total": sum((r["total"] for r in rows), Decimal(0)),
        "open_shifts": WorkShift.objects.filter(ended_at=None).select_related("user"),
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "ranges": [
            {"label": _("Этот месяц"), "start": month_start.isoformat(), "end": today.isoformat()},
            {"label": _("Прошлый месяц"), "start": prev_month_end.replace(day=1).isoformat(), "end": prev_month_end.isoformat()},
            {"label": _("7 дней"), "start": (today - timedelta(days=6)).isoformat(), "end": today.isoformat()},
        ],
    })
