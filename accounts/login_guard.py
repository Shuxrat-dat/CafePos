"""Защита входа от подбора пароля.

После нескольких неверных попыток подряд вход для этой пары «логин + адрес» закрывается на время.
Работает и для страницы входа кассы, и для /admin/. Счётчики лежат в кэше Django:
сервер один процесс, после перезапуска счётчики обнуляются, это допустимо.
"""
import math
import time

from django.conf import settings
from django.core.cache import cache

MAX_FAILURES = 5
WINDOW_SECONDS = 15 * 60
LOCK_SECONDS = 15 * 60
# С одного адреса на все логины вместе, чтобы нельзя было перебирать логины
MAX_FAILURES_PER_IP = 30


def client_ip(request):
    """Адрес гостя. За обратным прокси (Caddy) берём его из X-Forwarded-For, иначе подменить его может любой."""
    if request is None:
        return ""
    if settings.BEHIND_PROXY:
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


def _keys(request, username):
    ip = client_ip(request)
    name = (username or "").strip().lower()[:150]
    return f"login-fail:{name}:{ip}", f"login-lock:{name}:{ip}", f"login-fail-ip:{ip}", f"login-lock-ip:{ip}"


def locked_minutes(request, username):
    """Сколько минут ещё закрыт вход, или 0."""
    _fail, lock, _fail_ip, lock_ip = _keys(request, username)
    until = max(cache.get(lock) or 0, cache.get(lock_ip) or 0)
    left = until - time.time()
    return max(1, math.ceil(left / 60)) if left > 0 else 0


def register_failure(request, username):
    fail, lock, fail_ip, lock_ip = _keys(request, username)
    for counter, lock_key, limit in ((fail, lock, MAX_FAILURES), (fail_ip, lock_ip, MAX_FAILURES_PER_IP)):
        cache.add(counter, 0, WINDOW_SECONDS)
        try:
            count = cache.incr(counter)
        except ValueError:
            cache.set(counter, 1, WINDOW_SECONDS)
            count = 1
        if count >= limit:
            cache.set(lock_key, time.time() + LOCK_SECONDS, LOCK_SECONDS)
            cache.delete(counter)


def reset(request, username):
    fail, lock, _fail_ip, _lock_ip = _keys(request, username)
    cache.delete_many([fail, lock])
