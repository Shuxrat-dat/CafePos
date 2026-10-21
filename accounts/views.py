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
