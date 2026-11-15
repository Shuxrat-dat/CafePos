"""Онлайн-бронь: проверка заявки гостя, подбор свободных столов, подтверждение и показ брони на схеме столов.

Правила простые: стол занят бронью на 2 часа от её времени, подтверждённые брони одного стола не пересекаются,
заявка в статусе «новая» стол не занимает, пока сотрудник её не подтвердил."""
import re
from datetime import datetime, timedelta

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from core.models import CafeSettings

from .loyalty import normalize_phone
from .models import Booking, Order, Table

SLOT_MINUTES = 30
STAY_MINUTES = 120        # столько стол считается занятым бронью
HOLD_BEFORE_MINUTES = 60  # за сколько до времени бронь появляется на схеме столов
MIN_LEAD_MINUTES = 30     # заявку на сегодня принимаем не позже чем за 30 минут


class BookingError(Exception):
    pass


def slots(cafe):
    """Доступное время начала брони от открытия до «закрытие минус 1 час» с шагом в 30 минут."""
    day = datetime(2000, 1, 1)
    start = datetime.combine(day, cafe.booking_open)
    last = datetime.combine(day, cafe.booking_close) - timedelta(minutes=60)
    out, cur = [], start
    while cur <= last:
        out.append(cur.strftime("%H:%M"))
        cur += timedelta(minutes=SLOT_MINUTES)
    return out


def _overlaps(a, b):
    return abs((a - b).total_seconds()) < STAY_MINUTES * 60


def available_tables(day, at, guests, exclude=None):
    """Столы с достаточным числом мест, свободные от подтверждённых броней на это время (лучше подходящие первыми)."""
    start = timezone.make_aware(datetime.combine(day, at))
    taken = {
        b.table_id
        for b in Booking.objects.filter(status=Booking.Status.CONFIRMED, date=day, table__isnull=False)
        if (exclude is None or b.pk != exclude.pk) and _overlaps(b.starts_at, start)
    }
    return [t for t in Table.objects.select_related("hall").order_by("seats", "hall__id", "number")
            if t.seats >= guests and t.id not in taken]


def create_request(cafe, data, now=None):
    """Проверяет заявку гостя и создаёт её. Бросает BookingError с понятным текстом."""
    if not cafe.booking_enabled:
        raise BookingError(_("Онлайн-бронь сейчас выключена."))
    now = now or timezone.localtime()
    name = (data.get("name") or "").strip()[:100]
    if len(name) < 2:
        raise BookingError(_("Укажите ваше имя."))
    phone = normalize_phone(data.get("phone"))
    if not phone:
        raise BookingError(_("Укажите номер телефона, чтобы мы могли подтвердить бронь."))
    try:
        guests = int(data.get("guests") or 0)
    except ValueError:
        guests = 0
    if not 1 <= guests <= cafe.booking_max_guests:
        raise BookingError(_("Число гостей: от 1 до %(n)s. Для большей компании позвоните нам.") % {"n": cafe.booking_max_guests})
    try:
        day = datetime.strptime((data.get("date") or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        raise BookingError(_("Выберите дату."))
    if day < now.date() or day > now.date() + timedelta(days=cafe.booking_days_ahead):
        raise BookingError(_("Бронь возможна на ближайшие %(n)s дней.") % {"n": cafe.booking_days_ahead})
    raw_time = (data.get("time") or "").strip()
    if raw_time not in slots(cafe):
        raise BookingError(_("Выберите время из списка."))
    at = datetime.strptime(raw_time, "%H:%M").time()
    if timezone.make_aware(datetime.combine(day, at)) < now + timedelta(minutes=MIN_LEAD_MINUTES):
        raise BookingError(_("Это время уже слишком близко. Выберите позже или позвоните нам."))
    comment = re.sub(r"\s+", " ", (data.get("comment") or "")).strip()[:200]
    if not Table.objects.filter(seats__gte=guests).exists():
        raise BookingError(_("Для такой компании у нас нет подходящего стола. Позвоните нам."))
    if not available_tables(day, at, guests):
        raise BookingError(_("На это время свободных столов нет. Выберите другое время."))
    if Booking.objects.filter(phone=phone, date=day, time=at,
                              status__in=[Booking.Status.NEW, Booking.Status.CONFIRMED]).exists():
        raise BookingError(_("Такая заявка уже принята. Мы свяжемся с вами."))
    return Booking.objects.create(name=name, phone=phone, guests=guests, date=day, time=at, comment=comment)


def confirm(booking, table_id, user):
    """Сотрудник подтверждает заявку и назначает стол."""
    with transaction.atomic():
        locked = Booking.objects.select_for_update().get(pk=booking.pk)
        if locked.status != Booking.Status.NEW:
            raise BookingError(_("Заявка уже обработана."))
        table = next((t for t in available_tables(locked.date, locked.time, locked.guests, exclude=locked)
                      if str(t.id) == str(table_id)), None)
        if table is None:
            raise BookingError(_("Этот стол на это время занят или мал. Выберите другой."))
        locked.status = Booking.Status.CONFIRMED
        locked.table = table
        locked.handled_by = user
        locked.handled_at = timezone.now()
        locked.save(update_fields=["status", "table", "handled_by", "handled_at"])
    sync_today()
    return locked


def close(booking, status, user):
    """Отклонить новую заявку или отменить подтверждённую."""
    if status not in (Booking.Status.REJECTED, Booking.Status.CANCELLED):
        raise BookingError(_("Неверное действие."))
    with transaction.atomic():
        locked = Booking.objects.select_for_update().get(pk=booking.pk)
        if locked.status not in (Booking.Status.NEW, Booking.Status.CONFIRMED):
            raise BookingError(_("Заявка уже обработана."))
        was_shown = locked.applied and locked.table_id
        locked.status = status
        locked.handled_by = user
        locked.handled_at = timezone.now()
        locked.save(update_fields=["status", "handled_by", "handled_at"])
        if was_shown:
            table = locked.table
            # Снимаем со схемы только свою бронь: ручную бронь другого гостя не трогаем
            if table.reserved_for.startswith(f"{locked.name}, "):
                table.reserved_for, table.reserved_at = "", None
                table.save(update_fields=["reserved_for", "reserved_at"])
    return locked


def sync_today(now=None):
    """Подтверждённые брони на сегодня переносятся на схему столов (за час до времени), прошедшие помечаются завершёнными."""
    now = now or timezone.localtime()
    Booking.objects.filter(status=Booking.Status.CONFIRMED, date__lt=now.date()).update(status=Booking.Status.DONE)
    pending = Booking.objects.filter(
        status=Booking.Status.CONFIRMED, date=now.date(), applied=False, table__isnull=False
    ).select_related("table")
    for booking in pending:
        start = booking.starts_at
        if now < start - timedelta(minutes=HOLD_BEFORE_MINUTES):
            continue
        table = booking.table
        if table.is_reserved:
            continue  # на столе уже чья-то бронь: ждём, пока её снимут
        if Order.objects.filter(table=table, status=Order.Status.OPEN, items__isnull=False).exists():
            continue  # за столом ещё сидят
        table.reserved_for = f"{booking.name}, {booking.guests}"
        table.reserved_at = start
        table.save(update_fields=["reserved_for", "reserved_at"])
        booking.applied = True
        booking.save(update_fields=["applied"])
