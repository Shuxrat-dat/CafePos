"""Страница заказов: активные и завершённые, фильтры, просмотр и смена статуса."""
from datetime import date, datetime, time, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from accounts.permissions import KITCHEN_ROLES, has_role

from . import services
from .models import Order, OrderItem

KITCHEN_OPEN = [OrderItem.Status.QUEUED, OrderItem.Status.COOKING, OrderItem.Status.READY]


def _parse_date(value, default):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return default


def _active_filter():
    """Активные: открытые с позициями и оплаченные, которые ещё не выданы с кухни."""
    return Q(status=Order.Status.OPEN, n_items__gt=0) | Q(status=Order.Status.PAID, n_open__gt=0)


def _base_queryset(user):
    qs = Order.objects.annotate(
        n_items=Count("items", distinct=True),
        n_open=Count("items", filter=Q(items__status__in=KITCHEN_OPEN), distinct=True),
    ).select_related("table__hall", "waiter", "cashier")
    if not user.sees_all_orders:
        qs = qs.filter(waiter=user)
    return qs


@login_required
def orders(request):
    user = request.user
    today = timezone.localdate()
    tab = request.GET.get("tab", "active")
    if tab not in ("active", "done", "cancelled"):
        tab = "active"
    date_from = _parse_date(request.GET.get("from"), today)
    date_to = _parse_date(request.GET.get("to"), today)
    if date_to < date_from:
        date_from, date_to = date_to, date_from
    order_type = request.GET.get("type", "")
    stage = request.GET.get("stage", "")
    method = request.GET.get("method", "")
    search = request.GET.get("q", "").strip()[:50]

    tz = timezone.get_current_timezone()
    start = datetime.combine(date_from, time.min, tzinfo=tz)
    end = datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=tz)

    base = _base_queryset(user)
    active_qs = base.filter(_active_filter())
    done_qs = base.filter(status=Order.Status.PAID, n_open=0, closed_at__gte=start, closed_at__lt=end)
    cancelled_qs = base.filter(status=Order.Status.CANCELLED, n_items__gt=0, closed_at__gte=start, closed_at__lt=end)
    counts = {"active": active_qs.count(), "done": done_qs.count(), "cancelled": cancelled_qs.count()}

    qs = {"active": active_qs.order_by("created_at"), "done": done_qs.order_by("-closed_at"),
          "cancelled": cancelled_qs.order_by("-closed_at")}[tab]
    if order_type in Order.OrderType.values:
        qs = qs.filter(order_type=order_type)
    if method in Order.PaymentMethod.values and tab == "done":
        qs = qs.filter(payment_method=method)
    if search:
        query = Q(table__number__iexact=search) | Q(customer_name__icontains=search) | Q(customer_phone__icontains=search)
        query |= Q(waiter__first_name__icontains=search) | Q(waiter__username__icontains=search)
        if search.lstrip("#№").isdigit():
            query |= Q(id=int(search.lstrip("#№")))
        qs = qs.filter(query)

    rows = list(qs.prefetch_related("items__modifiers", "items__dish")[:500])
    if tab == "active" and stage:
        if stage == "bill":
            rows = [o for o in rows if o.awaiting_payment]
        else:
            rows = [o for o in rows if o.stage == stage]
    page = Paginator(rows, 30).get_page(request.GET.get("page"))
    period_total = sum(o.total for o in rows) if tab == "done" else None

    context = {
        "tab": tab,
        "counts": counts,
        "page": page,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "order_type": order_type,
        "stage": stage,
        "method": method,
        "search": search,
        "period_total": period_total,
        "types": Order.OrderType.choices,
        "methods": Order.PaymentMethod.choices,
        "stages": [("new", _("Новый")), ("cooking", _("Готовится")), ("ready", _("Готов")),
                   ("served", _("Подан")), ("bill", _("Ожидает оплаты"))],
    }
    template = "pos/_orders_table.html" if request.headers.get("HX-Request") and request.headers.get("HX-Target") == "orders-table" else "pos/orders.html"
    return render(request, template, context)


def _visible_order(request, order_id):
    order = get_object_or_404(Order.objects.select_related("table__hall", "waiter", "cashier", "cancelled_by"), id=order_id)
    if not request.user.sees_all_orders and order.waiter_id != request.user.id:
        raise PermissionDenied
    return order


@login_required
def order_view(request, order_id):
    order = _visible_order(request, order_id)
    items = list(order.items.select_related("dish").prefetch_related("modifiers"))
    order._prefetched_objects_cache = {"items": items}
    sent = [i.sent_at for i in items if i.sent_at]
    ready = [i.ready_at for i in items if i.ready_at]
    context = {
        "order": order,
        "order_items": items,
        "totals": order.totals,
        "sent_at": min(sent) if sent else None,
        "ready_at": max(ready) if ready and order.stage in ("ready", "served", "paid") else None,
        "can_kitchen": has_role(request.user, KITCHEN_ROLES),
        "modal": bool(request.headers.get("HX-Request")),
    }
    template = "pos/_order_view_modal.html" if context["modal"] else "pos/order_view.html"
    return render(request, template, context)


@login_required
@require_POST
def advance(request, order_id):
    """Смена этапа заказа со страницы заказов: отправить на кухню, начать, готово, подано."""
    order = _visible_order(request, order_id)
    if order.status == Order.Status.CANCELLED:
        raise Http404
    action = request.POST.get("action")
    if action == "send" and order.status == Order.Status.OPEN:
        services.send_to_kitchen(order)
        messages.success(request, _("Заказ №%(n)s отправлен на кухню.") % {"n": order.id})
    elif action in ("start", "ready"):
        if not has_role(request.user, KITCHEN_ROLES):
            raise PermissionDenied
        if action == "start":
            services.start_cooking(order)
        else:
            services.mark_items_ready(order.items.all())
        messages.success(request, _("Статус заказа №%(n)s обновлён.") % {"n": order.id})
    elif action == "serve":
        services.serve_ready(order)
        messages.success(request, _("Заказ №%(n)s выдан.") % {"n": order.id})
    else:
        raise Http404
    next_url = request.POST.get("next", "")
    if not url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        next_url = "orders"
    return redirect(next_url)
