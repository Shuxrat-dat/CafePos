"""Раздел «Брони» для сотрудников: новые заявки, ближайшие брони, подтверждение со столом."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _

from menu.public_views import _qr_svg

from . import booking as booking_service
from .models import Booking


@login_required
def bookings(request):
    booking_service.sync_today()
    if request.method == "POST":
        item = get_object_or_404(Booking, id=request.POST.get("booking_id") or 0)
        action = request.POST.get("action")
        try:
            if action == "confirm":
                booking_service.confirm(item, request.POST.get("table"), request.user)
                messages.success(request, _("Бронь подтверждена."))
            elif action == "reject":
                booking_service.close(item, Booking.Status.REJECTED, request.user)
                messages.success(request, _("Заявка отклонена."))
            elif action == "cancel":
                booking_service.close(item, Booking.Status.CANCELLED, request.user)
                messages.success(request, _("Бронь отменена."))
        except booking_service.BookingError as exc:
            messages.error(request, str(exc))
        return redirect(f"{reverse('bookings')}?tab={request.POST.get('tab', 'new')}")

    tab = request.GET.get("tab", "new")
    today = timezone.localdate()
    qs = Booking.objects.select_related("table__hall")
    counts = {
        "new": qs.filter(status=Booking.Status.NEW).count(),
        "upcoming": qs.filter(status=Booking.Status.CONFIRMED, date__gte=today).count(),
    }
    if tab == "upcoming":
        rows = list(qs.filter(status=Booking.Status.CONFIRMED, date__gte=today))
    elif tab == "history":
        rows = list(qs.exclude(status__in=[Booking.Status.NEW, Booking.Status.CONFIRMED]).order_by("-date", "-time")[:100])
    else:
        tab = "new"
        rows = list(qs.filter(status=Booking.Status.NEW))
        for item in rows:
            item.free_tables = booking_service.available_tables(item.date, item.time, item.guests)
    url = request.build_absolute_uri(reverse("book"))
    return render(request, "pos/bookings.html", {
        "rows": rows, "tab": tab, "counts": counts, "today": today, "book_url": url,
        "qr": _qr_svg(url) if request.user.manages_cafe else None,
    })
