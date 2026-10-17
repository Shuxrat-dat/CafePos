from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login, logout
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.db.models import Count, ProtectedError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from accounts.permissions import MANAGE_ROLES, role_required
from pos.models import Hall, Table

from .forms import CafeSettingsForm, HallForm, StaffForm, TableForm
from .models import CafeSettings

manager_only = role_required(MANAGE_ROLES)
User = get_user_model()

# Учётные записи демо-ресторана (создаются командой seed_demo только в демо-базе)
DEMO_USERS = ["owner", "admin", "kassir", "ofitsiant", "oshpaz"]


def _safe_next(request, fallback="/"):
    target = request.POST.get("next") or request.GET.get("next") or ""
    if url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}):
        return target
    return fallback


@require_POST
def set_language(request):
    language = request.POST.get("language", "")
    response = redirect(_safe_next(request))
    if language in dict(settings.LANGUAGES):
        response.set_cookie(
            settings.LANGUAGE_COOKIE_NAME, language, max_age=settings.LANGUAGE_COOKIE_AGE, samesite="Lax"
        )
    return response


# ---------- настройки заведения ----------

@manager_only
def manage_settings(request):
    cafe = CafeSettings.get()
    form = CafeSettingsForm(request.POST or None, request.FILES or None, instance=cafe)
    if request.method == "POST":
        if form.is_valid():
            form.save()
            messages.success(request, _("Настройки сохранены."))
            return redirect("manage_settings")
        messages.error(request, _("Проверьте поля формы."))
    return render(request, "manage/settings.html", {"form": form})


@manager_only
def manage_tables(request):
    hall_form = HallForm(prefix="hall")
    table_form = TableForm(prefix="table")
    if request.method == "POST":
        kind = request.POST.get("kind")
        if kind == "hall":
            hall_form = HallForm(request.POST, prefix="hall")
            if hall_form.is_valid():
                hall_form.save()
                messages.success(request, _("Зал добавлен."))
                return redirect("manage_tables")
        elif kind == "table":
            table_form = TableForm(request.POST, prefix="table")
            if table_form.is_valid():
                table_form.save()
                messages.success(request, _("Стол добавлен."))
                return redirect("manage_tables")
        messages.error(request, _("Проверьте поля формы."))
    if request.method == "GET" and request.GET.get("hall", "").isdigit():
        table_form = TableForm(prefix="table", initial={"hall": int(request.GET["hall"])})
    from pos.views import _busy_table_ids  # локальный импорт: pos.views сам зависит от core

    busy = _busy_table_ids()
    halls = list(Hall.objects.prefetch_related("tables").annotate(n=Count("tables")))
    summary = {"free": 0, "busy": 0, "reserved": 0, "seats": 0}
    for hall in halls:
        hall.stats = {"free": 0, "busy": 0, "reserved": 0, "seats": 0}
        for table in hall.tables.all():
            table.state = "busy" if table.id in busy else "reserved" if table.is_reserved else "free"
            top = (table.seats + 1) // 2
            table.chairs_top = range(top)
            table.chairs_bottom = range(table.seats - top)
            hall.stats[table.state] += 1
            hall.stats["seats"] += table.seats
        for key, value in hall.stats.items():
            summary[key] += value
    return render(request, "manage/tables.html", {
        "halls": halls, "hall_form": hall_form, "table_form": table_form, "summary": summary,
    })


@manager_only
def table_edit(request, table_id):
    table = get_object_or_404(Table, id=table_id)
    form = TableForm(request.POST or None, instance=table)
    if request.method == "POST":
        if form.is_valid():
            form.save()
            messages.success(request, _("Стол сохранён."))
            return redirect("manage_tables")
        messages.error(request, _("Проверьте поля формы."))
    return render(request, "manage/table_form.html", {"form": form, "table": table})


@manager_only
@require_POST
def table_delete(request, table_id):
    table = get_object_or_404(Table, id=table_id)
    try:
        table.delete()
    except ProtectedError:
        messages.error(request, _("У стола есть история заказов, удалить его нельзя."))
    else:
        messages.success(request, _("Стол удалён."))
    return redirect("manage_tables")


@manager_only
@require_POST
def hall_delete(request, hall_id):
    hall = get_object_or_404(Hall, id=hall_id)
    if hall.tables.exists():
        messages.error(request, _("Сначала удалите или перенесите столы этого зала."))
    else:
        hall.delete()
        messages.success(request, _("Зал удалён."))
    return redirect("manage_tables")


# ---------- персонал ----------

def _can_edit_user(editor, target):
    if editor.is_superuser:
        return True
    return not (target.is_superuser or (target.role == User.Role.OWNER and editor.role != User.Role.OWNER))


@manager_only
def manage_staff(request):
    staff = User.objects.order_by("-is_active", "role", "first_name", "username")
    rows = [{"user": u, "editable": _can_edit_user(request.user, u)} for u in staff]
    return render(request, "manage/staff.html", {"rows": rows})


@manager_only
def staff_form(request, user_id=None):
    target = get_object_or_404(User, id=user_id) if user_id else None
    if target is not None and not _can_edit_user(request.user, target):
        raise PermissionDenied
    form = StaffForm(request.POST or None, instance=target, editor=request.user)
    if request.method == "POST":
        if form.is_valid():
            user = form.save()
            messages.success(
                request, _("Сотрудник «%(name)s» сохранён.") % {"name": user.get_full_name() or user.username}
            )
            return redirect("manage_staff")
        messages.error(request, _("Проверьте поля формы."))
    return render(request, "manage/staff_form.html", {"form": form, "target": target})


# ---------- демо-режим ----------

@require_POST
def demo_login(request):
    """Вход в демо одним нажатием. Работает только в демо-режиме и только для демо-сотрудников."""
    if not settings.DEMO_MODE:
        raise Http404
    username = request.POST.get("username", "")
    if username not in DEMO_USERS:
        raise Http404
    user = User.objects.filter(username=username, is_active=True).first()
    if user is None:
        messages.error(request, _("Демо-данные не загружены. Запустите seed_demo."))
        return redirect("login")
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    return redirect("kitchen_board" if user.role == User.Role.COOK else "floor")


@manager_only
@require_POST
def demo_reset(request):
    """Возврат демо-ресторана к исходному состоянию. В рабочем режиме недоступен."""
    if not settings.DEMO_MODE:
        raise Http404
    call_command("seed_demo", reset=True, verbosity=0)
    logout(request)
    messages.success(request, _("Демо-данные восстановлены. Войдите снова."))
    return redirect("login")
