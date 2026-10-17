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
READY_WARN_MINUTES = 3  # готовое блюдо дольше этого ждёт официанта — остывает


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
        order.level = _order_level(order, column, now, warn)
        order.ready_count = sum(1 for i in order.kitchen_items if i.status == OrderItem.Status.READY)
        order.total_count = len(order.kitchen_items)
        # Часть позиций готова, часть ещё нет: подсказываем отдать всё вместе
        order.partly_ready = column != "ready" and order.ready_count > 0
        if column == "ready":
            ready_times = [i.ready_at for i in order.kitchen_items if i.ready_at]
            order.ready_since = max(ready_times) if ready_times else order.since
            order.ready_wait = max(0, int((now - order.ready_since).total_seconds() // 60))
        columns[column].append(order)
    for column in columns.values():
        column.sort(key=lambda o: o.since)
    return {"columns": columns, "warn_minutes": warn, "ready_warn_minutes": READY_WARN_MINUTES}


def _order_level(order, column, now, warn):
    """Цвет карточки. Готовые блюда остывают, остальные оцениваем по времени приготовления каждого блюда."""
    if column == "ready":
        times = [i.ready_at for i in order.kitchen_items if i.ready_at]
        waited = (now - max(times)).total_seconds() / 60 if times else 0
        return "late" if waited >= READY_WARN_MINUTES * 2 else "warn" if waited >= READY_WARN_MINUTES else "ok"
    worst = 0
    for item in order.kitchen_items:
        if item.status == OrderItem.Status.READY:
            item.level = "ok"
            continue
        target = item.dish.prep_minutes or warn
        started = item.sent_at or order.since
        elapsed = (now - started).total_seconds() / 60
        item.target_minutes = item.dish.prep_minutes
        item.level = "late" if elapsed >= target * 2 else "warn" if elapsed >= target else "ok"
        worst = max(worst, {"ok": 0, "warn": 1, "late": 2}[item.level])
    return ("ok", "warn", "late")[worst]


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
