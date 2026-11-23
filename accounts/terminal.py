"""Терминал в зале (моноблок): плитки официантов и вход по короткому PIN.

Как это защищено:
* Вход по PIN принимается только с устройства, которое владелец или администратор сделал терминалом
  (долгоживущий cookie с ключом, в базе только его хеш). С телефона или из интернета PIN не работает —
  там нужен обычный логин и пароль, поэтому подобрать PIN снаружи нельзя.
* После 5 неверных PIN подряд вход этого сотрудника по PIN закрыт на 5 минут, событие идёт в журнал.
* PIN есть только у официантов (PIN_LOGIN_ROLES). Владелец и администратор входят паролем.
"""
import hashlib
import logging
import math
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.utils import timezone
from django.utils.translation import gettext as _

from .cancel_pin import WEAK

log = logging.getLogger(__name__)

COOKIE = "cafepos_terminal"
COOKIE_AGE = 60 * 60 * 24 * 365 * 5  # устройство остаётся терминалом, пока его не отключат
SESSION_KEY = "terminal_id"
IDLE_SECONDS = 180  # экран сам возвращается к плиткам через 3 минуты без касаний
MAX_FAILURES = 5
LOCK_MINUTES = 5
PIN_LENGTHS = (4, 5, 6)


class PinError(Exception):
    """Текст готов для показа сотруднику."""


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def current(request):
    """Терминал, с которого пришёл запрос, или None. Результат кэшируется на запросе."""
    if hasattr(request, "_terminal"):
        return request._terminal
    from .models import Terminal

    token = request.COOKIES.get(COOKIE, "")
    terminal = None
    if token and len(token) < 200:
        terminal = Terminal.objects.filter(token_hash=_hash(token), is_active=True).first()
        if terminal and (not terminal.last_seen_at or timezone.now() - terminal.last_seen_at > timedelta(minutes=5)):
            Terminal.objects.filter(pk=terminal.pk).update(last_seen_at=timezone.now())
    request._terminal = terminal
    return terminal


def register(response, name, user):
    """Делает устройство терминалом: создаёт запись и кладёт ключ в cookie ответа."""
    from .models import Terminal

    token = secrets.token_urlsafe(32)
    terminal = Terminal.objects.create(name=name[:60], token_hash=_hash(token), created_by=user)
    response.set_cookie(COOKIE, token, max_age=COOKIE_AGE, httponly=True, samesite="Lax",
                        secure=settings.SESSION_COOKIE_SECURE)
    return terminal


def is_terminal_session(request):
    return bool(request.session.get(SESSION_KEY)) if hasattr(request, "session") else False


# ---------- PIN ----------

def weak_reason(pin):
    if not pin.isdigit() or len(pin) not in PIN_LENGTHS:
        return _("PIN — от 4 до 6 цифр.")
    if len(set(pin)) == 1 or pin in WEAK or pin in "0123456789" or pin in "9876543210":
        return _("PIN слишком простой. Придумайте другой.")
    return ""


def set_pin(user, raw):
    user.login_pin_hash = make_password(raw.strip())
    user.login_pin_failures = 0
    user.login_pin_locked_until = None
    user.save(update_fields=["login_pin_hash", "login_pin_failures", "login_pin_locked_until"])


def clear_pin(user):
    user.login_pin_hash = ""
    user.login_pin_failures = 0
    user.login_pin_locked_until = None
    user.save(update_fields=["login_pin_hash", "login_pin_failures", "login_pin_locked_until"])


def check_pin(user, raw):
    """Проверяет PIN. При ошибке бросает PinError с понятным текстом и считает неудачу."""
    from .models import User

    if not user.is_active or not user.can_use_terminal or not user.has_login_pin:
        raise PinError(_("Для этого сотрудника вход по PIN не настроен. Обратитесь к администратору."))
    now = timezone.now()
    if user.login_pin_locked_until and user.login_pin_locked_until > now:
        minutes = max(1, math.ceil((user.login_pin_locked_until - now).total_seconds() / 60))
        raise PinError(_("Слишком много неверных попыток. Попробуйте через %(m)s мин.") % {"m": minutes})
    pin = (raw or "").strip()
    if pin and check_password(pin, user.login_pin_hash):
        if user.login_pin_failures or user.login_pin_locked_until:
            User.objects.filter(pk=user.pk).update(login_pin_failures=0, login_pin_locked_until=None)
        return
    failures = user.login_pin_failures + 1
    if failures >= MAX_FAILURES:
        User.objects.filter(pk=user.pk).update(
            login_pin_failures=0, login_pin_locked_until=now + timedelta(minutes=LOCK_MINUTES)
        )
        try:
            from core import audit

            audit.record(user, audit.A.TERMINAL_PIN_LOCKED,
                         detail=_("Заблокирован на %(m)s мин. после неверных PIN") % {"m": LOCK_MINUTES})
        except Exception:
            log.exception("Терминал: блокировка PIN не записана")
        raise PinError(_("Слишком много неверных попыток. Вход по PIN закрыт на %(m)s мин.") % {"m": LOCK_MINUTES})
    User.objects.filter(pk=user.pk).update(login_pin_failures=failures)
    raise PinError(_("Неверный PIN. Осталось попыток: %(n)s.") % {"n": MAX_FAILURES - failures})
