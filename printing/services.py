"""Очередь печати: какие задания создать и как отдать их программе печати.

Печать не должна мешать кассе: если что-то пошло не так, ошибка пишется в журнал,
а заказ и оплата проходят как обычно.
"""
import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from . import documents
from .models import CHECK_INTERVAL_SECONDS, PrintAgent, Printer, PrintJob

log = logging.getLogger(__name__)

MAX_COPIES = 5
CLAIM_LIMIT = 10
# Сколько ждём ответа программы печати о задании, которое она забрала
RESULT_TIMEOUT = timedelta(minutes=2)


def _active():
    return Printer.objects.filter(is_active=True)


def _queue(printer, kind, title, data, order=None):
    copies = max(1, min(printer.copies, MAX_COPIES))
    return [
        PrintJob.objects.create(printer=printer, kind=kind, title=title[:100], data=data, order=order)
        for _n in range(copies)
    ]


def receipt_printers():
    return _active().filter(prints_receipts=True)


def queue_receipt(order, printer=None, precheck=False, auto=False):
    """Чек или пречек. auto=True — после оплаты, только на принтеры с автопечатью."""
    if printer is not None:
        printers = [printer]
    else:
        printers = receipt_printers()
        if auto:
            printers = printers.filter(auto_receipt=True)
    kind = PrintJob.Kind.PRECHECK if precheck else PrintJob.Kind.RECEIPT
    title = (_("Пречек №%(n)s") if precheck else _("Чек №%(n)s")) % {"n": order.id}
    jobs = []
    for p in printers:
        jobs += _queue(p, kind, title, documents.receipt(order, p, precheck=precheck), order)
    return jobs


def queue_kitchen(order, item_ids):
    """Новые позиции заказа расходятся по кухонным принтерам согласно категориям блюд."""
    items = list(order.items.filter(id__in=item_ids).select_related("dish").prefetch_related("modifiers").order_by("id"))
    if not items:
        return []
    addition = order.items.exclude(id__in=item_ids).exclude(sent_at=None).exists()
    jobs = []
    for printer in _active().filter(prints_kitchen=True).prefetch_related("categories"):
        mine = [item for item in items if printer.takes_dish(item.dish)]
        if mine:
            title = _("Кухня №%(n)s") % {"n": order.id}
            jobs += _queue(printer, PrintJob.Kind.KITCHEN, title, documents.kitchen(order, mine, printer, addition), order)
    return jobs


def queue_test(printer):
    return _queue(printer, PrintJob.Kind.TEST, _("Тестовая печать"), documents.test_page(printer))[0]


def retry(job):
    """Повторить задание с ошибкой: создаётся новое с теми же данными."""
    return PrintJob.objects.create(printer=job.printer, kind=job.kind, title=job.title, data=bytes(job.data), order=job.order)


def safe(func, *args, **kwargs):
    """Вызов, который никогда не роняет кассу."""
    try:
        return func(*args, **kwargs)
    except Exception:
        log.exception("Печать: не удалось поставить задание")
        return None


def after_commit(func, *args, **kwargs):
    """Поставить задание после сохранения заказа в базе: печатаем только то, что точно записано."""
    transaction.on_commit(lambda: safe(func, *args, **kwargs))


# ---------- программа печати ----------

def _expire_stale():
    """Задание забрали, но результат так и не пришёл: показываем это как ошибку, а не ждём вечно."""
    PrintJob.objects.filter(status=PrintJob.Status.SENT, claimed_at__lt=timezone.now() - RESULT_TIMEOUT).update(
        status=PrintJob.Status.ERROR, finished_at=timezone.now(), error_code="no_result",
    )


def claim_jobs(agent, info=""):
    """Отдаёт программе печати очередные задания и отмечает связь с ней."""
    now = timezone.now()
    agent.last_seen_at = now
    if info:
        agent.info = info[:150]
    agent.save(update_fields=["last_seen_at", "info"])
    _expire_stale()
    ids = list(
        PrintJob.objects.filter(status=PrintJob.Status.PENDING, printer__is_active=True)
        .order_by("id").values_list("id", flat=True)[:CLAIM_LIMIT]
    )
    claimed = PrintJob.objects.filter(id__in=ids, status=PrintJob.Status.PENDING).update(
        status=PrintJob.Status.SENT, claimed_at=now
    )
    if not claimed:
        return []
    return list(PrintJob.objects.filter(id__in=ids, status=PrintJob.Status.SENT).select_related("printer").order_by("id"))


def printers_to_check(agent):
    """Принтеры, связь с которыми программа печати должна проверить сейчас: раз в полминуты
    или сразу после кнопки «Проверить связь». Пустой список — проверять не нужно."""
    now = timezone.now()
    requested = agent.check_requested_at and (not agent.check_sent_at or agent.check_requested_at > agent.check_sent_at)
    due = not agent.check_sent_at or now - agent.check_sent_at >= timedelta(seconds=CHECK_INTERVAL_SECONDS)
    if not (requested or due):
        return []
    printers = list(Printer.objects.filter(is_active=True))
    agent.check_sent_at = now
    agent.save(update_fields=["check_sent_at"])
    return printers


def record_check(results):
    """Результаты проверки связи от программы печати: [{id, ok, code, detail}]."""
    now = timezone.now()
    for result in results:
        if not str(result.get("id", "")).isdigit():
            continue
        ok = bool(result.get("ok"))
        Printer.objects.filter(id=int(result["id"])).update(
            online=ok, checked_at=now,
            check_code="" if ok else str(result.get("code", "") or "unknown")[:30],
            check_detail="" if ok else str(result.get("detail", ""))[:200],
        )


def request_check():
    agent = PrintAgent.get()
    agent.check_requested_at = timezone.now()
    agent.save(update_fields=["check_requested_at"])


def problem_count():
    """Сколько включённых принтеров сейчас без связи (для пометки в меню). Если программа печати
    не на связи, проблемными считаются все: печатать сейчас не получится ни на один."""
    printers = list(Printer.objects.filter(is_active=True))
    if not printers:
        return 0
    if not PrintAgent.get().is_online:
        return len(printers)
    return sum(1 for p in printers if p.connection_state == "error")


def finish_job(job, ok, code="", detail=""):
    if job.status != PrintJob.Status.SENT:
        return False
    job.status = PrintJob.Status.DONE if ok else PrintJob.Status.ERROR
    job.error_code = "" if ok else (code or "unknown")[:30]
    job.error_detail = "" if ok else detail[:200]
    job.finished_at = timezone.now()
    job.save(update_fields=["status", "error_code", "error_detail", "finished_at"])
    return True
