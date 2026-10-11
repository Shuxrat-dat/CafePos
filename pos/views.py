from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from inventory.services import find_shortages, write_off_order
from menu.models import Category, Dish

from .models import GuestOrder, Hall, Order, OrderItem, Table


# ---------- вспомогательные функции ----------

def _get_order(request, order_id, open_only=True):
    """Находит заказ и проверяет, что пользователь вправе с ним работать."""
    order = get_object_or_404(Order, id=order_id)
    user = request.user
    if not user.sees_all_orders and order.waiter_id != user.id:
        raise PermissionDenied
    if open_only and order.status != Order.Status.OPEN:
        raise Http404
    return order


def _can_cancel(user, order):
    return order.status == Order.Status.OPEN and (not order.has_sent_items or user.can_cancel_sent_orders)


def _panel_context(request, order):
    return {"order": order, "can_cancel": _can_cancel(request.user, order)}


def _panel(request, order):
    return render(request, "pos/_order_panel.html", _panel_context(request, order))


def _clean_modifiers(request, dish):
    """Берём только те модификаторы, которые действительно привязаны к блюду."""
    ids = [i for i in request.POST.getlist("modifiers") if i.isdigit()]
    return list(dish.modifiers.filter(id__in=ids))


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


# ---------- зал и заказы ----------

@login_required
def floor(request):
    if request.user.role == "cook" and not request.user.is_superuser:
        return redirect("kitchen_board")
    halls = Hall.objects.prefetch_related("tables")
    open_orders = {
        o.table_id: o for o in Order.objects.filter(status=Order.Status.OPEN).select_related("waiter")
    }
    pending_tables = set(
        GuestOrder.objects.filter(status=GuestOrder.Status.PENDING).values_list("table_id", flat=True)
    )
    for hall in halls:
        for table in hall.tables.all():
            order = open_orders.get(table.id)
            table.open_order = order
            table.guest_pending = table.id in pending_tables
            table.locked = bool(order) and not request.user.sees_all_orders and order.waiter_id != request.user.id
    return render(request, "pos/floor.html", {"halls": halls})


@login_required
@require_POST
def open_table(request, table_id):
    table = get_object_or_404(Table, id=table_id)
    order = Order.objects.filter(table=table, status=Order.Status.OPEN).first()
    if order is None:
        order = Order.objects.create(table=table, waiter=request.user)
    elif not request.user.sees_all_orders and order.waiter_id != request.user.id:
        raise PermissionDenied
    return redirect("order_detail", order_id=order.id)


@login_required
def order_detail(request, order_id):
    order = _get_order(request, order_id, open_only=False)
    if order.status != Order.Status.OPEN:
        return redirect("floor")
    categories = Category.objects.filter(is_active=True).prefetch_related("dishes__modifiers")
    return render(request, "pos/order.html", {"categories": categories, **_panel_context(request, order)})


@login_required
def order_panel(request, order_id):
    order = _get_order(request, order_id, open_only=False)
    if order.status != Order.Status.OPEN:
        # Заказ закрыт или отменён: уводим страницу на план зала
        return HttpResponse(headers={"HX-Redirect": reverse("floor")})
    return _panel(request, order)


@login_required
def item_form(request, order_id, dish_id):
    order = _get_order(request, order_id)
    dish = get_object_or_404(Dish, id=dish_id, is_available=True)
    return render(
        request,
        "pos/_item_form.html",
        {"order": order, "dish": dish, "item": None, "selected": set(), "quantity": 1, "comment": ""},
    )


@login_required
@require_POST
def add_item(request, order_id, dish_id):
    order = _get_order(request, order_id)
    dish = get_object_or_404(Dish, id=dish_id, is_available=True)
    modifiers = _clean_modifiers(request, dish)
    comment = _clean_comment(request)
    qty = _clean_qty(request)

    item = _find_same(order, dish, [m.id for m in modifiers], comment)
    if item:
        item.quantity += qty
        item.save()
    else:
        item = OrderItem.objects.create(order=order, dish=dish, quantity=qty, comment=comment)
        item.modifiers.set(modifiers)
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
        return _panel(request, order)

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
        item.save()
    return _panel(request, order)


@login_required
@require_POST
def send_to_kitchen(request, order_id):
    order = _get_order(request, order_id)
    order.items.filter(status=OrderItem.Status.NEW).update(status=OrderItem.Status.COOKING)
    return _panel(request, order)


@login_required
@require_POST
def serve_item(request, order_id, item_id):
    order = _get_order(request, order_id)
    item = get_object_or_404(OrderItem, id=item_id, order=order, status=OrderItem.Status.READY)
    item.status = OrderItem.Status.SERVED
    item.save()
    return _panel(request, order)


# ---------- отмена и перенос ----------

@login_required
@require_POST
def cancel_order(request, order_id):
    order = _get_order(request, order_id)
    if not _can_cancel(request.user, order):
        return redirect("order_detail", order_id=order.id)
    order.status = Order.Status.CANCELLED
    order.closed_at = timezone.now()
    order.save()
    return redirect("floor")


@login_required
def move_form(request, order_id):
    order = _get_order(request, order_id)
    busy = set(Order.objects.filter(status=Order.Status.OPEN).values_list("table_id", flat=True))
    halls = []
    for hall in Hall.objects.prefetch_related("tables"):
        free = [t for t in hall.tables.all() if t.id not in busy]
        if free:
            halls.append({"name": hall.name, "tables": free})
    return render(request, "pos/_move_form.html", {"order": order, "halls": halls})


@login_required
@require_POST
def move_order(request, order_id, table_id):
    order = _get_order(request, order_id)
    table = get_object_or_404(Table, id=table_id)
    if not Order.objects.filter(table=table, status=Order.Status.OPEN).exists():
        order.table = table
        order.save(update_fields=["table"])
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


def _split_off(order, items, selected, method, received):
    """Переносит выбранные позиции в новый оплаченный заказ, остальное остаётся в открытом."""
    target = Order.objects.create(
        table=order.table,
        waiter=order.waiter,
        status=Order.Status.PAID,
        payment_method=method,
        cash_received=received,
        closed_at=timezone.now(),
    )
    for item in items:
        qty = selected.get(item.id)
        if not qty:
            continue
        if qty == item.quantity:
            item.order = target
            item.save()
        else:
            part = OrderItem.objects.create(
                order=target, dish=item.dish, quantity=qty, price=item.price,
                comment=item.comment, status=item.status,
            )
            part.modifiers.set(item.modifiers.all())
            item.quantity -= qty
            item.save()
    return target


@login_required
def pay(request, order_id):
    if not request.user.can_take_payment:
        raise PermissionDenied
    order = _get_order(request, order_id)
    if not order.can_pay:
        return redirect("order_detail", order_id=order.id)

    items = list(order.items.select_related("dish").prefetch_related("modifiers"))
    split = request.GET.get("split") == "1"
    error = ""
    selected = {}
    method = "cash"
    received_raw = ""

    if request.method == "POST":
        split = request.POST.get("split") == "1"
        method = request.POST.get("method", "")
        received_raw = request.POST.get("received", "").strip()
        pay_all = True

        if split:
            selected = _parse_split(request, items)
            if selected is None:
                selected, error = {}, "Проверьте количество у позиций."
            elif not selected:
                error = "Выберите хотя бы одну позицию."
            else:
                pay_all = all(selected.get(i.id) == i.quantity for i in items)

        due = order.total if pay_all else sum(i.unit_total * selected[i.id] for i in items if i.id in selected)

        received = None
        if not error:
            if method not in (Order.PaymentMethod.CASH, Order.PaymentMethod.CARD):
                error = "Выберите способ оплаты."
            elif method == Order.PaymentMethod.CASH:
                try:
                    received = int(received_raw) if received_raw else due
                except ValueError:
                    error = "Введите сумму числом."
                else:
                    if received < due:
                        error = "Полученная сумма меньше итога."

        if not error:
            with transaction.atomic():
                if pay_all:
                    target = order
                    target.payment_method = method
                    target.cash_received = received
                    target.status = Order.Status.PAID
                    target.closed_at = timezone.now()
                    target.save()
                else:
                    target = _split_off(order, items, selected, method, received)
                write_off_order(target)
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
            "split": split,
            "error": error,
            "method": method,
            "received": received_raw,
            "shortages": find_shortages(lines),
        },
    )
@login_required
def receipt(request, order_id):
    if not request.user.can_take_payment:
        raise PermissionDenied
    order = get_object_or_404(Order, id=order_id, status=Order.Status.PAID)
    change = None
    if order.payment_method == Order.PaymentMethod.CASH and order.cash_received is not None:
        change = order.cash_received - order.total
    remaining = Order.objects.filter(table=order.table, status=Order.Status.OPEN).first()
    return render(request, "pos/receipt.html", {"order": order, "change": change, "remaining": remaining})