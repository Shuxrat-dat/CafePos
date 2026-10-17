"""Баллы гостей по номеру телефона: начисление после оплаты и отмена при возврате.

1 балл = 1 сум. Баллы пока копятся и показываются на чеке, а списывать их кассир может как обычную скидку.
Ни одна функция не должна ломать оплату: ошибки гасятся, платёж важнее баллов.
"""
import logging
import re
from decimal import Decimal

from django.db import transaction
from django.utils.translation import gettext as _

from core.models import CafeSettings

from .models import Customer, LoyaltyEntry

log = logging.getLogger(__name__)


def normalize_phone(raw):
    """Узнаём гостя по последним 9 цифрам: +998 90 123-45-67, 901234567 и 8 90... дают один ключ."""
    digits = re.sub(r"\D", "", raw or "")
    return digits[-9:] if len(digits) >= 9 else ""


def find_customer(raw_phone):
    key = normalize_phone(raw_phone)
    return Customer.objects.filter(phone=key).first() if key else None


def earn(order):
    """Начисляет баллы за оплаченный заказ один раз. Возвращает запись или None."""
    try:
        percent = CafeSettings.get().loyalty_percent
        key = normalize_phone(order.customer_phone)
        if not percent or not key:
            return None
        total = Decimal(order.total)
        points = int(total * percent / 100)
        if points <= 0:
            return None
        with transaction.atomic():
            if LoyaltyEntry.objects.filter(order=order, points__gt=0).exists():
                return None
            customer, _created = Customer.objects.select_for_update().get_or_create(
                phone=key, defaults={"name": (order.customer_name or "")[:100]}
            )
            if order.customer_name and not customer.name:
                customer.name = order.customer_name[:100]
            customer.points += points
            customer.visits += 1
            customer.spent += total
            customer.save()
            return LoyaltyEntry.objects.create(
                customer=customer, order=order, points=points,
                comment=_("Начислено за заказ №%(id)s") % {"id": order.id},
            )
    except Exception:
        log.exception("Баллы: не удалось начислить за заказ %s", getattr(order, "pk", "?"))
        return None


def reverse(order):
    """При возврате забираем начисленные за заказ баллы."""
    try:
        entries = list(LoyaltyEntry.objects.filter(order=order))
        points = sum(e.points for e in entries)
        if points <= 0:
            return None
        with transaction.atomic():
            customer = Customer.objects.select_for_update().get(pk=entries[0].customer_id)
            customer.points -= points
            customer.visits = max(0, customer.visits - 1)
            customer.spent = max(Decimal(0), customer.spent - Decimal(order.total))
            customer.save()
            return LoyaltyEntry.objects.create(
                customer=customer, order=order, points=-points,
                comment=_("Возврат по заказу №%(id)s") % {"id": order.id},
            )
    except Exception:
        log.exception("Баллы: не удалось отменить начисление за заказ %s", getattr(order, "pk", "?"))
        return None
