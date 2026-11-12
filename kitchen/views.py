from django.db.models import Prefetch
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.permissions import KITCHEN_ROLES, role_required
from core.models import CafeSettings
from menu.models import Category
from pos import services
from pos.models import Order, OrderItem

kitchen_only = role_required(KITCHEN_ROLES)

ACTIVE = [OrderItem.Status.QUEUED, OrderItem.Status.COOKING, OrderItem.Status.READY]
READY_WARN_MINUTES = 3  # готовое блюдо дольше этого ждёт официанта — остывает


def _station(request):
    """Цех из адреса (?station=bar) или из кнопки на карточке. Пусто — все цеха."""
    value = request.POST.get("station") or request.GET.get("station") or ""
    return value if value in Category.Station.values else ""


def _station_items(order, station):
    items = order.items.all()
    return items.filter(dish__category__station=station) if station else items


def _stations_in_use():
    """Цеха, к которым привязаны активные категории. Переключатель нужен, только если их больше одного."""
    used = set(Category.objects.filter(is_active=True).values_list("station", flat=True))
    return [(value, label) for value, label in Category.Station.choices if value in used]


def _tickets_context(station=""):
    """Карточки кухни: те же заказы, что на кассе. Колонка определяется самой ранней стадией позиций.
    С выбранным цехом на карточке остаются только его блюда."""
    active_items = OrderItem.objects.filter(status__in=ACTIVE).select_related("dish").prefetch_related("modifiers")
    orders = Order.objects.exclude(status=Order.Status.CANCELLED)
    if station:
        active_items = active_items.filter(dish__category__station=station)
        orders = orders.filter(items__status__in=ACTIVE, items__dish__category__station=station)
    else:
        orders = orders.filter(items__status__in=ACTIVE)
    orders = (
        orders.select_related("table__hall", "waiter")
        .prefetch_related(Prefetch("items", queryset=active_items, to_attr="kitchen_items"))
        .distinct()
    )
    warn = CafeSettings.get().kitchen_warn_minutes
    now = timezone.now()
    columns = {"new": [], "cooking": [], "ready": []}
    for order in orders:
        if not order.kitchen_items:
            continue
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
    return {
        "columns": columns,
        "warn_minutes": warn,
        "ready_warn_minutes": READY_WARN_MINUTES,
        "station": station,
        "stations": _stations_in_use(),
    }


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
    return render(request, "kitchen/_tickets.html", _tickets_context(_station(request)))


@kitchen_only
def board(request):
    return render(request, "kitchen/board.html", _tickets_context(_station(request)))


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
    station = _station(request)
    if station:
        _station_items(order, station).filter(status=OrderItem.Status.QUEUED).update(status=OrderItem.Status.COOKING)
    else:
        services.start_cooking(order)
    return _tickets(request)


@kitchen_only
@require_POST
def ticket_ready(request, order_id):
    order = get_object_or_404(Order.objects.exclude(status=Order.Status.CANCELLED), id=order_id)
    services.mark_items_ready(_station_items(order, _station(request)))
    return _tickets(request)


@kitchen_only
@require_POST
def ticket_served(request, order_id):
    order = get_object_or_404(Order.objects.exclude(status=Order.Status.CANCELLED), id=order_id)
    station = _station(request)
    if station:
        _station_items(order, station).filter(status=OrderItem.Status.READY).update(status=OrderItem.Status.SERVED)
    else:
        services.serve_ready(order)
    return _tickets(request)
