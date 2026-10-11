from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Prefetch
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST

from pos.models import Order, OrderItem

KITCHEN_ROLES = {"cook", "admin", "owner"}


def kitchen_only(view):
    @login_required
    def wrapper(request, *args, **kwargs):
        if not (request.user.is_superuser or request.user.role in KITCHEN_ROLES):
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return wrapper


def _tickets(request):
    cooking = OrderItem.objects.filter(status=OrderItem.Status.COOKING).select_related("dish").prefetch_related("modifiers")
    orders = (
        Order.objects.filter(status=Order.Status.OPEN, items__status=OrderItem.Status.COOKING)
        .select_related("table__hall")
        .prefetch_related(Prefetch("items", queryset=cooking, to_attr="cooking_items"))
        .distinct()
        .order_by("id")
    )
    return render(request, "kitchen/_tickets.html", {"orders": orders})


@kitchen_only
def board(request):
    return render(request, "kitchen/board.html")


@kitchen_only
def tickets(request):
    return _tickets(request)


@kitchen_only
@require_POST
def mark_ready(request, item_id):
    item = get_object_or_404(OrderItem, id=item_id, status=OrderItem.Status.COOKING)
    item.status = OrderItem.Status.READY
    item.save()
    return _tickets(request)