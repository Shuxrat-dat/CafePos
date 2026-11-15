"""Фоновая синхронизация с Juma: связь, каталог, заказы. Запускается командой `manage.py juma_sync` отдельным процессом.

Каждый шаг ловит свои ошибки и пишет их в JumaState.last_error: сбой облака или сети не должен ни останавливать
процесс, ни мешать кассе работать.
"""
import logging
import threading
import time

from django.db import close_old_connections
from django.utils import timezone

from . import client
from .models import JumaState

log = logging.getLogger(__name__)

HEARTBEAT_EVERY = 30  # секунд, как в MERCHANT_API.md
EVENTS_EVERY = 5
KITCHEN_EVERY = 10
INTAKE_EVERY = 5  # секунд: новый заказ доходит до кассы за несколько секунд
CATALOG_EVERY = 20  # секунд: каталог шлётся, только если изменился, поэтому стоп-лист доходит до Juma за секунды
TICK = 5


def heartbeat_once():
    """Сообщает Juma, что касса на связи (и принимает ли заказы). True при успехе, без настроенной Juma ничего не делает."""
    if not client.enabled():
        return False
    from pos.services import get_current_shift

    state = JumaState.get()
    code, body = client.heartbeat(accepting_orders=state.accepting_orders, shift_open=bool(get_current_shift()))
    if client.ok(code):
        state.last_heartbeat_at = timezone.now()
        state.delivery_enabled = bool(body.get("delivery_enabled"))
        state.last_error = ""
        state.save(update_fields=["last_heartbeat_at", "delivery_enabled", "last_error"])
        return True
    state.last_error = client.reason(code, body)[:255]
    state.save(update_fields=["last_error"])
    return False


def _guarded(step, name):
    """Выполняет шаг, не пропуская исключения: процесс живёт, ошибка попадает в журнал и на экран «Juma»."""
    try:
        return step()
    except Exception:
        log.exception("Juma: шаг «%s» не выполнен", name)
        try:
            JumaState.objects.filter(pk=1).update(last_error=f"Внутренняя ошибка: {name}")
        except Exception:
            log.exception("Juma: не удалось записать ошибку")
        return False
    finally:
        close_old_connections()


def catalog_once():
    from . import catalog

    return catalog.sync_catalog()


def intake_step():
    from . import intake

    return intake.intake_once()


def events_step():
    from . import sync

    return sync.events_once()


def kitchen_step():
    from . import sync

    return sync.kitchen_once()


def run(stop=None):
    """Основной цикл. stop — threading.Event для остановки (Ctrl+C в команде). Шаги выполняются по своему расписанию."""
    stop = stop or threading.Event()
    steps = [("связь", heartbeat_once, HEARTBEAT_EVERY), ("каталог", catalog_once, CATALOG_EVERY), ("заказы", intake_step, INTAKE_EVERY),
             ("события", events_step, EVENTS_EVERY), ("кухня", kitchen_step, KITCHEN_EVERY)]
    last = {name: 0.0 for name, _step, _every in steps}
    log.info("Juma: синхронизация запущена")
    while not stop.is_set():
        now = time.monotonic()
        for name, step, every in steps:
            if now - last[name] >= every:
                last[name] = now
                _guarded(step, name)
        stop.wait(TICK)
    log.info("Juma: синхронизация остановлена")
