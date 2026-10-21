"""Установка кассы как приложения (PWA): манифест, service worker и страница «нет связи».

Приложение открывает тот же сайт в отдельном окне, без адресной строки, с иконкой на рабочем столе
или на экране телефона. Service worker ничего не хранит из заказов и данных: кэшируются только файлы
оформления (/static/), а при обрыве связи показывается страница «Нет связи с кассой».
"""
import hashlib
from pathlib import Path

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.templatetags.static import static
from django.utils.translation import get_language, gettext as _
from django.views.decorators.http import require_GET

THEME_COLOR = "#F7F3EC"
SW_PATH = Path(settings.BASE_DIR) / "static" / "js" / "sw.js"


def no_cache(response):
    response["Cache-Control"] = "no-cache, max-age=0"
    return response


@require_GET
def manifest(request):
    """Манифест приложения: название берётся из настроек заведения."""
    name = request.cafe.name or "CafePOS"
    data = {
        "id": "/",
        "name": name,
        "short_name": name[:12],
        "description": _("Касса, заказы, кухня и склад заведения"),
        "lang": get_language() or "uz",
        "start_url": "/?source=app",
        "scope": "/",
        "display": "standalone",
        "background_color": THEME_COLOR,
        "theme_color": THEME_COLOR,
        "icons": [
            {"src": static("icons/icon-192.png"), "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": static("icons/icon-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": static("icons/icon-maskable-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }
    response = JsonResponse(data, json_dumps_params={"ensure_ascii": False})
    response["Content-Type"] = "application/manifest+json; charset=utf-8"
    return no_cache(response)


@require_GET
def service_worker(request):
    """Service worker должен отдаваться с корня сайта, иначе он не сможет управлять всеми страницами."""
    code = SW_PATH.read_text(encoding="utf-8")
    # Версия кэша меняется вместе с кодом воркера: старый кэш удаляется сам
    version = hashlib.sha1(code.encode("utf-8")).hexdigest()[:10]
    response = HttpResponse(code.replace("__VERSION__", version), content_type="application/javascript; charset=utf-8")
    response["Service-Worker-Allowed"] = "/"
    return no_cache(response)


@require_GET
def offline(request):
    """Показывается вместо страницы, если нет связи с кассой. Работает без входа."""
    return no_cache(render(request, "offline.html"))
