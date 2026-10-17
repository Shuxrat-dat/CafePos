"""Журнал действий: запись событий и поиск подозрительного поведения сотрудников.

record() никогда не бросает исключение: сбой журнала не должен ломать работу кассы.
"""
import logging
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, Sum
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l

from .models import AuditLog

log = logging.getLogger(__name__)

A = AuditLog.Action


def _name(user):
    if user is None:
        return ""
    return (user.get_full_name() or user.get_username())[:150]


def record(user, action, *, order=None, amount=None, detail=""):
    """Добавляет запись. order может быть заказом или его номером."""
    try:
        order_id = getattr(order, "pk", order)
        AuditLog.objects.create(
            user=user if getattr(user, "pk", None) else None,
            user_name=_name(user),
            action=action,
            order_id=order_id,
            amount=None if amount is None else Decimal(amount).quantize(Decimal("1")),
            detail=str(detail)[:300],
        )
    except Exception:
        log.exception("Журнал действий: запись не сохранена")


# ---------- подозрительное поведение ----------

WINDOW_DAYS = 30
MIN_EVENTS = 3          # меньше стольких событий за период не считаем подозрительным
RATIO = 2               # во сколько раз больше среднего по команде

# (действие, название признака, подсказка владельцу)
SIGNALS = [
    (A.ORDER_CANCEL, _l("Много отмен заказов"), _l("Проверьте, не отменяют ли заказы после оплаты наличными.")),
    (A.ITEM_REMOVE, _l("Часто убирает блюда, уже отправленные на кухню"), _l("Сверьте с кухней: блюдо могли приготовить и выдать.")),
    (A.DISCOUNT, _l("Много скидок"), _l("Спросите, на каком основании даются скидки.")),
    (A.REFUND, _l("Много возвратов"), _l("Сверьте возвраты с чеками и причиной.")),
    (A.CASH_MOVE, _l("Частые изъятия или внесения наличных"), _l("Проверьте основания в журнале кассы.")),
]


def suspicious_report(days=WINDOW_DAYS, now=None):
    """Сотрудники, у которых отдельных событий заметно больше, чем у остальных.

    Возвращает список {user_id, user_name, flags:[{label, count, sum, team_avg, hint}]}
    по убыванию числа признаков. Пустой список — всё спокойно.
    """
    now = now or timezone.now()
    since = now - timedelta(days=days)
    rows = (
        AuditLog.objects.filter(created_at__gte=since, user__isnull=False)
        .values("user_id", "user_name", "action")
        .annotate(n=Count("id"), total=Sum("amount"))
    )
    per_user = defaultdict(dict)
    names = {}
    for row in rows:
        per_user[row["user_id"]][row["action"]] = (row["n"], row["total"] or Decimal(0))
        names[row["user_id"]] = row["user_name"]

    shift_diffs = (
        AuditLog.objects.filter(created_at__gte=since, action=A.SHIFT_CLOSE, user__isnull=False)
        .exclude(amount=0)
        .exclude(amount__isnull=True)
        .values("user_id", "user_name")
        .annotate(n=Count("id"), total=Sum("amount"))
    )

    team = list(per_user)
    report = {}
    for user_id in team:
        flags = []
        for action, label, hint in SIGNALS:
            count, total = per_user[user_id].get(action, (0, Decimal(0)))
            if count < MIN_EVENTS:
                continue
            others = [per_user[u].get(action, (0, 0))[0] for u in team if u != user_id]
            average = sum(others) / len(others) if others else 0
            # Если остальные почти не делают этого (скидки даёт в основном кассир), нужен заметный перебор
            if (average >= 1 and count >= RATIO * average) or (average < 1 and count >= MIN_EVENTS * 2):
                flags.append({"label": label, "count": count, "sum": total, "team_avg": round(average, 1), "hint": hint})
        report[user_id] = {"user_id": user_id, "user_name": names[user_id], "flags": flags}

    for row in shift_diffs:
        if row["n"] >= 2:
            entry = report.setdefault(
                row["user_id"], {"user_id": row["user_id"], "user_name": row["user_name"], "flags": []}
            )
            entry["flags"].append({
                "label": _("Касса не сходится при закрытии смены"), "count": row["n"], "sum": row["total"] or 0,
                "team_avg": 0, "hint": _("Расхождения повторяются: проверьте пересчёт наличных."),
            })
    result = [r for r in report.values() if r["flags"]]
    result.sort(key=lambda r: -len(r["flags"]))
    return result
