"""Пароль отмены кассира.

Кассир задаёт его сам (владелец может только сбросить). Без него кассир не может отменить заказ,
убрать блюдо или оформить возврат. После нескольких неверных попыток подряд пароль на время
блокируется, об этом узнаёт владелец. Хранится только хеш, как у обычного пароля.
"""
import logging
import math
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password, make_password
from django.db.models import F
from django.utils import timezone
from django.utils.translation import gettext as _

log = logging.getLogger(__name__)

MAX_FAILURES = 5
LOCK_MINUTES = 10
MIN_LENGTH = 4
MAX_LENGTH = 32
WEAK = {"1234", "12345", "123456", "4321", "54321", "654321", "0000", "1111", "2222", "3333", "4444", "5555",
        "6666", "7777", "8888", "9999", "1212", "2580", "1122", "0123", "9876"}


class PinError(Exception):
    """Ошибка проверки пароля отмены. Текст готов для показа сотруднику."""


def normalize(raw):
    return (raw or "").strip()


def weak_reason(pin, user):
    """Почему пароль отмены не подходит, или пустая строка."""
    if len(pin) < MIN_LENGTH:
        return _("Пароль отмены слишком короткий: нужно не меньше %(n)s символов.") % {"n": MIN_LENGTH}
    if len(pin) > MAX_LENGTH:
        return _("Пароль отмены слишком длинный: не больше %(n)s символов.") % {"n": MAX_LENGTH}
    if len(set(pin)) == 1 or pin in WEAK:
        return _("Пароль отмены слишком простой. Придумайте другой.")
    if pin.lower() == (user.get_username() or "").lower():
        return _("Пароль отмены не должен совпадать с логином.")
    if user.has_usable_password() and user.check_password(pin):
        return _("Пароль отмены должен отличаться от пароля для входа.")
    return ""


def set_pin(user, raw):
    """Сохраняет новый пароль отмены. Проверка надёжности выполняется в форме (weak_reason)."""
    user.cancel_pin_hash = make_password(normalize(raw))
    user.save(update_fields=["cancel_pin_hash"])


def reset_pin(user):
    """Сброс владельцем: пароль удаляется, блокировка снимается. Новый кассир задаёт сам."""
    user.cancel_pin_hash = ""
    user.cancel_pin_failures = 0
    user.cancel_pin_locked_until = None
    user.save(update_fields=["cancel_pin_hash", "cancel_pin_failures", "cancel_pin_locked_until"])


def check(user, raw):
    """Проверяет пароль отмены. Для сотрудников, которым он не нужен, ничего не делает.
    При ошибке бросает PinError с понятным текстом."""
    if not user.needs_cancel_pin:
        return
    if not user.has_cancel_pin:
        raise PinError(_("Сначала задайте свой пароль отмены: пункт «Пароль отмены» в меню слева."))
    now = timezone.now()
    if user.cancel_pin_locked_until and user.cancel_pin_locked_until > now:
        minutes = max(1, math.ceil((user.cancel_pin_locked_until - now).total_seconds() / 60))
        raise PinError(_("Слишком много неверных попыток. Попробуйте через %(m)s мин. или попросите владельца сбросить пароль.") % {"m": minutes})
    pin = normalize(raw)
    if not pin:
        raise PinError(_("Введите пароль отмены."))
    if check_password(pin, user.cancel_pin_hash):
        if user.cancel_pin_failures or user.cancel_pin_locked_until:
            user.cancel_pin_failures = 0
            user.cancel_pin_locked_until = None
            user.save(update_fields=["cancel_pin_failures", "cancel_pin_locked_until"])
        return
    _register_failure(user)


def _register_failure(user):
    # request.user приходит в обёртке (SimpleLazyObject), поэтому модель берём явно
    get_user_model().objects.filter(pk=user.pk).update(cancel_pin_failures=F("cancel_pin_failures") + 1)
    user.refresh_from_db(fields=["cancel_pin_failures", "cancel_pin_locked_until"])
    if user.cancel_pin_failures >= MAX_FAILURES:
        user.cancel_pin_failures = 0
        user.cancel_pin_locked_until = timezone.now() + timedelta(minutes=LOCK_MINUTES)
        user.save(update_fields=["cancel_pin_failures", "cancel_pin_locked_until"])
        _report_lock(user)
        raise PinError(_("Слишком много неверных попыток. Пароль отмены заблокирован на %(m)s мин.") % {"m": LOCK_MINUTES})
    left = MAX_FAILURES - user.cancel_pin_failures
    raise PinError(_("Неверный пароль отмены. Осталось попыток: %(n)s.") % {"n": left})


def _report_lock(user):
    """Блокировка попадает в журнал и владельцу в Telegram. Сбой уведомления кассу не ломает."""
    try:
        from core import audit, telegram

        audit.record(user, audit.A.CANCEL_PIN_LOCKED, detail=_("Заблокирован на %(m)s мин. после неверных попыток") % {"m": LOCK_MINUTES})
        telegram.notify_pin_locked(user)
    except Exception:
        log.exception("Пароль отмены: блокировка не записана")
