from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Prefetch
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from accounts.permissions import REPORT_ROLES
from menu.models import Category, Dish
from menu.public_views import _qr_svg

from .models import GuestOrder, GuestOrderItem, OrderItem, Table
from .views import _find_same, _get_order, _panel

MAX_LINES = 20
MAX_QTY = 20
MAX_PENDING_PER_TABLE = 3
COOLDOWN_SECONDS = 60


def table_menu(request, table_id):
    """Меню стола для гостя: без входа, заказ уходит официанту на подтверждение."""
    table = get_object_or_404(Table, id=table_id)
    categories = Category.objects.filter(is_active=True).prefetch_related(
        Prefetch("dishes", queryset=Dish.objects.filter(is_available=True, is_hidden=False), to_attr="available")
    )
    categories = [c for c in categories if c.available]
    dishes = {d.id: d for c in categories for d in c.available}
    picked = {}
    error = ""

    if request.method == "POST":
        for dish_id in dishes:
            raw = request.POST.get(f"qty_{dish_id}", "").strip()
            if raw.isascii() and raw.isdigit() and int(raw) > 0:
                picked[dish_id] = min(int(raw), MAX_QTY)
        now = timezone.now().timestamp()
        last = request.session.get("guest_last_order", 0)
        pending = GuestOrder.objects.filter(table=table, status=GuestOrder.Status.PENDING).count()
        if now - last < COOLDOWN_SECONDS:
            error = _("Подождите минуту перед следующим заказом.")
        elif not picked:
            error = _("Выберите хотя бы одно блюдо.")
        elif len(picked) > MAX_LINES:
            error = _("Слишком много разных блюд в одном заказе.")
        elif pending >= MAX_PENDING_PER_TABLE:
            error = _("Заказ этого стола уже ожидает официанта. Подождите, пожалуйста.")
        else:
            with transaction.atomic():
                guest = GuestOrder.objects.create(table=table)
                GuestOrderItem.objects.bulk_create(
                    [GuestOrderItem(guest_order=guest, dish=dishes[i], quantity=q) for i, q in picked.items()]
                )
            request.session["guest_last_order"] = now
            return redirect(f"{reverse('table_menu', args=[table.id])}?sent=1")

    for category in categories:
        for dish in category.available:
            dish.picked = picked.get(dish.id, 0)
    context = {
        "table": table,
        "categories": categories,
        "error": error,
        "sent": request.GET.get("sent") == "1",
    }
    return render(request, "menu/table_menu.html", context)


@login_required
def tables_qr(request):
    """Страница с QR-кодами всех столов для печати (владелец и администратор)."""
    user = request.user
    if not (user.is_superuser or user.role in REPORT_ROLES):
        raise PermissionDenied
    host = request.get_host().split(":")[0]
    items = []
    for table in Table.objects.select_related("hall").order_by("hall__sort_order", "hall_id", "number"):
        url = request.build_absolute_uri(reverse("table_menu", args=[table.id]))
        items.append({"table": table, "url": url, "qr": _qr_svg(url)})
    context = {"items": items, "is_local": host in ("localhost", "127.0.0.1")}
    return render(request, "menu/tables_qr.html", context)

@login_required
@require_POST
def accept_guest(request, order_id, guest_id):
    order = _get_order(request, order_id)
    guest = get_object_or_404(GuestOrder, id=guest_id, table=order.table, status=GuestOrder.Status.PENDING)
    with transaction.atomic():
        # Статус меняем первым и по условию: повторное нажатие ничего не добавит
        changed = GuestOrder.objects.filter(id=guest.id, status=GuestOrder.Status.PENDING).update(
            status=GuestOrder.Status.ACCEPTED
        )
        if changed:
            for guest_item in guest.items.select_related("dish"):
                if not guest_item.dish.can_order:
                    continue
                if guest_item.dish.sold_by_weight:
                    # Вес гость не знает: каждая порция отдельной позицией, вес укажет официант
                    for _n in range(guest_item.quantity):
                        OrderItem.objects.create(order=order, dish=guest_item.dish, quantity=1)
                    continue
                item = _find_same(order, guest_item.dish, [], "")
                if item:
                    item.quantity += guest_item.quantity
                    item.save()
                else:
                    OrderItem.objects.create(order=order, dish=guest_item.dish, quantity=guest_item.quantity)
    return _panel(request, order)


@login_required
@require_POST
def reject_guest(request, order_id, guest_id):
    order = _get_order(request, order_id)
    get_object_or_404(GuestOrder, id=guest_id, table=order.table, status=GuestOrder.Status.PENDING)
    GuestOrder.objects.filter(id=guest_id, status=GuestOrder.Status.PENDING).update(
        status=GuestOrder.Status.REJECTED
    )
    return _panel(request, order)

def review(request, token):
    """Страница оценки по QR на чеке: без входа, один отзыв на заказ."""
    from core import telegram

    from . import reviews
    from .models import Order, Review

    order_id = reviews.read_token(token)
    order = Order.objects.filter(id=order_id, status=Order.Status.PAID).first() if order_id else None
    if order is None:
        return render(request, "menu/review.html", {"state": "invalid"}, status=404)
    existing = Review.objects.filter(order=order).first()
    if existing:
        return render(request, "menu/review.html", {"state": "done", "review": existing})

    error = ""
    if request.method == "POST":
        raw = request.POST.get("rating", "")
        comment = request.POST.get("comment", "").strip()[:500]
        if not (raw.isascii() and raw.isdigit() and 1 <= int(raw) <= 5):
            error = _("Выберите оценку от 1 до 5.")
        else:
            created, _new = Review.objects.get_or_create(
                order=order, defaults={"rating": int(raw), "comment": comment}
            )
            if _new:
                telegram.notify_review(created)
            return redirect(reverse("order_review", args=[token]))
    return render(request, "menu/review.html", {"state": "form", "error": error, "token": token})
