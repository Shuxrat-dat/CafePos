from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Prefetch
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from accounts import login_guard
from accounts.permissions import REPORT_ROLES
from menu.models import Category, Dish
from menu.public_views import _qr_svg

from core.models import CafeSettings

from . import services
from .models import GuestOrder, GuestOrderItem, Order, OrderItem, Table
from .views import _find_same, _get_order, _panel

MAX_TRACKED_ITEMS = 60
MAX_LINES = 20
MAX_QTY = 20
MAX_PENDING_PER_TABLE = 3
COOLDOWN_SECONDS = 60
MAX_ORDERS_PER_IP_HOUR = 40


def _ip_allows_order(request):
    """Ограничение по адресу: сессию гость может сбросить, адрес нет.
    Предел мягкий: гости в одном Wi-Fi кафе выходят в интернет с одного адреса."""
    ip = login_guard.client_ip(request)
    return (cache.get(f"guest-hour:{ip}") or 0) < MAX_ORDERS_PER_IP_HOUR


def _remember_ip_order(request):
    ip = login_guard.client_ip(request)
    cache.add(f"guest-hour:{ip}", 0, 3600)
    try:
        cache.incr(f"guest-hour:{ip}")
    except ValueError:
        cache.set(f"guest-hour:{ip}", 1, 3600)


def _item_state(item):
    """Что видит гость про своё блюдо: принято, готовится, готово, подано."""
    S = OrderItem.Status
    if item.status == S.COOKING:
        return "cooking", _("Готовится")
    if item.status == S.READY:
        return "ready", _("Готово")
    if item.status == S.SERVED:
        return "served", _("Подано")
    return "accepted", _("Принято")


def _my_items(request, table):
    """Блюда, которые гость заказал с этого телефона и которые ещё не оплачены."""
    ids = request.session.get("guest_items") or []
    if not ids:
        return []
    items = list(
        OrderItem.objects.filter(id__in=ids, order__table=table, order__status=Order.Status.OPEN)
        .select_related("dish__category")
        .order_by("id")
    )
    for item in items:
        item.guest_state, item.guest_label = _item_state(item)
    return items


def table_menu(request, table_id):
    """Меню стола для гостя: без входа. Заказ сразу уходит в работу и на кухню
    (или официанту на подтверждение, если так включено в настройках).
    Заказать можно только по ссылке из QR-кода (с ключом стола), без ключа меню только для просмотра."""
    table = get_object_or_404(Table, id=table_id)
    can_order = constant_time_compare(request.GET.get("k", ""), table.menu_key)
    auto = CafeSettings.get().guest_auto_send
    if request.GET.get("status") == "1":
        # Страница опрашивает этот адрес, чтобы показывать, как готовится заказ
        if not can_order:
            raise Http404
        return render(request, "menu/_guest_status.html", {"my_items": _my_items(request, table)})
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
        if not can_order:
            error = _("Этот QR-код устарел. Попросите официанта принять заказ.")
        elif now - last < COOLDOWN_SECONDS:
            error = _("Подождите минуту перед следующим заказом.")
        elif not _ip_allows_order(request):
            error = _("Слишком много заказов. Попросите официанта.")
        elif not picked:
            error = _("Выберите хотя бы одно блюдо.")
        elif len(picked) > MAX_LINES:
            error = _("Слишком много разных блюд в одном заказе.")
        elif pending >= MAX_PENDING_PER_TABLE:
            error = _("Заказ этого стола уже ожидает официанта. Подождите, пожалуйста.")
        else:
            lines = [(dishes[i], q) for i, q in picked.items()]
            with transaction.atomic():
                placed = services.place_guest_order(table, lines) if auto else None
                guest = GuestOrder.objects.create(
                    table=table, status=GuestOrder.Status.ACCEPTED if placed else GuestOrder.Status.PENDING
                )
                GuestOrderItem.objects.bulk_create(
                    [GuestOrderItem(guest_order=guest, dish=dish, quantity=q) for dish, q in lines]
                )
            if placed:
                tracked = (request.session.get("guest_items") or []) + [item.id for item in placed[1]]
                request.session["guest_items"] = tracked[-MAX_TRACKED_ITEMS:]
            request.session["guest_last_order"] = now
            _remember_ip_order(request)
            return redirect(f"{table.menu_path()}&sent={2 if placed else 1}")

    for category in categories:
        for dish in category.available:
            dish.picked = picked.get(dish.id, 0)
    context = {
        "table": table,
        "can_order": can_order,
        "categories": categories,
        "error": error,
        "auto": auto,
        "sent": request.GET.get("sent") in ("1", "2"),
        "sent_to_kitchen": request.GET.get("sent") == "2",
        "my_items": _my_items(request, table) if can_order else [],
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
        url = request.build_absolute_uri(table.menu_path())
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
