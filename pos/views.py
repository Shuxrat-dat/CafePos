import json
import uuid
from collections import OrderedDict
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth import logout as auth_logout
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, Prefetch
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from accounts import cancel_pin
from accounts.models import User
from accounts.permissions import DISCOUNT_ROLES, KITCHEN_ROLES, MANAGE_ROLES, has_role
from core import audit, telegram
from core.models import CafeSettings
from menu.public_views import _qr_svg
from inventory.services import find_shortages
from menu.models import Category, Dish
from printing.services import receipt_printers

from . import loyalty, reviews, services
from .models import CashMovement, CashShift, GuestOrder, Hall, Order, OrderItem, Payment, Table


# ---------- вспомогательные функции ----------

def toast(response, message, kind="success"):
    """Всплывающее уведомление для ответов HTMX."""
    response.headers["HX-Trigger"] = json.dumps({"toast": {"message": str(message), "kind": kind}})
    return response


def _get_order(request, order_id, open_only=True):
    """Находит заказ и проверяет, что пользователь вправе с ним работать."""
    order = get_object_or_404(Order.objects.select_related("table__hall", "waiter"), id=order_id)
    user = request.user
    # Открытый заказ может править любой сотрудник зала: стол не закреплён за одним официантом.
    # Закрытые и отменённые чужие заказы официанту не показываем.
    if not user.sees_all_orders and order.status != Order.Status.OPEN and order.waiter_id != user.id:
        raise PermissionDenied
    if open_only and order.status != Order.Status.OPEN:
        raise Http404
    return order


def _can_cancel(user, order):
    # Кассир видит отмену всегда: окно с паролем подскажет, что его нужно задать, а без пароля отмена не пройдёт
    return order.status == Order.Status.OPEN and (
        not order.has_sent_items or user.can_cancel_sent_orders or user.needs_cancel_pin
    )


def _pin_error(request):
    """Проверка пароля отмены из формы. Пусто, если всё верно или пароль этому сотруднику не нужен."""
    try:
        cancel_pin.check(request.user, request.POST.get("pin"))
    except cancel_pin.PinError as exc:
        return str(exc)
    return ""


def _remove_form(request, order, item, error="", one=False):
    """Окно подтверждения удаления блюда с вводом пароля отмены (с ошибкой, если пароль не подошёл).
    one=True — убрать только одну порцию (кнопка «−»), а не всё блюдо (корзина)."""
    one = one and item.quantity > 1 and not item.by_weight
    response = render(request, "pos/_remove_item_form.html", {"order": order, "item": item, "error": error, "one": one})
    response["HX-Retarget"] = "#modal"
    response["HX-Reswap"] = "innerHTML"
    return response


def _items(order):
    return order.items.select_related("dish").prefetch_related("modifiers")


def _panel_context(request, order):
    order_items = list(_items(order))
    # Подставляем уже загруженные позиции, чтобы суммы не делали лишних запросов
    order._prefetched_objects_cache = {"items": order_items}
    return {
        "order": order,
        "order_items": order_items,
        "totals": order.totals,
        "can_cancel": _can_cancel(request.user, order),
        "can_discount": has_role(request.user, DISCOUNT_ROLES),
        "loyalty_customer": loyalty.find_customer(order.customer_phone),
        "takeaway_mode": _takeaway_mode(request, order),
    }


def _panel(request, order, message=None, kind="success"):
    response = render(request, "pos/_order_panel.html", _panel_context(request, order))
    return toast(response, message, kind) if message else response


def _modifier_groups(dish):
    """Модификаторы блюда: группы с выбором одного варианта и отдельные добавки."""
    groups = OrderedDict()
    extras = []
    for modifier in dish.modifiers.all():
        if modifier.group:
            groups.setdefault(modifier.group, []).append(modifier)
        else:
            extras.append(modifier)
    return list(groups.items()), extras


def _clean_modifiers(request, dish):
    """Берём только модификаторы этого блюда и не больше одного из каждой группы."""
    ids = {int(i) for i in request.POST.getlist("modifiers") if i.isdigit()}
    chosen, used_groups = [], set()
    for modifier in dish.modifiers.filter(id__in=ids):
        if modifier.group:
            if modifier.group in used_groups:
                continue
            used_groups.add(modifier.group)
        chosen.append(modifier)
    return chosen


def _clean_qty(request):
    try:
        qty = int(request.POST.get("quantity", "1"))
    except ValueError:
        qty = 1
    return max(1, min(qty, 50))


MAX_WEIGHT_GRAMS = 50000


def _clean_weight(request, dish=None):
    """Количество блюда на вес или в розлив; возвращает граммы (миллилитры).
    Просто: «1» — 1 кг, «1,5» — полтора кг. Целое больше 50 — граммы: «250» — 250 г (так привычнее для мелких порций).
    В режиме «на сумму» — сумма в сумах, из неё считаются граммы.
    Возвращает (граммы, None) или (None, текст ошибки)."""
    from decimal import ROUND_HALF_UP

    liters = dish is not None and dish.measure == "l"
    hint = (_("Укажите объём в литрах, например 1 или 0,5.") if liters
            else _("Укажите вес в килограммах, например 1 или 1,5."))
    raw = request.POST.get("weight", "").strip().replace(" ", "").replace(",", ".")
    if request.POST.get("weight_mode") == "sum" and dish is not None and dish.price > 0:
        try:
            amount = Decimal(raw)
        except InvalidOperation:
            amount = None
        if amount is None or not amount.is_finite() or amount <= 0:
            return None, _("Введите сумму, например 50000.")
        grams = int((amount * 1000 / dish.price).to_integral_value(ROUND_HALF_UP))
    else:
        try:
            value = Decimal(raw)
        except InvalidOperation:
            return None, hint
        if not value.is_finite():
            return None, hint
        in_units = "." in raw or value <= 50  # килограммы (литры); иначе граммы (миллилитры)
        grams = int((value * 1000 if in_units else value).to_integral_value())
    if grams < 10 or grams > MAX_WEIGHT_GRAMS:
        if liters:
            return None, _("Объём должен быть от 10 мл до 50 л. Например: 1 или 0,5.")
        return None, _("Вес должен быть от 10 г до 50 кг. Например: 1 или 1,5.")
    return grams, None


def _weight_form(request, order, dish, item, error):
    """Форма позиции снова в окне: с ошибкой веса и уже введёнными данными."""
    groups, extras = _modifier_groups(dish)
    response = render(
        request,
        "pos/_item_form.html",
        {
            "order": order, "dish": dish, "item": item,
            "selected": {int(i) for i in request.POST.getlist("modifiers") if i.isdigit()},
            "quantity": 1, "comment": _clean_comment(request), "weight": request.POST.get("weight", ""),
            "weight_mode": request.POST.get("weight_mode", "weight"),
            "weight_error": error, "groups": groups, "extras": extras,
        },
    )
    response["HX-Retarget"] = "#modal"
    response["HX-Reswap"] = "innerHTML"
    return response


def _clean_comment(request):
    return request.POST.get("comment", "").strip()[:200]


def _find_same(order, dish, modifier_ids, comment, takeaway=False):
    from menu.models import best_promotion, promo_price

    # Склеиваем только позиции с той же ценой: началась или кончилась акция — новая строка по новой цене
    promo = best_promotion(dish)
    price = promo_price(dish.price, promo.percent) if promo else dish.price
    items = order.items.filter(
        dish=dish, status=OrderItem.Status.NEW, comment=comment, price=price, takeaway=takeaway
    ).prefetch_related("modifiers")
    for item in items:
        if {m.id for m in item.modifiers.all()} == set(modifier_ids):
            return item
    return None


def _takeaway_mode(request, order):
    """Режим «С собой» для заказа за столом: новые блюда помечаются «с собой» (без обслуживания, на кухне упакуют)."""
    return order.is_dine_in and request.session.get(f"takeaway_mode_{order.id}", False)


def _order_changed(order):
    """После изменения состава пречек больше не актуален."""
    if order.bill_requested_at:
        order.bill_requested_at = None
        order.save(update_fields=["bill_requested_at"])


def _open_orders():
    """Открытые заказы, в которых есть хотя бы одна позиция (пустые черновики стол не занимают)."""
    return (
        Order.objects.filter(status=Order.Status.OPEN)
        .annotate(n_items=Count("items"))
        .filter(n_items__gt=0)
    )


def _busy_table_ids():
    return set(_open_orders().exclude(table=None).values_list("table_id", flat=True))


# ---------- зал ----------

def _floor_context(request):
    user = request.user
    from . import booking

    booking.sync_today()  # подтверждённые брони на сегодня появляются на схеме
    halls = list(Hall.objects.prefetch_related("tables"))
    orders = _open_orders().select_related("waiter", "table").prefetch_related("items__modifiers")
    by_table = {}
    counter_orders = []
    for order in orders:
        if order.table_id and order.is_dine_in:
            by_table.setdefault(order.table_id, order)
        else:
            counter_orders.append(order)
    pending_tables = set(
        GuestOrder.objects.filter(status=GuestOrder.Status.PENDING).values_list("table_id", flat=True)
    )
    counts = {"free": 0, "busy": 0, "bill": 0, "reserved": 0}
    for hall in halls:
        for table in hall.tables.all():
            order = by_table.get(table.id)
            table.open_order = order
            table.guest_pending = table.id in pending_tables
            table.locked = False  # стол не закреплён за официантом: открыть его может любой
            if order and order.awaiting_payment:
                table.state = "bill"
            elif order:
                table.state = "busy"
            elif table.is_reserved:
                table.state = "reserved"
            else:
                table.state = "free"
            counts[table.state] += 1
    # Навынос и доставка, которые ещё готовятся или ждут выдачи (в том числе уже оплаченные)
    paid_in_work = (
        Order.objects.filter(status=Order.Status.PAID, items__status__in=["queued", "cooking", "ready"])
        .exclude(order_type=Order.OrderType.DINE_IN)
        .distinct()
        .prefetch_related("items__modifiers")
    )
    counter_orders += list(paid_in_work)
    if not user.sees_all_orders:
        counter_orders = [o for o in counter_orders if o.status == Order.Status.OPEN or o.waiter_id == user.id]
    counter_orders.sort(key=lambda o: o.created_at)
    counts["total"] = sum(counts.values())

    categories = list(Category.objects.filter(is_active=True).order_by("sort_order"))
    quick_dishes = list(Dish.objects.filter(is_available=True, is_hidden=False).select_related("category")[:15])

    current_orders = list(
        Order.objects.filter(status=Order.Status.OPEN)
        .select_related("table", "waiter")
        .prefetch_related("items__dish", "items__modifiers")
        .order_by("id")
    )
    # Пустые черновики в списке не показываем
    current_orders = [o for o in current_orders if o.items.all()]
    now = timezone.now()
    marks_ready = _can_mark_ready(user)
    for o in current_orders:
        seconds = int((now - o.created_at).total_seconds())
        if seconds >= 86400:
            o.timer_display = f"{seconds // 86400} {_('д')} {seconds % 86400 // 3600} {_('ч')}"
        elif seconds >= 3600:
            o.timer_display = f"{seconds // 3600} {_('ч')} {seconds % 3600 // 60} {_('мин')}"
        else:
            o.timer_display = f"{seconds // 60:02d}:{seconds % 60:02d}"
        o.age_seconds = seconds
        o.stage_action = _stage_action({i.status for i in o.items.all()}, marks_ready)
        if o.stage_action == "send":
            o.status_color, o.stage_rank = "orange", 0
            o.action_label = _("Отправить на кухню")
        elif o.stage_action == "serve":
            o.status_color, o.stage_rank = "green", 2
            o.action_label = _("Отнёс гостю")
        elif o.stage_action in ("ready", "wait"):
            # Блюда на кухне: готовность отмечает повар на экране кухни
            o.status_color, o.stage_rank = "blue", 1
            o.action_label = _("Готово") if o.stage_action == "ready" else _("Готовится")
        else:
            # Всё выдано гостю: остаётся принять оплату
            o.status_color, o.stage_rank = "green", 3
            o.action_label = _("К оплате")
            o.ready_to_pay = True
    if user.can_take_payment:
        # Кассе первыми нужны заказы, которые ждут оплаты
        current_orders.sort(key=lambda o: (not getattr(o, "ready_to_pay", False), o.id))

    # Три раздела: в зале, навынос, доставка. В каждом только свои заказы.
    tab_types = [Order.OrderType.DINE_IN, Order.OrderType.TAKEAWAY, Order.OrderType.DELIVERY]
    order_id = request.GET.get("order_id")
    wanted = next((o for o in current_orders if str(o.id) == str(order_id)), None) if order_id else None
    active_tab = request.GET.get("tab")
    if active_tab not in tab_types:
        active_tab = wanted.order_type if wanted else Order.OrderType.DINE_IN  # открыли заказ — его раздел
    floor_tabs = [
        {"key": key, "label": label, "count": sum(1 for o in current_orders if o.order_type == key)}
        for key, label in ((Order.OrderType.DINE_IN, _("В зале")), (Order.OrderType.TAKEAWAY, _("Навынос")),
                           (Order.OrderType.DELIVERY, _("Доставка")))
    ]
    current_orders = [o for o in current_orders if o.order_type == active_tab]
    selected_order = None
    if wanted and wanted.order_type == active_tab:
        selected_order = wanted
    if not selected_order and current_orders:
        selected_order = current_orders[0]

    if selected_order:
        items_list = list(selected_order.items.select_related("dish").prefetch_related("modifiers"))
        selected_order._prefetched_objects_cache = {"items": items_list}
        cafe_settings = CafeSettings.get()
        totals = selected_order.totals
        subtotal = totals["subtotal"]
        service = totals["service"]
        vat_rate = cafe_settings.vat_percent / Decimal("100") if cafe_settings.vat_percent else Decimal(0)
        tax = round(subtotal * vat_rate)
        selected_order.custom_totals = {
            "subtotal": subtotal,
            "service": service,
            "tax": tax,
            "total": totals["total"],
        }

    pending_since = {}
    for table_id, created_at in GuestOrder.objects.filter(status=GuestOrder.Status.PENDING).values_list(
        "table_id", "created_at"
    ):
        pending_since[table_id] = min(created_at, pending_since.get(table_id, created_at))

    def _minutes(since):
        return max(0, int((now - since).total_seconds() // 60))

    table_queue = []
    for hall in halls:
        for t in hall.tables.all():
            if t.guest_pending:
                kind, wait = "pending", _minutes(pending_since[t.id]) if t.id in pending_since else 0
            elif t.open_order:
                kind, wait = "busy", _minutes(t.open_order.created_at)
            else:
                kind, wait = "free", None
            table_queue.append({
                "number": t.number,
                "seats": t.seats,
                "wait": wait,
                "table_id": t.id,
                "kind": kind,
                "locked": t.locked,
                "order_id": t.open_order.id if t.open_order else None,
            })
    order_kind = {"pending": 0, "free": 1, "busy": 2}
    table_queue.sort(key=lambda row: (order_kind[row["kind"]], -(row["wait"] or 0)))
    queue_counts = {k: sum(1 for row in table_queue if row["kind"] == k) for k in order_kind}

    return {
        "queue_counts": queue_counts,
        "halls": halls,
        "counts": counts,
        "counter_orders": counter_orders,
        "categories": categories,
        "quick_dishes": quick_dishes,
        "current_orders": current_orders,
        "floor_tabs": floor_tabs,
        "active_tab": active_tab,
        "selected_order": selected_order,
        "table_queue": table_queue,
    }


def _home_url(user, order=None):
    """Куда вернуться из заказа: к схеме столов; заказ навынос или на доставку у кассы живёт в «Заказах», в своём разделе."""
    if order is not None and not user.simple_floor and order.order_type != Order.OrderType.DINE_IN:
        return f"{reverse('floor')}?tab={order.order_type}"
    return reverse(user.home_url_name)


def _can_mark_ready(user):
    """Готовность блюд отмечает кухня. Без повара в программе (некому смотреть экран кухни) это делает зал."""
    if has_role(user, KITCHEN_ROLES):
        return True
    return not User.objects.filter(role=User.Role.COOK, is_active=True).exists()


def _stage_action(statuses, marks_ready):
    """Следующий шаг по карточке заказа: send — на кухню, serve — отнести гостю, ready — отметить готовым,
    wait — ждём кухню (кнопки нет), пусто — всё подано."""
    if OrderItem.Status.NEW in statuses:
        return "send"
    if OrderItem.Status.READY in statuses:
        return "serve"
    if statuses & {OrderItem.Status.QUEUED, OrderItem.Status.COOKING}:
        return "ready" if marks_ready else "wait"
    return ""


@login_required
@require_POST
def toggle_order_stage(request, order_id):
    """Кнопка на карточке заказа: делает тот же шаг, что написан на ней."""
    order = _get_order(request, order_id, open_only=False)
    action = _stage_action({i.status for i in order.items.all()}, _can_mark_ready(request.user))
    if action == "send":
        services.send_to_kitchen(order)
    elif action == "serve":
        services.serve_ready(order)
    elif action == "ready":
        services.mark_items_ready(order.items.all())
    return redirect(f"{reverse('floor')}?tab={order.order_type}")  # остаёмся в своём разделе


@login_required
@require_POST
def quick_add(request, dish_id):
    order_id = request.POST.get("order_id") or request.GET.get("order_id")
    if order_id and str(order_id).isdigit():
        order = _get_order(request, order_id)
    else:
        # Заказ не выбран: берём свой пустой черновик навынос или заводим новый
        order = (
            Order.objects.filter(status=Order.Status.OPEN, waiter=request.user, table=None,
                                 order_type=Order.OrderType.TAKEAWAY)
            .annotate(n_items=Count("items"))
            .filter(n_items=0)
            .first()
        ) or services.create_order(request.user, Order.OrderType.TAKEAWAY)
    dish = get_object_or_404(Dish, id=dish_id, is_available=True)
    if dish.sold_by_weight:
        # Вес нужно ввести: открываем экран заказа сразу с формой этого блюда
        return redirect(f"{reverse('order_detail', args=[order.id])}?weigh={dish.id}")
    item = order.items.filter(dish=dish, status=OrderItem.Status.NEW).first()
    if item:
        item.quantity += 1
        item.save(update_fields=["quantity"])
    else:
        order.items.create(dish=dish, quantity=1, status=OrderItem.Status.NEW)  # цену (с акцией) ставит модель
    _order_changed(order)
    return redirect(f"{reverse('floor')}?order_id={order.id}")


@login_required
def floor(request):
    if request.user.role == "cook" and not request.user.is_superuser:
        return redirect("kitchen_board")
    return render(request, "pos/floor.html", _floor_context(request))


@login_required
def global_search(request):
    """Поиск из шапки: заказы и столы. Права те же, что везде: официант видит только свои заказы."""
    from django.db.models import Q

    user = request.user
    q = request.GET.get("q", "").strip()[:40]
    orders, tables = [], []
    if q and has_role(user, {"owner", "admin", "waiter", "cashier"}):
        cond = Q(table__number__icontains=q) | Q(customer_name__icontains=q) | Q(customer_phone__icontains=q)
        if q.lstrip("#").isdigit():
            # Номер за день (сегодняшние заказы) и внутренний номер — оба находят заказ
            today = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
            cond |= Q(id=int(q.lstrip("#"))) | Q(day_number=int(q.lstrip("#")), created_at__gte=today)
        found = Order.objects.filter(cond).select_related("table", "waiter").order_by("-created_at")
        if not user.sees_all_orders:
            found = found.filter(Q(waiter=user) | Q(status=Order.Status.OPEN))
        orders = list(found[:8])
        busy = {o.table_id: o for o in _open_orders().exclude(table=None)}
        for table in Table.objects.select_related("hall").filter(number__icontains=q)[:6]:
            table.open_order = busy.get(table.id)
            table.locked = False
            tables.append(table)
    return render(request, "pos/_search_results.html", {"q": q, "orders": orders, "tables": tables})


@login_required
def floor_grid(request):
    return render(request, "pos/_floor_grid.html", _floor_context(request))


@login_required
def table_map(request):
    """Схема залов для официанта и кассира: нажал на стол, открылся его заказ (или новый, если стол свободен).
    Официант видит чужие занятые столы, но открыть их не может, кассир открывает любые."""
    context = _floor_context(request)
    if request.user.simple_floor:
        context["counter_orders"] = []  # официанту навынос и доставка видны в «Заказах», здесь только столы
    # Кассиру и администратору — фильтр «чьи это столы»: официанты, у которых сейчас есть открытые столы
    waiters = {}
    for hall in context["halls"]:
        for table in hall.tables.all():
            order = getattr(table, "open_order", None)
            if order:
                entry = waiters.setdefault(order.waiter_id, {"user": order.waiter, "count": 0})
                entry["count"] += 1
    context["floor_waiters"] = sorted(waiters.values(), key=lambda w: (w["user"].first_name or w["user"].username).lower())
    if request.headers.get("HX-Request"):
        return render(request, "pos/_floor_grid.html", context)
    return render(request, "pos/tables.html", context)


@login_required
@require_POST
def new_order(request):
    """Новый заказ навынос или на доставку. Пустой черновик того же типа переиспользуем."""
    order_type = request.POST.get("type", Order.OrderType.TAKEAWAY)
    if order_type not in (Order.OrderType.TAKEAWAY, Order.OrderType.DELIVERY):
        raise Http404
    order = (
        Order.objects.filter(status=Order.Status.OPEN, waiter=request.user, order_type=order_type, table=None)
        .annotate(n_items=Count("items"))
        .filter(n_items=0)
        .first()
    )
    if order is None:
        order = services.create_order(request.user, order_type)
    return redirect("order_detail", order_id=order.id)


@login_required
@require_POST
def open_table(request, table_id):
    table = get_object_or_404(Table, id=table_id)
    order = (
        Order.objects.filter(table=table, status=Order.Status.OPEN, order_type=Order.OrderType.DINE_IN)
        .annotate(n_items=Count("items"))
        .order_by("-n_items", "id")
        .first()
    )
    if order is None:
        order = services.create_order(request.user, Order.OrderType.DINE_IN, table)
    elif order.n_items == 0 and order.waiter_id != request.user.id:
        # Пустой черновик, который кто-то открыл и бросил, стол не занимает (на схеме он свободен):
        # заказ переходит к тому, кто нажал на стол
        order.waiter = request.user
        order.save(update_fields=["waiter"])
    if table.is_reserved:
        # Гость по брони пришёл: бронь снимается, стол становится занятым
        table.reserved_for, table.reserved_at = "", None
        table.save(update_fields=["reserved_for", "reserved_at"])
        # Депозит, внесённый при брони, зачитывается в заказ
        if services.attach_deposits(table, order):
            messages.success(request, _("Депозит брони зачтён в счёт заказа."))
    return redirect("order_detail", order_id=order.id)


@login_required
def reserve_table(request, table_id):
    """Бронь стола. При брони можно принять депозит; при снятии брони депозит возвращают или оставляют заведению."""
    table = get_object_or_404(Table.objects.select_related("hall"), id=table_id)
    error = ""
    if request.method == "POST":
        if request.POST.get("action") == "clear":
            held = services.held_deposits(table)
            choice = request.POST.get("deposit_action", "")
            if held and choice not in ("refund", "keep"):
                error = _("Выберите, что сделать с депозитом.")
            else:
                try:
                    services.settle_deposits(table, choice, request.user)
                except services.PaymentError as exc:
                    error = str(exc)
                else:
                    table.reserved_for, table.reserved_at = "", None
                    table.save(update_fields=["reserved_for", "reserved_at"])
                    messages.success(request, _("Бронь снята."))
                    return HttpResponse(headers={"HX-Redirect": reverse("floor")})
        else:
            name = request.POST.get("name", "").strip()[:100]
            raw_time = request.POST.get("time", "").strip()
            reserved_at = None
            if raw_time:
                try:
                    hour, minute = (int(x) for x in raw_time.split(":")[:2])
                    today = timezone.localdate()
                    reserved_at = timezone.make_aware(datetime(today.year, today.month, today.day, hour, minute))
                except ValueError:
                    error = _("Укажите время в формате ЧЧ:ММ.")
            if not name and not error:
                error = _("Укажите, на чьё имя бронь.")
            deposit_amount = None
            if not error and request.POST.get("deposit", "").strip():
                try:
                    deposit_amount = services.parse_money(request.POST.get("deposit", ""))
                except services.PaymentError:
                    error = _("Введите депозит числом больше нуля.")
            if not error:
                from django.db import transaction as db_transaction

                try:
                    with db_transaction.atomic():
                        table.reserved_for, table.reserved_at = name, reserved_at
                        table.save(update_fields=["reserved_for", "reserved_at"])
                        if deposit_amount is not None:
                            services.take_deposit(table, name, deposit_amount,
                                                  request.POST.get("deposit_method", "cash"), request.user)
                except services.PaymentError as exc:
                    error = str(exc)
                    table.refresh_from_db()
                else:
                    messages.success(request, _("Стол забронирован."))
                    return HttpResponse(headers={"HX-Redirect": reverse("floor")})
    held = services.held_deposits(table)
    return render(request, "pos/_reserve_form.html", {
        "table": table, "error": error, "form": request.POST,
        "held_total": sum((d.amount for d in held), Decimal(0)), "held": held,
    })


# ---------- экран заказа ----------

@login_required
def order_detail(request, order_id):
    order = _get_order(request, order_id, open_only=False)
    if order.status != Order.Status.OPEN:
        return redirect("order_view", order_id=order.id)
    categories = list(
        Category.objects.filter(is_active=True).prefetch_related(
            Prefetch(
                "dishes",
                queryset=Dish.objects.filter(is_hidden=False).prefetch_related("modifiers"),
                to_attr="pos_dishes",
            )
        )
    )
    categories = [c for c in categories if c.pos_dishes]
    audit.record_view(request.user, order)
    weigh = request.GET.get("weigh", "")
    context = {"categories": categories, "weigh_dish_id": int(weigh) if weigh.isdigit() else None,
               **_panel_context(request, order)}
    return render(request, "pos/order.html", context)


@login_required
def order_panel(request, order_id):
    order = _get_order(request, order_id, open_only=False)
    if order.status != Order.Status.OPEN:
        # Заказ закрыт или отменён на другом устройстве: уводим страницу на план зала
        return HttpResponse(headers={"HX-Redirect": _home_url(request.user, order)})
    return _panel(request, order)


@login_required
def item_form(request, order_id, dish_id):
    order = _get_order(request, order_id)
    dish = get_object_or_404(Dish, id=dish_id, is_available=True, is_hidden=False)
    groups, extras = _modifier_groups(dish)
    return render(
        request,
        "pos/_item_form.html",
        {"order": order, "dish": dish, "item": None, "selected": set(), "quantity": 1, "comment": "",
         "weight": "", "groups": groups, "extras": extras},
    )


@login_required
@require_POST
def add_item(request, order_id, dish_id):
    order = _get_order(request, order_id)
    dish = get_object_or_404(Dish, id=dish_id, is_hidden=False)
    if not dish.is_available:
        return _panel(request, order, _("«%(name)s» сейчас нет в продаже.") % {"name": dish.name}, "error")
    modifiers = _clean_modifiers(request, dish)
    comment = _clean_comment(request)

    if dish.sold_by_weight:
        # Каждое взвешивание — отдельная позиция: две рыбы разного веса не складываются
        grams, error = _clean_weight(request, dish)
        if error:
            return _weight_form(request, order, dish, None, error)
        item = OrderItem.objects.create(order=order, dish=dish, quantity=1, weight_grams=grams, comment=comment,
                                        takeaway=_takeaway_mode(request, order))
        item.modifiers.set(modifiers)
        audit.record(request.user, audit.A.ITEM_ADD, order=order, amount=item.line_total,
                     detail=f"{order.place_label}: {dish.name}")
        _order_changed(order)
        return _panel(request, order)

    qty = _clean_qty(request)
    takeaway = _takeaway_mode(request, order)
    item = _find_same(order, dish, [m.id for m in modifiers], comment, takeaway)
    if item:
        item.quantity = min(item.quantity + qty, 99)
        item.save()
    else:
        item = OrderItem.objects.create(order=order, dish=dish, quantity=qty, comment=comment, takeaway=takeaway)
        item.modifiers.set(modifiers)
    audit.record(request.user, audit.A.ITEM_ADD, order=order, amount=dish.price * qty,
                 detail=f"{order.place_label}: {qty} x {dish.name}")
    _order_changed(order)
    return _panel(request, order)


@login_required
def edit_item(request, order_id, item_id):
    order = _get_order(request, order_id)
    item = get_object_or_404(OrderItem, id=item_id, order=order)
    if item.status != OrderItem.Status.NEW and not item.by_weight:
        raise Http404

    if request.method == "POST":
        if item.by_weight:
            grams, error = _clean_weight(request, item.dish)
            if error:
                return _weight_form(request, order, item.dish, item, error)
            item.weight_grams, item.quantity = grams, 1
            if item.status != OrderItem.Status.NEW:
                # Блюдо уже на кухне: меняется только вес (рыбу часто взвешивают после заказа)
                item.save(update_fields=["weight_grams", "quantity"])
                _order_changed(order)
                return _panel(request, order, _("Вес указан."))
        else:
            item.quantity = _clean_qty(request)
        item.comment = _clean_comment(request)
        item.save()
        item.modifiers.set(_clean_modifiers(request, item.dish))
        _order_changed(order)
        return _panel(request, order, _("Позиция изменена."))

    groups, extras = _modifier_groups(item.dish)
    return render(
        request,
        "pos/_item_form.html",
        {
            "order": order,
            "dish": item.dish,
            "item": item,
            "selected": {m.id for m in item.modifiers.all()},
            "quantity": item.quantity,
            "weight": item.weight_grams or "",
            "comment": item.comment,
            "groups": groups,
            "extras": extras,
        },
    )


def _change_sent_qty(request, order, item):
    """«−» и «+» у блюда, уже отправленного на кухню (официанту, администратору, владельцу).
    «+» добавляет порцию новой строкой: её отправляют на кухню, и повара о ней узнают.
    «−» убирает порцию; запись в журнал и уведомление владельцу, как при удалении блюда."""
    user = request.user
    if item.by_weight:
        return _panel(request, order)
    # «+» — это новая порция дозаказом, её может добавить любой, кто ведёт заказ (и кассир с паролем отмены)
    if request.POST.get("delta") != "1" and (not user.can_remove_sent_items or user.needs_cancel_pin):
        return _panel(request, order, _("Блюдо уже на кухне. Убрать его может администратор."), "error")
    if request.POST.get("delta") == "1":
        same = _find_same(order, item.dish, [m.id for m in item.modifiers.all()], item.comment, item.takeaway)
        if same:
            same.quantity = min(same.quantity + 1, 99)
            same.save()
        else:
            new = OrderItem.objects.create(order=order, dish=item.dish, quantity=1, comment=item.comment,
                                           takeaway=item.takeaway)
            new.modifiers.set(item.modifiers.all())
        _order_changed(order)
        return _panel(request, order, _("Порция добавлена. Отправьте её на кухню."))
    if item.quantity <= 1:
        return remove_item(request, order.id, item.id)
    was = item.quantity
    item.quantity = 1
    removed = item.line_total  # цена одной порции (с добавками)
    audit.record(user, audit.A.ITEM_REMOVE, order=order, amount=removed,
                 detail=f"{item.dish.name} −1 ({was} → {was - 1})")
    telegram.notify_item_removed(order, item, user)
    item.quantity = was - 1
    item.save()
    _order_changed(order)
    return _panel(request, order, _("Порция убрана."))


@login_required
@require_POST
def change_qty(request, order_id, item_id):
    order = _get_order(request, order_id)
    item = get_object_or_404(OrderItem, id=item_id, order=order)
    if item.status != OrderItem.Status.NEW:
        return _change_sent_qty(request, order, item)
    if item.by_weight and request.POST.get("delta") == "1":
        # Вторую порцию на вес добавляют отдельным взвешиванием
        return _panel(request, order)
    if request.POST.get("delta") != "1" and item.quantity <= 1 and request.user.needs_cancel_pin:
        # Убрать последнюю порцию то же самое, что удалить блюдо: нужен пароль отмены
        return _remove_form(request, order, item)
    item.quantity += 1 if request.POST.get("delta") == "1" else -1
    if item.quantity <= 0:
        item.delete()
    else:
        item.quantity = min(item.quantity, 99)
        item.save()
    _order_changed(order)
    return _panel(request, order)


MAX_QTY = 99


@login_required
def set_qty(request, order_id, item_id):
    """Ввод количества цифрами: нажали на число в корзине, ввели 20 порций сомсы.
    Для блюда, уже ушедшего на кухню: больше — лишние порции новой строкой (их отправят на кухню),
    меньше — порции убираются с записью в журнал и уведомлением, как кнопкой «−»."""
    order = _get_order(request, order_id)
    item = get_object_or_404(OrderItem, id=item_id, order=order)
    user = request.user
    sent = item.status != OrderItem.Status.NEW
    if item.by_weight or (sent and (not user.can_remove_sent_items or user.needs_cancel_pin)):
        raise Http404

    if request.method != "POST":
        return render(request, "pos/_qty_form.html", {"order": order, "item": item, "dish": item.dish,
                                                      "max_qty": MAX_QTY})
    try:
        qty = int(request.POST.get("quantity", ""))
    except ValueError:
        return _panel(request, order, _("Введите количество цифрами."), "error")
    qty = max(1, min(qty, MAX_QTY))
    diff = qty - item.quantity
    if diff == 0:
        return _panel(request, order)
    if not sent:
        item.quantity = qty
        item.save()
        _order_changed(order)
        return _panel(request, order, _("Позиция изменена."))
    if diff > 0:
        same = _find_same(order, item.dish, [m.id for m in item.modifiers.all()], item.comment, item.takeaway)
        if same:
            same.quantity = min(same.quantity + diff, MAX_QTY)
            same.save()
        else:
            new = OrderItem.objects.create(order=order, dish=item.dish, quantity=diff, comment=item.comment,
                                           takeaway=item.takeaway)
            new.modifiers.set(item.modifiers.all())
        _order_changed(order)
        return _panel(request, order, _("Порция добавлена. Отправьте её на кухню."))
    was, removed_n = item.quantity, -diff
    item.quantity = removed_n
    removed = item.line_total
    audit.record(user, audit.A.ITEM_REMOVE, order=order, amount=removed,
                 detail=f"{item.dish.name} −{removed_n} ({was} → {qty})")
    telegram.notify_item_removed(order, item, user)
    item.quantity = qty
    item.save()
    _order_changed(order)
    return _panel(request, order, _("Порция убрана."))


@login_required
@require_POST
def remove_item(request, order_id, item_id):
    """Удалить позицию. Уже отправленную на кухню может убрать только тот, кто вправе отменять такие заказы."""
    order = _get_order(request, order_id)
    item = get_object_or_404(OrderItem, id=item_id, order=order)
    one = request.POST.get("one") == "1" and item.quantity > 1 and not item.by_weight
    error = _pin_error(request)
    if error:
        return _remove_form(request, order, item, error, one=one)
    if item.status != OrderItem.Status.NEW and not request.user.can_remove_sent_items:
        return _panel(request, order, _("Блюдо уже на кухне. Убрать его может администратор."), "error")
    if one:
        # «−» с паролем отмены: убирается одна порция, остальные остаются (20 шашлыков → 19)
        was = item.quantity
        if item.status != OrderItem.Status.NEW:
            item.quantity = 1
            audit.record(request.user, audit.A.ITEM_REMOVE, order=order, amount=item.line_total,
                         detail=f"{item.dish.name} −1 ({was} → {was - 1})")
            telegram.notify_item_removed(order, item, request.user)
        item.quantity = was - 1
        item.save()
        _order_changed(order)
        return _panel(request, order, _("Порция убрана."))
    if item.status != OrderItem.Status.NEW:
        audit.record(request.user, audit.A.ITEM_REMOVE, order=order, amount=item.line_total,
                     detail=f"{item.dish.name} × {item.quantity}")
        telegram.notify_item_removed(order, item, request.user)
    item.delete()
    _order_changed(order)
    return _panel(request, order, _("Позиция удалена."))


@login_required
@require_POST
def toggle_takeaway_mode(request, order_id):
    """Кнопка «С собой»: следующие блюда этого стола идут с собой (без обслуживания), повторное нажатие выключает."""
    order = _get_order(request, order_id)
    if not order.is_dine_in:
        return _panel(request, order)
    key = f"takeaway_mode_{order.id}"
    request.session[key] = not request.session.get(key, False)
    if request.session[key]:
        return _panel(request, order, _("Режим «С собой»: новые блюда упакуют, обслуживание на них не начисляется."))
    return _panel(request, order)


@login_required
@require_POST
def toggle_item_takeaway(request, order_id, item_id):
    """Пометить или снять «с собой» у ещё не отправленной позиции заказа за столом."""
    order = _get_order(request, order_id)
    item = get_object_or_404(OrderItem, id=item_id, order=order, status=OrderItem.Status.NEW)
    if order.is_dine_in:
        item.takeaway = not item.takeaway
        item.save(update_fields=["takeaway"])
        _order_changed(order)
    return _panel(request, order)


@login_required
def remove_item_form(request, order_id, item_id):
    """Окно «Убрать блюдо?» с паролем отмены."""
    order = _get_order(request, order_id)
    item = get_object_or_404(OrderItem, id=item_id, order=order)
    return _remove_form(request, order, item, one=request.GET.get("one") == "1")


@login_required
@require_POST
def send_to_kitchen(request, order_id):
    order = _get_order(request, order_id)
    if not services.send_to_kitchen(order):
        return _panel(request, order)
    return _panel(request, order, _("Заказ отправлен на кухню."))


@login_required
@require_POST
def serve_item(request, order_id, item_id):
    order = _get_order(request, order_id)
    item = get_object_or_404(OrderItem, id=item_id, order=order, status=OrderItem.Status.READY)
    item.status = OrderItem.Status.SERVED
    item.save()
    return _panel(request, order)


@login_required
@require_POST
def serve_all(request, order_id):
    order = _get_order(request, order_id)
    services.serve_ready(order)
    return _panel(request, order, _("Готовые блюда отмечены поданными."))


@login_required
@require_POST
def set_type(request, order_id):
    """Сменить тип заказа. Для заказа в зале сначала выбирается стол."""
    order = _get_order(request, order_id)
    order_type = request.POST.get("type")
    if order_type == Order.OrderType.DINE_IN:
        response = move_form(request, order_id)
        response.headers["HX-Retarget"] = "#modal"
        return response
    if order_type not in Order.OrderType.values:
        raise Http404
    services.set_order_type(order, order_type)
    response = _panel(request, order, _("Тип заказа: %(t)s.") % {"t": order.get_order_type_display()})
    if order_type == Order.OrderType.DELIVERY and not order.delivery_address:
        response.headers["HX-Trigger-After-Settle"] = json.dumps({"openCustomer": True})
    return response


@login_required
def customer_form(request, order_id):
    order = _get_order(request, order_id)
    error = ""
    if request.method == "POST":
        order.customer_name = request.POST.get("customer_name", "").strip()[:100]
        order.customer_phone = request.POST.get("customer_phone", "").strip()[:30]
        order.delivery_address = request.POST.get("delivery_address", "").strip()[:255]
        order.note = request.POST.get("note", "").strip()[:255]
        if order.order_type == Order.OrderType.DELIVERY and not (order.customer_phone and order.delivery_address):
            error = _("Для доставки нужны телефон и адрес.")
        else:
            order.save(update_fields=["customer_name", "customer_phone", "delivery_address", "note"])
            return _panel(request, order, _("Данные клиента сохранены."))
        response = render(request, "pos/_customer_form.html", {"order": order, "error": error})
        response.headers["HX-Retarget"] = "#modal"
        return response
    return render(request, "pos/_customer_form.html", {"order": order, "error": error})


@login_required
def discount_form(request, order_id):
    if not has_role(request.user, DISCOUNT_ROLES):
        raise PermissionDenied
    order = _get_order(request, order_id)
    error = ""
    if request.method == "POST":
        max_percent = None if has_role(request.user, MANAGE_ROLES) else CafeSettings.get().cashier_max_discount
        error = services.apply_discount(
            order, request.POST.get("kind", ""), request.POST.get("value", ""), request.POST.get("reason", ""),
            max_percent=max_percent,
        )
        if not error:
            if order.discount_kind:
                telegram.notify_discount(order, request.user)
                audit.record(
                    request.user, audit.A.DISCOUNT, order=order,
                    amount=order.discount_for(order.totals["subtotal"]),
                    detail=f"{order.get_discount_kind_display()} {order.discount_value}: {order.discount_reason or '—'}",
                )
            else:
                audit.record(request.user, audit.A.DISCOUNT, order=order, detail=_("Скидка снята"))
            message = _("Скидка снята.") if not order.discount_kind else _("Скидка применена.")
            return _panel(request, order, message)
        response = render(request, "pos/_discount_form.html", {"order": order, "error": error, "form": request.POST})
        response.headers["HX-Retarget"] = "#modal"
        return response
    return render(request, "pos/_discount_form.html", {"order": order, "error": error, "form": {}})


# ---------- пречек, отмена и перенос ----------

@login_required
@require_POST
def request_bill(request, order_id):
    """Пречек: гость попросил счёт. Стол переходит в статус «ожидает оплаты».
    Если принтер для чеков один, пречек сразу уходит на него, а официант остаётся в заказе: так быстрее.
    Нет принтера или их несколько — открывается страница пречека (печать через браузер или выбор принтера)."""
    order = _get_order(request, order_id)
    is_htmx = bool(request.headers.get("HX-Request"))
    if not order.items.exists():
        return _panel(request, order) if is_htmx else redirect("order_detail", order_id=order.id)
    # Добавили блюда и сразу попросили счёт: новые блюда сначала уходят на кухню отдельным дозаказом,
    # иначе в пречеке они есть, а повар о них не знает
    services.send_to_kitchen(order)
    order.bill_requested_at = timezone.now()
    order.save(update_fields=["bill_requested_at"])

    printers = list(receipt_printers())
    if len(printers) == 1:
        from printing import services as print_services
        from printing.models import PrintAgent

        print_services.queue_receipt(order, printer=printers[0], precheck=True)
        order = _get_order(request, order_id)  # печать оставляет на заказе подставленный список позиций: берём чистый объект
        if PrintAgent.get().is_online:
            message, kind = _("Отправлено на принтер."), "success"
        else:
            message, kind = _("Задание в очереди, но программа печати не на связи. Чек напечатается, когда она запустится."), "warning"
        if is_htmx and request.POST.get("list") == "1":
            return toast(HttpResponse(status=204), message, kind)  # из списка заказов: только сообщение, экран не меняется
        if is_htmx:
            return _panel(request, order, message, kind)
        getattr(messages, kind)(request, message)
        return redirect("order_detail", order_id=order.id)

    url = reverse("precheck", args=[order.id])
    if is_htmx:
        response = HttpResponse(status=204)
        response["HX-Redirect"] = url
        return response
    return redirect(url)


@login_required
def precheck(request, order_id):
    order = _get_order(request, order_id)
    return render(request, "pos/receipt.html", {
        **_panel_context(request, order), "is_precheck": True, "printers": list(receipt_printers()),
    })


@login_required
def cancel_form(request, order_id):
    order = _get_order(request, order_id)
    if not _can_cancel(request.user, order):
        raise PermissionDenied
    return render(request, "pos/_cancel_form.html", {"order": order})


@login_required
@require_POST
def cancel_order(request, order_id):
    order = _get_order(request, order_id)
    htmx = bool(request.headers.get("HX-Request"))
    reason = request.POST.get("reason", "")
    # Сначала пароль отмены (у кассира он обязателен), потом права на этот заказ
    error = _pin_error(request)
    if not error and not _can_cancel(request.user, order):
        error = _("Заказ уже на кухне. Отменить его может администратор.")
    if error:
        if htmx:
            return render(request, "pos/_cancel_form.html", {"order": order, "error": error, "reason": reason})
        messages.error(request, error)
        return redirect("order_detail", order_id=order.id)
    services.cancel_order(order, request.user, reason)
    telegram.notify_order_cancelled(order, request.user)
    messages.success(request, _("Заказ №%(n)s отменён.") % {"n": order.number})
    if htmx:
        return HttpResponse(headers={"HX-Redirect": _home_url(request.user, order)})
    return redirect(_home_url(request.user, order))


@login_required
def assign_waiter(request, order_id):
    """Кассир или администратор передаёт открытый заказ другому официанту (например, первый ушёл со смены).
    Доля за заказ при оплате уйдёт новому официанту."""
    from accounts.models import User
    from core import audit

    order = _get_order(request, order_id)
    if not (request.user.can_take_payment or request.user.manages_cafe):
        raise PermissionDenied
    if order.status != Order.Status.OPEN:
        raise Http404
    waiters = User.objects.filter(is_active=True, role=User.Role.WAITER).order_by("first_name", "username")
    if request.method == "POST":
        new = get_object_or_404(waiters, id=request.POST.get("waiter_id") or 0)
        if new.id != order.waiter_id:
            old = order.waiter
            order.waiter = new
            order.save(update_fields=["waiter"])
            place = (_("стол %(n)s") % {"n": order.table.number}) if order.table_id else f"№{order.number}"
            audit.record(request.user, audit.A.WAITER_CHANGE, order=order, detail=_("%(place)s: %(old)s → %(new)s") % {
                "place": place, "old": old.get_full_name() or old.username, "new": new.get_full_name() or new.username,
            })
            messages.success(request, _("Заказ передан официанту %(name)s.") % {"name": new.first_name or new.username})
        return redirect("order_detail", order_id=order.id)
    return render(request, "pos/_assign_waiter.html", {"order": order, "waiters": waiters})


@login_required
def move_form(request, order_id):
    order = _get_order(request, order_id)
    busy = _busy_table_ids()
    halls = []
    for hall in Hall.objects.prefetch_related("tables"):
        free = [t for t in hall.tables.all() if t.id not in busy and t.id != order.table_id]
        if free:
            halls.append({"hall": hall, "tables": free})
    return render(request, "pos/_move_form.html", {"order": order, "halls": halls})


@login_required
@require_POST
def move_order(request, order_id, table_id):
    order = _get_order(request, order_id)
    table = get_object_or_404(Table, id=table_id)
    if order.status != Order.Status.OPEN:
        return redirect("order_detail", order_id=order.id)
    # Проверка «стол свободен» и перенос — одной транзакцией: два официанта не пересадят гостей на один стол
    with transaction.atomic():
        if table.id != order.table_id and table.id in _busy_table_ids():
            messages.error(request, _("Стол %(n)s уже занят.") % {"n": table.number})
            return redirect("order_detail", order_id=order.id)
        # Пустые черновики на целевом столе закрываем, чтобы не было двух открытых заказов
        Order.objects.filter(table=table, status=Order.Status.OPEN).exclude(id=order.id).annotate(
            n_items=Count("items")
        ).filter(n_items=0).update(status=Order.Status.CANCELLED, closed_at=timezone.now())
        was_dine_in = order.is_dine_in
        services.set_order_type(order, Order.OrderType.DINE_IN, table)
        if table.is_reserved:
            table.reserved_for, table.reserved_at = "", None
            table.save(update_fields=["reserved_for", "reserved_at"])
    if was_dine_in:
        messages.success(request, _("Заказ перенесён на стол %(n)s.") % {"n": table.number})
    else:
        messages.success(request, _("Заказ теперь в зале, стол %(n)s.") % {"n": table.number})
    return redirect("order_detail", order_id=order.id)


# ---------- оплата ----------

def _parse_split(request, items):
    """Возвращает {id позиции: сколько штук оплачиваем} или None, если данные неверны."""
    selected = {}
    for item in items:
        raw = request.POST.get(f"qty_{item.id}", "0").strip() or "0"
        try:
            qty = int(raw)
        except ValueError:
            return None
        if qty < 0 or qty > item.quantity:
            return None
        if qty:
            selected[item.id] = qty
    return selected


def split_totals(order, part_subtotal, part_no_service=0):
    """Суммы для оплачиваемой части счёта: процентная скидка и обслуживание пропорциональны,
    скидка фиксированной суммой остаётся на остатке заказа."""
    part = Order(service_percent=order.service_percent)
    if order.discount_kind == Order.DiscountKind.PERCENT:
        part.discount_kind, part.discount_value = order.discount_kind, order.discount_value
    return part.totals_for(part_subtotal, part_no_service)


def _quick_amounts(due):
    """Подсказки для наличных: ближайшие «круглые» суммы сверху."""
    due = int(due)
    amounts = set()
    for step in (5000, 10000, 50000, 100000):
        value = -(-due // step) * step
        if value > due:
            amounts.add(value)
    for bill in (200000, 500000):
        if bill > due:
            amounts.add(bill)
    return sorted(amounts)[:4]


@login_required
def pay(request, order_id):
    if not request.user.can_take_payment:
        raise PermissionDenied

    idempotency_key = request.POST.get("idempotency_key", "").strip() or None
    if request.method == "POST" and idempotency_key:
        existing = Payment.objects.filter(idempotency_key=idempotency_key).first()
        if existing:
            return redirect("receipt", order_id=existing.order_id)

    order = _get_order(request, order_id)
    if not order.can_pay:
        return redirect("order_detail", order_id=order.id)
    if any(item.needs_weight for item in order.items.all()):
        messages.error(request, _("Укажите вес у блюд на вес, потом принимайте оплату."))
        return redirect("order_detail", order_id=order.id)

    allowed = CafeSettings.get().payment_methods
    items = list(_items(order))
    order._prefetched_objects_cache = {"items": items}
    totals = order.totals
    split = request.GET.get("split") == "1"
    error = ""
    selected = {}
    method = allowed[0]
    received_raw = card_raw = debt_raw = click_raw = ""

    if request.method == "POST":
        split = request.POST.get("split") == "1"
        method = request.POST.get("method", "")
        received_raw = request.POST.get("received", "").strip()
        card_raw = request.POST.get("card", "").strip()
        pay_all = True

        if split:
            selected = _parse_split(request, items)
            if selected is None:
                selected, error = {}, _("Проверьте количество у позиций.")
            elif not selected:
                error = _("Выберите хотя бы одну позицию.")
            else:
                pay_all = all(selected.get(i.id) == i.quantity for i in items)

        if pay_all:
            due = order.amount_due
        else:
            due = split_totals(
                order,
                sum(i.unit_total * selected[i.id] for i in items if i.id in selected),
                sum(i.unit_total * selected[i.id] for i in items if i.id in selected and i.takeaway),
            )["total"]

        debt_raw = request.POST.get("debt", "").strip()
        click_raw = request.POST.get("click", "").strip()
        debt_part = click_part = Decimal(0)
        if not error:
            try:
                debt_part = services.parse_mixed_debt(method, debt_raw, allowed)
                click_part = services.parse_mixed_click(method, click_raw, allowed)
            except services.PaymentError as exc:
                error = str(exc)

        if not error and (method == Order.PaymentMethod.DEBT or debt_part > 0) and Order.PaymentMethod.DEBT in allowed:
            # Гость, на которого пишем долг: имя и телефон сохраняем в заказе (они же попадут в чек)
            debt_name = request.POST.get("debt_name", "").strip()[:100]
            debt_phone = request.POST.get("debt_phone", "").strip()[:20]
            if debt_name and debt_phone:
                order.customer_name, order.customer_phone = debt_name, debt_phone
                order.save(update_fields=["customer_name", "customer_phone"])

        if not error:
            try:
                received, card = services.check_payment(
                    due, method, received_raw, card_raw, allowed, debt=debt_part, click=click_part
                )
                target = services.pay_order(
                    order, request.user, method, received, card,
                    selected=None if pay_all else selected,
                    idempotency_key=idempotency_key,
                    debt=debt_part,
                    click=click_part,
                )
            except services.PaymentError as exc:
                error = str(exc)
            else:
                return redirect("receipt", order_id=target.id)

    for item in items:
        item.picked = selected.get(item.id, 0)
    if split:
        lines = [(i, selected[i.id]) for i in items if i.id in selected]
    else:
        lines = [(i, i.quantity) for i in items]
    return render(
        request,
        "pos/pay.html",
        {
            "order": order,
            "items": items,
            "totals": totals,
            "split": split,
            "error": error,
            "method": method if method in allowed else allowed[0],
            "methods": allowed,
            "received": received_raw,
            "card": card_raw,
            "debt_raw": debt_raw,
            "click_raw": click_raw,
            "quick_amounts": _quick_amounts(order.amount_due),
            "due_total": order.amount_due,
            "deposit": order.deposit_applied,
            "shortages": find_shortages(lines),
            "percent_discount": order.discount_value if order.discount_kind == Order.DiscountKind.PERCENT else 0,
            "idempotency_key": idempotency_key or str(uuid.uuid4()),
        },
    )


@login_required
def receipt(request, order_id):
    if not request.user.can_take_payment:
        raise PermissionDenied
    order = get_object_or_404(
        Order.objects.select_related("table__hall", "waiter", "cashier"), id=order_id, status=Order.Status.PAID
    )
    order_items = list(_items(order))
    order._prefetched_objects_cache = {"items": order_items}
    remaining = _open_orders().filter(table=order.table).first() if order.table_id else None
    review_url = request.build_absolute_uri(reverse("order_review", args=[reviews.make_token(order)]))
    entry = order.loyalty_entries.filter(points__gt=0).select_related("customer").first()
    return render(
        request,
        "pos/receipt.html",
        {
            "order": order,
            "order_items": order_items,
            "totals": order.totals,
            "change": order.change,
            "remaining": remaining,
            "is_precheck": False,
            "review_qr": _qr_svg(review_url),
            "loyalty_entry": entry,
            "printers": list(receipt_printers()),
        },
    )


# ---------- кассовые смены и возвраты ----------

@login_required
@require_POST
def refund_order_view(request, order_id):
    """Возврат оплаченного заказа."""
    if not request.user.can_take_payment:
        raise PermissionDenied
    order = get_object_or_404(Order, id=order_id, status=Order.Status.PAID)
    error = _pin_error(request)
    if error:
        messages.error(request, error)
        return redirect("order_view", order_id=order.id)
    if request.user.needs_cancel_pin:
        # Кассир возвращает только чеки своей текущей смены, старые чеки возвращает администратор
        shift = services.get_current_shift()
        if shift is None or order.shift_id != shift.id:
            messages.error(request, _("Кассир может вернуть только чек текущей смены. Старый чек возвращает администратор."))
            return redirect("order_view", order_id=order.id)
    reason = request.POST.get("reason", "").strip()
    return_to_stock = request.POST.get("return_to_stock") == "1"
    try:
        services.refund_order(order, request.user, reason, return_to_stock=return_to_stock)
    except services.PaymentError as exc:
        messages.error(request, str(exc))
    else:
        telegram.notify_refund(order, request.user, reason)
        messages.success(request, _("Заказ №%(id)s успешно возвращён.") % {"id": order.number})
    return redirect("order_view", order_id=order.id)


def _next_or_floor(request):
    """Куда вернуться после действия со сменой: только на свой же сайт."""
    target = request.POST.get("next", "")
    if url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}):
        return target
    return "floor"


@login_required
def shift_current(request):
    """Страница или модальное окно информации о текущей кассовой смене (X-отчёт)."""
    if not request.user.can_take_payment:
        raise PermissionDenied
    shift = services.get_current_shift()
    return render(request, "pos/shift_modal.html", {"shift": shift})


@login_required
@require_POST
def shift_open(request):
    """Открытие кассовой смены."""
    if not request.user.can_take_payment:
        raise PermissionDenied
    initial_cash = request.POST.get("initial_cash", "0")
    try:
        shift = services.open_shift(request.user, initial_cash=initial_cash)
        messages.success(request, _("Кассовая смена №%(id)s открыта.") % {"id": shift.id})
    except services.PaymentError as exc:
        messages.error(request, str(exc))
    return redirect(_next_or_floor(request))


@login_required
@require_POST
def shift_close(request):
    """Закрытие кассовой смены со сверкой наличных (Z-отчёт)."""
    if not request.user.can_take_payment:
        raise PermissionDenied
    shift = services.get_current_shift()
    if not shift:
        messages.error(request, _("Нет открытой кассовой смены."))
        return redirect("floor")
    actual_cash = request.POST.get("actual_cash", "")
    comment = request.POST.get("comment", "")
    logout_after = request.POST.get("logout_after") == "1"
    try:
        closed = services.close_shift(shift, actual_cash=actual_cash, comment=comment, user=request.user)
        telegram.notify_shift_closed(closed)
        diff_str = _(" (расхождение: %(d)s сум)") % {"d": closed.difference} if closed.difference else ""
        messages.success(request, _("Кассовая смена №%(id)s закрыта%(diff)s.") % {"id": closed.id, "diff": diff_str})
        if logout_after:
            auth_logout(request)
            return redirect("login")
    except services.PaymentError as exc:
        messages.error(request, str(exc))
    return redirect("floor")


@login_required
@require_POST
def shift_movement(request):
    """Внесение или изъятие наличных."""
    if not request.user.can_take_payment:
        raise PermissionDenied
    shift = services.get_current_shift()
    if not shift:
        messages.error(request, _("Сначала откройте кассовую смену."))
        return redirect("floor")
    kind = request.POST.get("kind", "")
    amount = request.POST.get("amount", "")
    reason = request.POST.get("reason", "")
    try:
        mv = services.add_cash_movement(shift, kind=kind, amount_raw=amount, reason=reason, user=request.user)
        messages.success(
            request,
            _("Операция «%(kind)s» на сумму %(amount)s сум выполнена.") % {
                "kind": mv.get_kind_display(),
                "amount": mv.amount,
            },
        )
    except services.PaymentError as exc:
        messages.error(request, str(exc))
    return redirect(_next_or_floor(request))



# ---------- долги гостей ----------

@login_required
def debts(request):
    """Кто сколько должен. Видят те, кто принимает оплату."""
    if not request.user.can_take_payment:
        raise PermissionDenied
    from django.db.models import Sum

    from .models import Customer

    rows = list(
        Customer.objects.annotate(balance=Sum("debt_entries__amount"))
        .filter(balance__gt=0)
        .order_by("-balance")
    )
    total = sum((c.balance for c in rows), Decimal(0))
    return render(request, "pos/debts.html", {"rows": rows, "total": total})


@login_required
def debt_detail(request, customer_id):
    """История долга гостя и приём погашения в текущую смену."""
    if not request.user.can_take_payment:
        raise PermissionDenied
    from .models import Customer, DebtEntry

    customer = get_object_or_404(Customer, id=customer_id)
    error = ""
    if request.method == "POST":
        try:
            amount = services.parse_money(request.POST.get("amount", ""))
            services.repay_debt(customer, amount, request.POST.get("method", ""), request.user,
                                request.POST.get("comment", ""))
        except services.PaymentError as exc:
            error = str(exc)
        else:
            messages.success(request, _("Погашение принято."))
            return redirect("debt_detail", customer_id=customer.id)
    entries = DebtEntry.objects.filter(customer=customer).select_related("order", "cashier")[:200]
    return render(request, "pos/debt_detail.html", {
        "customer": customer,
        "debt": customer.debt,
        "entries": entries,
        "error": error,
        "shift_open": services.get_current_shift() is not None,
        "methods": DebtEntry.Method.choices,
    })


# ---------- перенос блюд между столами и объединение ----------

@login_required
def move_items(request, order_id):
    """Окно «Перенести блюда»: часть позиций или все (объединение) — в другой открытый заказ или на свободный стол."""
    order = _get_order(request, order_id)
    items = list(order.items.select_related("dish").prefetch_related("modifiers"))
    targets = Order.objects.filter(status=Order.Status.OPEN).exclude(id=order.id).select_related("table__hall", "waiter")
    targets = [t for t in targets.order_by("table__hall__id", "table__number", "id") if t.items.exists()]
    busy = _busy_table_ids()
    free_tables = [t for t in Table.objects.select_related("hall").order_by("hall__id", "number") if t.id not in busy]
    error = ""

    if request.method == "POST":
        selection = {}
        for item in items:
            raw = request.POST.get(f"qty_{item.id}", "0").strip()
            if raw.isdigit() and int(raw) > 0:
                selection[item.id] = min(int(raw), item.quantity)
        target_raw = request.POST.get("target", "")
        target = None
        if target_raw.startswith("order:") and target_raw[6:].isdigit():
            target = next((t for t in targets if t.id == int(target_raw[6:])), None)
        elif target_raw.startswith("table:") and target_raw[6:].isdigit():
            table = next((t for t in free_tables if t.id == int(target_raw[6:])), None)
            if table:
                target = services.create_order(request.user, Order.OrderType.DINE_IN, table)
        if target is None:
            error = _("Выберите, куда перенести.")
        else:
            try:
                moved = services.move_items(order, target, selection)
            except services.PaymentError as exc:
                error = str(exc)
                if target_raw.startswith("table:") and not target.items.exists():
                    target.delete()  # пустой черновик на свободном столе не оставляем
            else:
                order.refresh_from_db()
                place = target.place_label
                if order.status == Order.Status.CANCELLED:
                    messages.success(request, _("Столы объединены: всё перенесено в %(p)s.") % {"p": place})
                else:
                    messages.success(request, _("Перенесено позиций: %(n)s → %(p)s.") % {"n": moved, "p": place})
                response = HttpResponse("")
                response["HX-Redirect"] = reverse("order_detail", args=[target.id])
                return response

    return render(request, "pos/_move_items_form.html", {
        "order": order, "items": items, "targets": targets, "free_tables": free_tables, "error": error,
    })
