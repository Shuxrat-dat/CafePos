import json
from collections import OrderedDict
from datetime import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Prefetch
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from accounts.permissions import DISCOUNT_ROLES, has_role
from core.models import CafeSettings
from inventory.services import find_shortages
from menu.models import Category, Dish

from . import services
from .models import GuestOrder, Hall, Order, OrderItem, Table


# ---------- вспомогательные функции ----------

def toast(response, message, kind="success"):
    """Всплывающее уведомление для ответов HTMX."""
    response.headers["HX-Trigger"] = json.dumps({"toast": {"message": str(message), "kind": kind}})
    return response


def _get_order(request, order_id, open_only=True):
    """Находит заказ и проверяет, что пользователь вправе с ним работать."""
    order = get_object_or_404(Order.objects.select_related("table__hall", "waiter"), id=order_id)
    user = request.user
    if not user.sees_all_orders and order.waiter_id != user.id:
        raise PermissionDenied
    if open_only and order.status != Order.Status.OPEN:
        raise Http404
    return order


def _can_cancel(user, order):
    return order.status == Order.Status.OPEN and (not order.has_sent_items or user.can_cancel_sent_orders)


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


def _clean_comment(request):
    return request.POST.get("comment", "").strip()[:200]


def _find_same(order, dish, modifier_ids, comment):
    items = order.items.filter(dish=dish, status=OrderItem.Status.NEW, comment=comment).prefetch_related("modifiers")
    for item in items:
        if {m.id for m in item.modifiers.all()} == set(modifier_ids):
            return item
    return None


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
            table.locked = bool(order) and not user.sees_all_orders and order.waiter_id != user.id
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
        counter_orders = [o for o in counter_orders if o.waiter_id == user.id]
    counter_orders.sort(key=lambda o: o.created_at)
    counts["total"] = sum(counts.values())
    return {"halls": halls, "counts": counts, "counter_orders": counter_orders}


@login_required
def floor(request):
    if request.user.role == "cook" and not request.user.is_superuser:
        return redirect("kitchen_board")
    return render(request, "pos/floor.html", _floor_context(request))


@login_required
def floor_grid(request):
    return render(request, "pos/_floor_grid.html", _floor_context(request))


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
    elif not request.user.sees_all_orders and order.waiter_id != request.user.id:
        raise PermissionDenied
    if table.is_reserved:
        # Гость по брони пришёл: бронь снимается, стол становится занятым
        table.reserved_for, table.reserved_at = "", None
        table.save(update_fields=["reserved_for", "reserved_at"])
    return redirect("order_detail", order_id=order.id)


@login_required
def reserve_table(request, table_id):
    table = get_object_or_404(Table.objects.select_related("hall"), id=table_id)
    error = ""
    if request.method == "POST":
        if request.POST.get("action") == "clear":
            table.reserved_for, table.reserved_at = "", None
            table.save(update_fields=["reserved_for", "reserved_at"])
            messages.success(request, _("Бронь снята."))
            return HttpResponse(headers={"HX-Redirect": reverse("floor")})
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
        if not error:
            table.reserved_for, table.reserved_at = name, reserved_at
            table.save(update_fields=["reserved_for", "reserved_at"])
            messages.success(request, _("Стол забронирован."))
            return HttpResponse(headers={"HX-Redirect": reverse("floor")})
    return render(request, "pos/_reserve_form.html", {"table": table, "error": error, "form": request.POST})


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
    context = {"categories": categories, **_panel_context(request, order)}
    return render(request, "pos/order.html", context)


@login_required
def order_panel(request, order_id):
    order = _get_order(request, order_id, open_only=False)
    if order.status != Order.Status.OPEN:
        # Заказ закрыт или отменён на другом устройстве: уводим страницу на план зала
        return HttpResponse(headers={"HX-Redirect": reverse("floor")})
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
         "groups": groups, "extras": extras},
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
    qty = _clean_qty(request)

    item = _find_same(order, dish, [m.id for m in modifiers], comment)
    if item:
        item.quantity = min(item.quantity + qty, 99)
        item.save()
    else:
        item = OrderItem.objects.create(order=order, dish=dish, quantity=qty, comment=comment)
        item.modifiers.set(modifiers)
    _order_changed(order)
    return _panel(request, order)


@login_required
def edit_item(request, order_id, item_id):
    order = _get_order(request, order_id)
    item = get_object_or_404(OrderItem, id=item_id, order=order, status=OrderItem.Status.NEW)

    if request.method == "POST":
        item.comment = _clean_comment(request)
        item.quantity = _clean_qty(request)
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
            "comment": item.comment,
            "groups": groups,
            "extras": extras,
        },
    )


@login_required
@require_POST
def change_qty(request, order_id, item_id):
    order = _get_order(request, order_id)
    item = get_object_or_404(OrderItem, id=item_id, order=order, status=OrderItem.Status.NEW)
    item.quantity += 1 if request.POST.get("delta") == "1" else -1
    if item.quantity <= 0:
        item.delete()
    else:
        item.quantity = min(item.quantity, 99)
        item.save()
    _order_changed(order)
    return _panel(request, order)


@login_required
@require_POST
def remove_item(request, order_id, item_id):
    """Удалить позицию. Уже отправленную на кухню может убрать только тот, кто вправе отменять такие заказы."""
    order = _get_order(request, order_id)
    item = get_object_or_404(OrderItem, id=item_id, order=order)
    if item.status != OrderItem.Status.NEW and not request.user.can_cancel_sent_orders:
        return _panel(request, order, _("Блюдо уже на кухне. Убрать его может администратор."), "error")
    item.delete()
    _order_changed(order)
    return _panel(request, order, _("Позиция удалена."))


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
        error = services.apply_discount(
            order, request.POST.get("kind", ""), request.POST.get("value", ""), request.POST.get("reason", "")
        )
        if not error:
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
    """Пречек: гость попросил счёт. Стол переходит в статус «ожидает оплаты»."""
    order = _get_order(request, order_id)
    if not order.items.exists():
        return redirect("order_detail", order_id=order.id)
    order.bill_requested_at = timezone.now()
    order.save(update_fields=["bill_requested_at"])
    return redirect("precheck", order_id=order.id)


@login_required
def precheck(request, order_id):
    order = _get_order(request, order_id)
    return render(request, "pos/receipt.html", {**_panel_context(request, order), "is_precheck": True})


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
    if not _can_cancel(request.user, order):
        messages.error(request, _("Заказ уже на кухне. Отменить его может администратор."))
        return redirect("order_detail", order_id=order.id)
    services.cancel_order(order, request.user, request.POST.get("reason", ""))
    messages.success(request, _("Заказ №%(n)s отменён.") % {"n": order.id})
    return redirect("floor")


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


def split_totals(order, part_subtotal):
    """Суммы для оплачиваемой части счёта: процентная скидка и обслуживание пропорциональны,
    скидка фиксированной суммой остаётся на остатке заказа."""
    part = Order(service_percent=order.service_percent)
    if order.discount_kind == Order.DiscountKind.PERCENT:
        part.discount_kind, part.discount_value = order.discount_kind, order.discount_value
    return part.totals_for(part_subtotal)


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
    order = _get_order(request, order_id)
    if not order.can_pay:
        return redirect("order_detail", order_id=order.id)

    allowed = CafeSettings.get().payment_methods
    items = list(_items(order))
    order._prefetched_objects_cache = {"items": items}
    totals = order.totals
    split = request.GET.get("split") == "1"
    error = ""
    selected = {}
    method = allowed[0]
    received_raw = card_raw = ""

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
            due = totals["total"]
        else:
            due = split_totals(order, sum(i.unit_total * selected[i.id] for i in items if i.id in selected))["total"]

        if not error:
            try:
                received, card = services.check_payment(due, method, received_raw, card_raw, allowed)
                target = services.pay_order(
                    order, request.user, method, received, card, selected=None if pay_all else selected
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
            "quick_amounts": _quick_amounts(totals["total"]),
            "shortages": find_shortages(lines),
            "percent_discount": order.discount_value if order.discount_kind == Order.DiscountKind.PERCENT else 0,
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
        },
    )
