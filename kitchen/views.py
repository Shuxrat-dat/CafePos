from django.db.models import Prefetch
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.permissions import KITCHEN_ROLES, role_required
from core.models import CafeSettings
from pos import services
from pos.models import Order, OrderItem

kitchen_only = role_required(KITCHEN_ROLES)

ACTIVE = [OrderItem.Status.QUEUED, OrderItem.Status.COOKING, OrderItem.Status.READY]


def _tickets_context():
    """Карточки кухни: те же заказы, что на кассе. Колонка определяется самой ранней стадией позиций."""
    active_items = OrderItem.objects.filter(status__in=ACTIVE).select_related("dish").prefetch_related("modifiers")
    orders = (
        Order.objects.exclude(status=Order.Status.CANCELLED)
        .filter(items__status__in=ACTIVE)
        .select_related("table__hall", "waiter")
        .prefetch_related(Prefetch("items", queryset=active_items, to_attr="kitchen_items"))
        .distinct()
    )
    warn = CafeSettings.get().kitchen_warn_minutes
    now = timezone.now()
    columns = {"new": [], "cooking": [], "ready": []}
    for order in orders:
        statuses = {i.status for i in order.kitchen_items}
        sent = [i.sent_at for i in order.kitchen_items if i.sent_at]
        order.since = min(sent) if sent else order.created_at
        minutes = int((now - order.since).total_seconds() // 60)
        order.wait_minutes = minutes
        if OrderItem.Status.QUEUED in statuses:
            column = "new"
        elif OrderItem.Status.COOKING in statuses:
            column = "cooking"
        else:
            column = "ready"
        if column != "ready":
            order.level = "late" if minutes >= warn * 2 else "warn" if minutes >= warn else "ok"
        else:
            order.level = "ok"
        columns[column].append(order)
    for column in columns.values():
        column.sort(key=lambda o: o.since)
    return {"columns": columns, "warn_minutes": warn}


def _tickets(request):
    return render(request, "kitchen/_tickets.html", _tickets_context())


@kitchen_only
def board(request):
    return render(request, "kitchen/board.html", _tickets_context())


@kitchen_only
def tickets(request):
    return _tickets(request)


@kitchen_only
@require_POST
def mark_ready(request, item_id):
    item = get_object_or_404(OrderItem, id=item_id, status__in=[OrderItem.Status.QUEUED, OrderItem.Status.COOKING])
    services.mark_items_ready(OrderItem.objects.filter(id=item.id))
    return _tickets(request)


@kitchen_only
@require_POST
def ticket_start(request, order_id):
    order = get_object_or_404(Order.objects.exclude(status=Order.Status.CANCELLED), id=order_id)
    services.start_cooking(order)
    return _tickets(request)


@kitchen_only
@require_POST
def ticket_ready(request, order_id):
    order = get_object_or_404(Order.objects.exclude(status=Order.Status.CANCELLED), id=order_id)
    services.mark_items_ready(order.items.all())
    return _tickets(request)


@kitchen_only
@require_POST
def ticket_served(request, order_id):
    order = get_object_or_404(Order.objects.exclude(status=Order.Status.CANCELLED), id=order_id)
    services.serve_ready(order)
    return _tickets(request)
