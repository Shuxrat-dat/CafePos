"""Экран «Juma» на кассе: заказы доставки из облака и действия кассира (кнопки вызывают juma.actions)."""
from datetime import timedelta

from django.contrib import messages
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from accounts.permissions import PAYMENT_ROLES, role_required

from . import actions, client
from .models import JumaOrder, JumaState

ACTIVE = (JumaOrder.Status.PLACED, JumaOrder.Status.SENT, JumaOrder.Status.ACCEPTED, JumaOrder.Status.PREPARING, JumaOrder.Status.READY)
READY_MINUTES = (10, 15, 20, 30, 45)


def _context(request):
    state = JumaState.get()
    since = timezone.now() - timedelta(hours=12)
    links = JumaOrder.objects.select_related("order").prefetch_related("order__items__dish", "order__items__modifiers")
    active = list(links.filter(status__in=ACTIVE))
    refunds = list(links.filter(payment_status=JumaOrder.Payment.REFUND_PENDING))
    skip = [link.pk for link in active + refunds]
    recent = list(links.filter(created_at__gte=since).exclude(pk__in=skip)[:20])
    heartbeat_ok = bool(state.last_heartbeat_at and timezone.now() - state.last_heartbeat_at < timedelta(minutes=2))
    return {
        "state": state,
        "enabled": client.enabled(),
        "heartbeat_ok": heartbeat_ok,
        "active": active,
        "refunds": refunds,
        "recent": recent,
        "ready_minutes": READY_MINUTES,
        "cancel_reasons": [
            ("OUT_OF_STOCK", _("Нет в наличии")),
            ("ITEM_UNAVAILABLE", _("Блюдо недоступно")),
            ("PRICE_CHANGED", _("Изменилась цена")),
            ("CLOSED", _("Заведение закрыто")),
            ("OTHER", _("Другая причина")),
        ],
    }


@role_required(PAYMENT_ROLES)
def board(request):
    return render(request, "juma/board.html", _context(request))


@role_required(PAYMENT_ROLES)
def panel(request):
    return render(request, "juma/_panel.html", _context(request))


def _act(request, link_id, func, *args):
    link = get_object_or_404(JumaOrder, pk=link_id)
    ok, text = func(link, *args)
    if text:
        (messages.warning if ok else messages.error)(request, text)
    return render(request, "juma/_panel.html", _context(request))


@role_required(PAYMENT_ROLES)
@require_POST
def accept(request, link_id):
    minutes = request.POST.get("minutes", "")
    minutes = int(minutes) if minutes.isdigit() and 1 <= int(minutes) <= 240 else None
    return _act(request, link_id, actions.accept, minutes)


@role_required(PAYMENT_ROLES)
@require_POST
def paid(request, link_id):
    return _act(request, link_id, actions.mark_paid)


@role_required(PAYMENT_ROLES)
@require_POST
def ready(request, link_id):
    return _act(request, link_id, actions.mark_ready)


@role_required(PAYMENT_ROLES)
@require_POST
def handover(request, link_id):
    return _act(request, link_id, actions.hand_over, request.user, request.POST.get("code", ""))


@role_required(PAYMENT_ROLES)
@require_POST
def cancel(request, link_id):
    return _act(request, link_id, actions.cancel, request.user, request.POST.get("reason", ""))


@role_required(PAYMENT_ROLES)
@require_POST
def refund_done(request, link_id):
    return _act(request, link_id, actions.confirm_refund)


@role_required(PAYMENT_ROLES)
@require_POST
def pause(request):
    state = JumaState.get()
    state.accepting_orders = not state.accepting_orders
    state.save(update_fields=["accepting_orders"])
    return render(request, "juma/_panel.html", _context(request))
