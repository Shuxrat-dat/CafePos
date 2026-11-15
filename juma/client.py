"""HTTP-клиент Merchant API облака Juma (docs/MERCHANT_API.md проекта Juma).

Касса сама ходит в облако (она за NAT): ключ заведения берётся из .env (JUMA_URL, JUMA_MERCHANT_KEY). Ошибки сети наружу
не выбрасываются: каждая функция возвращает (код ответа, словарь), код 0 — облако недоступно. Касса работает как обычно.
"""
import json
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings
from django.utils.translation import gettext as _

API = "/api/merchant/v1"
TIMEOUT = 10
POS_VERSION = "1.0"


def enabled():
    """Облако настроено в .env (адрес и ключ заведения)."""
    return bool(settings.JUMA_URL and settings.JUMA_MERCHANT_KEY)


def _call(method, path, payload=None, timeout=TIMEOUT):
    """(код ответа, словарь). Код 0 — облако недоступно."""
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(settings.JUMA_URL.rstrip("/") + API + path, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    request.add_header("Authorization", f"Bearer {settings.JUMA_MERCHANT_KEY}")
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


def ok(code):
    return 200 <= code < 300


def reason(code, body=None):
    """Понятная причина неудачи для экрана «Juma»."""
    if code == 0:
        return _("Нет связи с Juma. Проверьте интернет.")
    if code == 401:
        return _("Juma не приняла ключ заведения. Проверьте JUMA_MERCHANT_KEY в файле .env.")
    if code == 403:
        return _("Заведение приостановлено в Juma.")
    detail = ((body or {}).get("error") or {}).get("message", "")
    return _("Juma вернула ошибку %(code)s.") % {"code": code} + (f" {detail}" if detail else "")


def heartbeat(accepting_orders=True, shift_open=None):
    payload = {"pos": "CAFEPOS", "pos_version": POS_VERSION, "accepting_orders": bool(accepting_orders)}
    if shift_open is not None:
        payload["shift_open"] = bool(shift_open)
    return _call("POST", "/heartbeat", payload)


def catalog_state():
    return _call("GET", "/catalog")


def put_catalog(payload):
    return _call("PUT", "/catalog", payload, timeout=60)


def patch_availability(payload):
    return _call("PATCH", "/catalog/availability", payload)


def pending_orders(wait=0):
    return _call("GET", f"/orders/pending?wait={int(wait)}", timeout=TIMEOUT + int(wait))


def get_order(order_id):
    return _call("GET", f"/orders/{order_id}")


def ack_order(order_id):
    return _call("POST", f"/orders/{order_id}/ack", {})


def reject_order(order_id, code, items=None, comment=""):
    payload = {"code": code}
    if items:
        payload["items"] = list(items)
    if comment:
        payload["comment"] = comment[:255]
    return _call("POST", f"/orders/{order_id}/reject", payload)


def set_status(order_id, payload):
    return _call("POST", f"/orders/{order_id}/status", payload)


def set_payment(order_id, payload):
    return _call("POST", f"/orders/{order_id}/payment", payload)


def events(after=0, wait=0):
    query = urllib.parse.urlencode({"after": int(after), "wait": int(wait)})
    return _call("GET", f"/events?{query}", timeout=TIMEOUT + int(wait))
