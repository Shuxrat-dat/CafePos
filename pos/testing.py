"""Помощники для тестов: оплата теперь принимается только в открытой кассовой смене."""
from django.contrib.auth import get_user_model

from .models import CashShift


def open_shift_for_tests(cashier=None):
    """Открывает смену напрямую, без журнала. Кассир по умолчанию — первый сотрудник в базе."""
    cashier = cashier or get_user_model().objects.order_by("pk").first()
    return CashShift.objects.create(cashier=cashier, initial_cash=0)
