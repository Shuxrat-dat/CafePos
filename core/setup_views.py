"""Быстрая настройка нового заведения и резервная копия: всё, что нужно владельцу в первый день."""
import os
import sqlite3
import tempfile
from datetime import datetime

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _

from accounts.permissions import MANAGE_ROLES, role_required
from menu import importer
from menu.models import Category, Dish
from pos.models import Hall, Table

from . import audit
from .forms import CafeBasicsForm
from .menu_templates import TEMPLATES, rows_for
from .models import CafeSettings

manager_only = role_required(MANAGE_ROLES)
User = get_user_model()

DEFAULT_NAME = "CafePOS"
MAX_TABLES = 60


def _status(cafe):
    tables = Table.objects.count()
    dishes = Dish.objects.count()
    staff = User.objects.filter(is_active=True, is_superuser=False).exclude(role="owner").count()
    return {
        "cafe_done": cafe.name.strip() not in ("", DEFAULT_NAME),
        "halls": Hall.objects.count(), "tables": tables, "tables_done": tables > 0,
        "dishes": dishes, "categories": Category.objects.count(), "menu_done": dishes > 0,
        "staff": staff, "staff_done": staff > 0,
    }


def _import_report(request, report, source):
    messages.success(request, _("Меню (%(src)s): добавлено %(c)s, обновлено %(u)s, пропущено %(s)s.") % {
        "src": source, "c": report["created"], "u": report["updated"], "s": report["skipped"]})
    request.session["import_errors"] = [[line, str(text)] for line, text in report["errors"]]
    audit.record(request.user, audit.A.MENU_IMPORT, detail=_("%(src)s: +%(c)s, обновлено %(u)s") % {
        "src": source, "c": report["created"], "u": report["updated"]})


@manager_only
def setup_wizard(request):
    cafe = CafeSettings.get()
    form = CafeBasicsForm(instance=cafe)
    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "cafe":
            form = CafeBasicsForm(request.POST, instance=cafe)
            if form.is_valid():
                form.save()
                messages.success(request, _("Данные заведения сохранены."))
                return redirect("setup_wizard")
            messages.error(request, _("Проверьте название заведения."))
        elif action == "tables":
            name = request.POST.get("hall_name", "").strip()[:100] or _("Основной зал")
            count, seats = request.POST.get("count", ""), request.POST.get("seats", "")
            if not (count.isdigit() and 1 <= int(count) <= MAX_TABLES and seats.isdigit() and 1 <= int(seats) <= 30):
                messages.error(request, _("Укажите число столов от 1 до %(n)s и мест от 1 до 30.") % {"n": MAX_TABLES})
            else:
                with transaction.atomic():
                    # SQLite не различает регистр кириллицы в iexact, поэтому сравниваем сами
                    hall = next((h for h in Hall.objects.all() if h.name.lower() == name.lower()), None) \
                        or Hall.objects.create(name=name, sort_order=Hall.objects.count())
                    existing = set(hall.tables.values_list("number", flat=True))
                    new = [Table(hall=hall, number=str(n), seats=int(seats))
                           for n in range(1, int(count) + 1) if str(n) not in existing]
                    Table.objects.bulk_create(new)
                messages.success(request, _("Зал «%(h)s»: добавлено столов — %(n)s.") % {"h": name, "n": len(new)})
                return redirect("setup_wizard")
        elif action == "template":
            key = request.POST.get("template", "")
            if key in TEMPLATES:
                _import_report(request, importer.import_rows(rows_for(key)), str(TEMPLATES[key][0]))
                return redirect("setup_wizard")
            messages.error(request, _("Выберите один из готовых вариантов меню."))
        elif action == "import":
            upload = request.FILES.get("file")
            if upload is None:
                messages.error(request, _("Выберите файл с меню."))
            else:
                try:
                    report = importer.import_rows(importer.read_rows(upload))
                except importer.ImportProblem as exc:
                    messages.error(request, str(exc))
                else:
                    _import_report(request, report, upload.name[:60])
                    return redirect("setup_wizard")
        elif action == "finish":
            cafe.setup_done = True
            cafe.save(update_fields=["setup_done"])
            messages.success(request, _("Заведение настроено. Можно принимать заказы."))
            return redirect("floor")
    context = {
        "form": form, "status": _status(cafe), "cafe_obj": cafe,
        "templates": [(key, str(v[0]), str(v[1]), len(v[2])) for key, v in TEMPLATES.items()],
        "import_errors": request.session.pop("import_errors", []),
    }
    return render(request, "manage/setup.html", context)


@manager_only
def setup_menu_template(request):
    """Образец файла для импорта меню: открывается в Excel."""
    response = HttpResponse("﻿" + importer.TEMPLATE_CSV, content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="menu-template.csv"'
    return response


# ---------- резервная копия ----------

def _backup_files():
    folder = os.path.join(settings.BASE_DIR, "backups")
    if not os.path.isdir(folder):
        return []
    files = []
    for name in sorted(os.listdir(folder), reverse=True)[:10]:
        path = os.path.join(folder, name)
        if os.path.isfile(path):
            files.append({
                "name": name, "mb": round(os.path.getsize(path) / 1_000_000, 1),
                "when": datetime.fromtimestamp(os.path.getmtime(path)),
            })
    return files


def _is_sqlite():
    return settings.DATABASES["default"]["ENGINE"].endswith("sqlite3")


def _sqlite_snapshot():
    """Согласованная копия базы одним файлом, даже если касса сейчас работает. Возвращает байты."""
    handle, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(handle)
    try:
        source = sqlite3.connect(str(settings.DATABASES["default"]["NAME"]))
        target = sqlite3.connect(path)
        with target:
            source.backup(target)
        target.close()
        source.close()
        with open(path, "rb") as file:
            return file.read()
    finally:
        if os.path.exists(path):
            os.remove(path)


@manager_only
def manage_backup(request):
    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "download":
            if not _is_sqlite():
                messages.error(request, _("Для PostgreSQL используйте команду backup_db на сервере."))
                return redirect("manage_backup")
            data = _sqlite_snapshot()
            stamp = timezone.localtime().strftime("%Y-%m-%d_%H-%M")
            audit.record(request.user, audit.A.BACKUP, detail=_("Скачана копия базы"))
            response = HttpResponse(data, content_type="application/octet-stream")
            response["Content-Disposition"] = f'attachment; filename="cafepos-{stamp}.sqlite3"'
            return response
        if action == "server":
            try:
                call_command("backup_db", verbosity=0)
                audit.record(request.user, audit.A.BACKUP, detail=_("Копия сохранена на сервере"))
                messages.success(request, _("Копия сохранена в папке backups."))
            except Exception as exc:
                messages.error(request, _("Не удалось сделать копию: %(e)s") % {"e": str(exc)[:200]})
            return redirect("manage_backup")
    return render(request, "manage/backup.html", {"files": _backup_files(), "sqlite": _is_sqlite()})
