from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render, resolve_url
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from core import audit

from . import cancel_pin, earnings, terminal
from .forms import CancelPinForm, LoginForm
from .models import Terminal, User
from .permissions import MANAGE_ROLES, PIN_LOGIN_ROLES, role_required


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


# ---------- терминал: плитки сотрудников и вход по PIN ----------

class LoginView(auth_views.LoginView):
    """Обычный вход. На устройстве-терминале сразу показываем плитки, пароль — по ссылке «Войти паролем»."""

    authentication_form = LoginForm

    def get_default_redirect_url(self):
        user = self.request.user
        return resolve_url(user.home_url_name if user.is_authenticated else settings.LOGIN_REDIRECT_URL)

    def get(self, request, *args, **kwargs):
        if terminal.current(request) and not request.user.is_authenticated and not request.GET.get("password"):
            return redirect("terminal_home")
        return super().get(request, *args, **kwargs)


def _tile_users():
    return User.objects.filter(is_active=True, role__in=PIN_LOGIN_ROLES).exclude(login_pin_hash="").order_by(
        "first_name", "username"
    )


@never_cache  # иначе браузер по «Назад» покажет старую копию с открытым окном PIN и заблокированными кнопками
def terminal_home(request, error="", selected=None, status=200):
    device = terminal.current(request)
    if device is None:
        return render(request, "accounts/terminal_off.html", status=403 if request.method == "POST" else 200)
    if request.method == "GET" and request.user.is_authenticated and terminal.is_terminal_session(request):
        return redirect("table_map")  # официант ещё внутри (вернулся «Назад»): выйти можно только замком
    return render(request, "accounts/terminal.html", {
        "device": device, "staff": _tile_users(), "error": error, "selected": selected,
    }, status=status)


@never_cache
@require_POST
def terminal_login(request):
    device = terminal.current(request)
    if device is None:
        raise PermissionDenied
    user = User.objects.filter(pk=request.POST.get("user_id") or 0, is_active=True).first()
    if user is None or not user.can_use_terminal:
        return terminal_home(request, error=_("Выберите себя в списке."), status=400)
    try:
        terminal.check_pin(user, request.POST.get("pin"))
    except terminal.PinError as exc:
        return terminal_home(request, error=str(exc), selected=user, status=400)
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    request.session[terminal.SESSION_KEY] = device.id
    audit.record(user, audit.A.TERMINAL_LOGIN, detail=device.name)
    return redirect("table_map")


@require_POST
def terminal_lock(request):
    """Кнопка «замок» и автоблокировка: сотрудник выходит, терминал снова показывает плитки."""
    logout(request)
    return redirect("terminal_home" if terminal.current(request) else "login")


@role_required(MANAGE_ROLES)
def manage_terminals(request):
    device = terminal.current(request)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "register":
            name = (request.POST.get("name") or "").strip() or _("Терминал")
            response = redirect("manage_terminals")
            created = terminal.register(response, name, request.user)
            audit.record(request.user, audit.A.TERMINAL, detail=_("Добавлен терминал «%(n)s»") % {"n": created.name})
            messages.success(request, _("Это устройство теперь терминал «%(n)s». Выйдите — появятся плитки сотрудников.") % {"n": created.name})
            return response
        if action == "disable":
            item = get_object_or_404(Terminal, pk=request.POST.get("id"))
            item.is_active = False
            item.save(update_fields=["is_active"])
            audit.record(request.user, audit.A.TERMINAL, detail=_("Отключён терминал «%(n)s»") % {"n": item.name})
            messages.success(request, _("Терминал «%(n)s» отключён.") % {"n": item.name})
            return redirect("manage_terminals")
        raise Http404
    staff = User.objects.filter(is_active=True, role__in=PIN_LOGIN_ROLES).order_by("first_name", "username")
    return render(request, "manage/terminals.html", {
        "terminals": Terminal.objects.filter(is_active=True), "device": device, "staff": staff,
    })


# ---------- заработок официанта ----------

@login_required
def my_earnings(request):
    user = request.user
    starts = earnings.periods()
    period = request.GET.get("p") if request.GET.get("p") in starts else "today"
    orders = list(earnings.paid_orders(user, min(starts.values())))
    summary = {key: earnings.summarize(o for o in orders if o.closed_at >= start) for key, start in starts.items()}
    shown = [o for o in orders if o.closed_at >= starts[period]]
    from . import payouts
    from .models import SalaryPayment

    return render(request, "accounts/earnings.html", {
        "summary": summary, "period": period, "orders": shown, "percent": user.sales_percent,
        "balance": payouts.balance(user),
        "payments": SalaryPayment.objects.filter(user=user)[:10],
    })
