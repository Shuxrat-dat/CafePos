"""Клиент центрального Telegram-бота: быстрое подключение, события и сводки.

Касса сама токен бота не знает. Она получает от центра свой ключ, присылает события и сводки по HTTPS,
а владелец подключается одним нажатием на ссылку. Ошибки сети наружу не выбрасываются: касса работает как обычно.
"""
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings
from django.db import connection
from django.utils.translation import gettext as _

from .models import CafeSettings

log = logging.getLogger(__name__)

TIMEOUT = 8
SNAPSHOT_EVERY = 60          # не чаще раза в минуту, как бы часто ни шли оплаты
_state = {"last": 0.0, "pending": False}
_lock = threading.Lock()


def enabled():
    """Центр настроен в .env (HUB_URL и общий секрет)."""
    return bool(settings.HUB_URL and settings.HUB_REGISTRATION_TOKEN)


def connected(cafe=None):
    cafe = cafe or CafeSettings.get()
    return bool(settings.HUB_URL and cafe.hub_key)


def _call(method, path, payload=None, key="", raw=None, headers=None, timeout=TIMEOUT):
    """(код ответа, словарь). Код 0 — центр недоступен."""
    data = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else None)
    request = urllib.request.Request(settings.HUB_URL.rstrip("/") + path, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if key:
        request.add_header("Authorization", f"Bearer {key}")
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode())
        except (ValueError, OSError):
            return exc.code, {}
    except (urllib.error.URLError, OSError, ValueError):
        return 0, {}


def reason(code):
    if code == 0:
        return _("Нет связи с центром уведомлений. Проверьте интернет и попробуйте ещё раз.")
    if code == 403:
        return _("Центр не принял секрет подключения. Проверьте HUB_REGISTRATION_TOKEN в файле .env.")
    return _("Центр уведомлений вернул ошибку %(code)s.") % {"code": code}


def connect(cafe):
    """Регистрирует заведение в центре и сохраняет ключ. Возвращает (успех, ссылка или причина)."""
    code, body = _call("POST", "/v1/venues", {
        "name": cafe.name, "lang": cafe.default_language, "registration_token": settings.HUB_REGISTRATION_TOKEN})
    if code != 200 or not body.get("venue_key"):
        return False, reason(code)
    cafe.hub_key = body["venue_key"]
    cafe.save(update_fields=["hub_key"])
    push_soon(delay=1)
    return True, body.get("link_url", "")


def status(cafe, want_link=False):
    """Состояние подключения или None, если центр недоступен. {'lost': True}, если центр не знает этот ключ."""
    code, body = _call("GET", "/v1/status" + ("?link=1" if want_link else ""), key=cafe.hub_key)
    if code == 401:
        return {"lost": True}
    return body if code == 200 else None


def new_link(cafe):
    code, body = _call("POST", "/v1/link", {}, key=cafe.hub_key)
    return (True, body.get("link_url", "")) if code == 200 else (False, reason(code))


def remove_recipient(cafe, chat_id):
    _call("POST", "/v1/disconnect", {"chat_id": str(chat_id)}, key=cafe.hub_key)


def disconnect(cafe):
    """Убирает всех получателей и забывает ключ."""
    if cafe.hub_key:
        _call("POST", "/v1/disconnect", {}, key=cafe.hub_key)
    cafe.hub_key = ""
    cafe.save(update_fields=["hub_key"])


def send_test(cafe):
    code, body = _call("POST", "/v1/test", {}, key=cafe.hub_key)
    if code != 200:
        return False, reason(code)
    return (True, "") if body.get("sent") else (False, _("Пока некому отправлять: подключите хотя бы одного получателя."))


def send_event(cafe, event, text):
    code, body = _call("POST", "/v1/events", {"event": event, "text": text}, key=cafe.hub_key)
    if code != 200:
        log.warning("Центр уведомлений: событие %s не доставлено (%s)", event, code)
    return code == 200


def send_document(cafe, path, caption=""):
    with open(path, "rb") as fh:
        content = fh.read()
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    code, body = _call(
        "POST", "/v1/document", raw=content, key=cafe.hub_key, timeout=120,
        headers={"X-Filename": urllib.parse.quote(name), "X-Caption": urllib.parse.quote(caption)})
    if code != 200:
        return False, reason(code)
    return (True, "") if body.get("sent") else (False, _("Никто из получателей не включил копии базы."))


def push_snapshot():
    """Собирает сводку и отправляет в центр. Возвращает True при успехе."""
    from .hub_snapshot import build_snapshot

    cafe = CafeSettings.get()
    if not connected(cafe):
        return False
    code, _body = _call("POST", "/v1/snapshot", {"data": build_snapshot()}, key=cafe.hub_key, timeout=20)
    return code == 200


def _job():
    try:
        push_snapshot()
    except Exception:
        log.exception("Центр уведомлений: сводка не отправлена")
    finally:
        with _lock:
            _state["pending"] = False
            _state["last"] = time.time()
        connection.close()  # поток держит своё соединение с базой


def push_soon(delay=3):
    """Ставит отправку сводки в фон, не чаще раза в минуту. Вызывается после оплат и смен."""
    try:
        if not settings.HUB_URL or not connected():
            return
        with _lock:
            if _state["pending"]:
                return
            _state["pending"] = True
            wait = max(delay, SNAPSHOT_EVERY - (time.time() - _state["last"]))
        timer = threading.Timer(wait, _job)
        timer.daemon = True
        timer.start()
    except Exception:
        log.exception("Центр уведомлений: не удалось запланировать сводку")


def connect_signals():
    """Сводка уходит в центр после оплаты, отмены и смены: цифры в боте обновляются почти сразу."""
    from django.db.models.signals import post_save

    from pos.models import CashShift, Order

    def on_order(sender, instance, **kwargs):
        if instance.status in (Order.Status.PAID, Order.Status.CANCELLED):
            push_soon()

    def on_shift(sender, instance, **kwargs):
        push_soon()

    # weak=False: иначе локальные функции сразу исчезнут из памяти и сигнал перестанет работать
    post_save.connect(on_order, sender=Order, dispatch_uid="hub_order_saved", weak=False)
    post_save.connect(on_shift, sender=CashShift, dispatch_uid="hub_shift_saved", weak=False)
