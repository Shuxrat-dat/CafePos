from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _

from core import audit

from . import cancel_pin
from .forms import CancelPinForm


@login_required
def cancel_pin_settings(request):
    """Кассир сам задаёт или меняет свой пароль отмены."""
    user = request.user
    if not user.needs_cancel_pin:
        raise Http404
    form = CancelPinForm(request.POST or None, user=user)
    if request.method == "POST":
        if form.is_valid():
            changed = user.has_cancel_pin
            cancel_pin.set_pin(user, form.cleaned_data["pin"])
            audit.record(user, audit.A.CANCEL_PIN_SET, detail=_("Пароль изменён") if changed else _("Пароль задан"))
            messages.success(request, _("Пароль отмены сохранён."))
            return redirect(request.GET.get("next") if (request.GET.get("next") or "").startswith("/") else "floor")
        messages.error(request, _("Проверьте поля формы."))
    return render(request, "accounts/cancel_pin.html", {"form": form, "has_pin": user.has_cancel_pin})


@login_required
def clock(request):
    """Табель: сотрудник сам отмечает начало и конец работы. Повторное нажатие закрывает открытую смену."""
    from django.utils import timezone
    from django.utils.http import url_has_allowed_host_and_scheme

    from .models import WorkShift

    if request.method != "POST":
        raise Http404
    shift = WorkShift.objects.filter(user=request.user, ended_at=None).first()
    if shift:
        shift.ended_at = timezone.now()
        shift.closed_by = request.user
        shift.save(update_fields=["ended_at", "closed_by"])
        hours = shift.hours_within(shift.started_at, shift.ended_at)
        messages.success(request, _("Смена закончена: %(h)s ч.") % {"h": f"{hours:.1f}".replace(".", ",")})
    else:
        WorkShift.objects.create(user=request.user)
        messages.success(request, _("Смена начата. Хорошей работы!"))
    back = request.POST.get("next", "")
    if not url_has_allowed_host_and_scheme(back, allowed_hosts={request.get_host()}):
        back = "/"
    return redirect(back)
