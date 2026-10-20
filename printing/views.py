import base64
import io
import json
import secrets
import zipfile

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from accounts.permissions import MANAGE_ROLES, role_required
from pos.models import Order

from . import services
from .forms import PrinterForm
from .models import PrintAgent, Printer, PrintJob

manager_only = role_required(MANAGE_ROLES)
AGENT_FILES = ("cafepos_print_agent.py", "start_print_agent.bat", "install_print_agent.bat", "install_print_agent.ps1")


# ---------- Настройки → Принтеры ----------

@manager_only
def manage_printers(request):
    agent = PrintAgent.get()
    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "new_token":
            agent.new_token()
            messages.success(request, _("Создан новый токен. Скачайте программу печати заново или впишите токен в её настройки."))
        elif action == "retry":
            job = get_object_or_404(PrintJob, id=request.POST.get("job", ""), status=PrintJob.Status.ERROR)
            services.retry(job)
            messages.success(request, _("Задание поставлено в очередь ещё раз."))
        elif action == "check":
            services.request_check()
            if agent.is_online:
                messages.success(request, _("Проверяем связь с принтерами. Результат появится через несколько секунд."))
            else:
                messages.warning(request, _("Программа печати не на связи, проверить принтеры нельзя. Запустите её на компьютере в кафе."))
        return redirect("manage_printers")
    printers = list(Printer.objects.prefetch_related("categories"))
    for printer in printers:
        printer.recent = printer.last_job
    context = {
        "printers": printers,
        "agent": agent,
        "jobs": PrintJob.objects.select_related("printer").order_by("-id")[:20],
        "pending": PrintJob.objects.filter(status=PrintJob.Status.PENDING).count(),
    }
    if request.headers.get("HX-Request"):
        # Список принтеров обновляется на странице сам: видно, как меняется связь
        return render(request, "manage/_printers_list.html", context)
    return render(request, "manage/printers.html", context)


@manager_only
def printer_form(request, printer_id=None):
    printer = get_object_or_404(Printer, id=printer_id) if printer_id else None
    form = PrinterForm(request.POST or None, instance=printer)
    if request.method == "POST":
        if form.is_valid():
            printer = form.save()
            if request.POST.get("action") == "test":
                services.queue_test(printer)
                messages.success(request, _("Принтер сохранён, тестовая страница поставлена в очередь."))
            else:
                messages.success(request, _("Принтер сохранён."))
            return redirect("manage_printers")
        messages.error(request, _("Проверьте поля формы."))
    return render(request, "manage/printer_form.html", {"form": form, "printer": printer})


@manager_only
@require_POST
def printer_test(request, printer_id):
    printer = get_object_or_404(Printer, id=printer_id)
    services.queue_test(printer)
    if PrintAgent.get().is_online:
        messages.success(request, _("Тестовая страница отправлена на «%(name)s».") % {"name": printer.name})
    else:
        messages.warning(request, _("Тестовая страница в очереди, но программа печати не на связи. Запустите её на компьютере в кафе."))
    return redirect("manage_printers")


@manager_only
@require_POST
def printer_delete(request, printer_id):
    printer = get_object_or_404(Printer, id=printer_id)
    printer.delete()
    messages.success(request, _("Принтер удалён."))
    return redirect("manage_printers")


@manager_only
def agent_download(request):
    """Архив с программой печати и файлом настроек, где уже записаны адрес кассы и токен."""
    agent = PrintAgent.get()
    config = (
        "[server]\n"
        f"url = {request.build_absolute_uri('/').rstrip('/')}\n"
        f"token = {agent.token}\n\n"
        "[agent]\n"
        "# Как часто спрашивать кассу о новых заданиях, секунд\n"
        "interval = 2\n"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in AGENT_FILES:
            content = (settings.BASE_DIR / "print_agent" / name).read_bytes()
            if name.endswith((".bat", ".ps1", ".ini")):
                # cmd.exe надёжно читает только окончания строк Windows (CRLF)
                content = content.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            archive.writestr(f"cafepos_print_agent/{name}", content)
        archive.writestr("cafepos_print_agent/cafepos_print_agent.ini", config.replace("\n", "\r\n"))
    response = HttpResponse(buffer.getvalue(), content_type="application/zip")
    response["Content-Disposition"] = 'attachment; filename="cafepos_print_agent.zip"'
    return response


# ---------- печать из кассы ----------

@login_required
@require_POST
def print_order(request, order_id):
    """Кнопка «На принтер» на экране чека и пречека."""
    order = get_object_or_404(Order, id=order_id)
    precheck = request.POST.get("kind") == "precheck"
    if precheck:
        if order.status != Order.Status.OPEN or not (request.user.sees_all_orders or order.waiter_id == request.user.id):
            raise PermissionDenied
    elif not request.user.can_take_payment or order.status != Order.Status.PAID:
        raise PermissionDenied
    printer = None
    if request.POST.get("printer", "").isdigit():
        printer = get_object_or_404(services.receipt_printers(), id=int(request.POST["printer"]))
    jobs = services.queue_receipt(order, printer=printer, precheck=precheck)
    if not jobs:
        messages.error(request, _("Нет включённых принтеров для чеков. Добавьте принтер в «Настройки → Принтеры»."))
    elif PrintAgent.get().is_online:
        messages.success(request, _("Отправлено на принтер."))
    else:
        messages.warning(request, _("Задание в очереди, но программа печати не на связи. Чек напечатается, когда она запустится."))
    return redirect("precheck" if precheck else "receipt", order_id=order.id)


@login_required
@require_GET
def order_print_status(request, order_id):
    """Состояние печати по заказу для экрана чека: обновляется само, показывает ошибку принтера."""
    order = get_object_or_404(Order, id=order_id)
    if not (request.user.sees_all_orders or order.waiter_id == request.user.id or request.user.can_take_payment):
        raise PermissionDenied
    jobs = list(order.print_jobs.select_related("printer").order_by("-id")[:6])
    return render(request, "pos/_print_status.html", {
        "order": order, "jobs": jobs, "agent_online": PrintAgent.get().is_online,
        "waiting": any(j.status in (PrintJob.Status.PENDING, PrintJob.Status.SENT) for j in jobs),
    })


# ---------- API программы печати ----------

def _agent(request):
    header = request.headers.get("Authorization", "")
    token = header[7:].strip() if header.startswith("Bearer ") else ""
    agent = PrintAgent.get()
    if not token or not secrets.compare_digest(token, agent.token):
        return None
    return agent


def _printer_info(printer):
    return {
        "id": printer.id, "name": printer.name, "connection": printer.connection,
        "host": printer.host, "port": printer.port, "windows_name": printer.windows_name,
    }


@csrf_exempt
@require_GET
def agent_jobs(request):
    agent = _agent(request)
    if agent is None:
        return JsonResponse({"error": "unauthorized"}, status=401)
    jobs = services.claim_jobs(agent, request.headers.get("X-Agent-Info", ""))
    return JsonResponse({
        "jobs": [
            {"id": j.id, "title": j.title, "printer": _printer_info(j.printer), "data": base64.b64encode(bytes(j.data)).decode()}
            for j in jobs
        ],
        # Принтеры, к которым нужно попробовать подключиться (без печати), и прислать результат на /status/
        "check": [_printer_info(p) for p in services.printers_to_check(agent)],
    })


@csrf_exempt
@require_POST
def agent_status(request):
    if _agent(request) is None:
        return JsonResponse({"error": "unauthorized"}, status=401)
    try:
        payload = json.loads(request.body or b"{}")
    except ValueError:
        return JsonResponse({"error": "bad json"}, status=400)
    results = payload.get("printers", [])
    if isinstance(results, list):
        services.record_check([r for r in results if isinstance(r, dict)])
    return JsonResponse({"ok": True})


@csrf_exempt
@require_POST
def agent_result(request, job_id):
    if _agent(request) is None:
        return JsonResponse({"error": "unauthorized"}, status=401)
    job = get_object_or_404(PrintJob, id=job_id)
    try:
        payload = json.loads(request.body or b"{}")
    except ValueError:
        return JsonResponse({"error": "bad json"}, status=400)
    services.finish_job(job, bool(payload.get("ok")), str(payload.get("code", ""))[:30], str(payload.get("detail", ""))[:200])
    return JsonResponse({"ok": True})
