"""Онлайн-бронь для гостя: без входа, с телефона. Заявку потом подтверждает сотрудник."""
from datetime import timedelta

from django.core.cache import cache
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _

from accounts import login_guard
from core import telegram
from core.models import CafeSettings
from pos import booking as booking_service

MAX_REQUESTS_PER_IP_HOUR = 5


def _info(request, title, text, icon, status=200):
    return render(request, "menu/book_info.html", {"title": title, "text": text, "icon": icon}, status=status)


def _hits(request):
    return cache.get(f"book-hour:{login_guard.client_ip(request)}") or 0


def _remember(request):
    key = f"book-hour:{login_guard.client_ip(request)}"
    cache.add(key, 0, 3600)
    try:
        cache.incr(key)
    except ValueError:
        cache.set(key, 1, 3600)


def book(request):
    cafe = CafeSettings.get()
    if not cafe.booking_enabled:
        return _info(request, _("Онлайн-бронь недоступна"), _("Позвоните нам, и мы забронируем стол."), "phone", status=404)
    today = timezone.localdate()
    error = ""
    form = {"guests": "2", "date": today.isoformat()}
    if request.method == "POST":
        form = request.POST
        if request.POST.get("website"):  # поле-ловушка для ботов: человек его не видит
            return redirect("book_done")
        if _hits(request) >= MAX_REQUESTS_PER_IP_HOUR:
            error = _("Слишком много заявок с этого устройства. Попробуйте позже или позвоните нам.")
        else:
            try:
                created = booking_service.create_request(cafe, request.POST)
            except booking_service.BookingError as exc:
                error = str(exc)
            else:
                _remember(request)
                telegram.notify_booking(created)
                request.session["booking_done"] = {
                    "name": created.name, "date": created.date.strftime("%d.%m.%Y"),
                    "time": created.time.strftime("%H:%M"), "guests": created.guests,
                }
                return redirect("book_done")
    return render(request, "menu/book.html", {
        "cafe_phone": cafe.phone, "slots": booking_service.slots(cafe), "form": form, "error": error,
        "min_date": today.isoformat(), "max_date": (today + timedelta(days=cafe.booking_days_ahead)).isoformat(),
        "max_guests": cafe.booking_max_guests,
    })


def book_done(request):
    summary = request.session.pop("booking_done", None)
    return render(request, "menu/book_done.html", {"summary": summary, "cafe_phone": CafeSettings.get().phone})
