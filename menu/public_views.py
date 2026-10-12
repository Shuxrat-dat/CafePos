from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Prefetch
from django.shortcuts import render
from django.urls import reverse
from django.utils.safestring import mark_safe

import qrcode
import qrcode.constants

from accounts.permissions import REPORT_ROLES

from .models import Category, Dish


def public_menu(request):
    """Меню для гостей: без входа, только просмотр."""
    categories = Category.objects.filter(is_active=True).prefetch_related(
        Prefetch("dishes", queryset=Dish.objects.filter(is_available=True, is_hidden=False), to_attr="available")
    )
    categories = [c for c in categories if c.available]
    return render(request, "menu/public_menu.html", {"categories": categories})


def _qr_svg(text):
    """Рисует QR-код как SVG своими силами, без зависимости от версии библиотеки."""
    qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(text)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    size = len(matrix)
    cells = "".join(
        f'<rect x="{x}" y="{y}" width="1" height="1"/>'
        for y, row in enumerate(matrix)
        for x, filled in enumerate(row)
        if filled
    )
    return mark_safe(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" shape-rendering="crispEdges">'
        f'<rect width="{size}" height="{size}" fill="#fff"/><g fill="#000">{cells}</g></svg>'
    )


@login_required
def menu_qr(request):
    user = request.user
    if not (user.is_superuser or user.role in REPORT_ROLES):
        raise PermissionDenied
    url = request.build_absolute_uri(reverse("public_menu"))
    host = request.get_host().split(":")[0]
    context = {
        "url": url,
        "qr": _qr_svg(url),
        "is_local": host in ("localhost", "127.0.0.1"),
    }
    return render(request, "menu/qr.html", context)