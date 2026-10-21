import re

from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect

from .permissions import KITCHEN_ROLES, MANAGE_ROLES, PAYMENT_ROLES, REPORT_ROLES, SERVICE_ROLES, STOCK_ROLES

PAY_RE = re.compile(r"^/orders/\d+/(pay|receipt)/$")
OPEN_PREFIXES = ("/admin/", "/login/", "/logout/", "/static/", "/media/", "/menu/", "/lang/", "/demo/", "/ping/",
                 "/manifest.webmanifest", "/sw.js", "/offline/")


class RoleAccessMiddleware:
    """Первая линия защиты: роль пользователя проверяется по адресу страницы.
    Вью дополнительно проверяют права на конкретные действия."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user
        path = request.path
        if user.is_authenticated and not user.is_superuser and not path.startswith(OPEN_PREFIXES):
            if user.role not in self._allowed_roles(path):
                if path == "/" and user.role == "cook":
                    return redirect("kitchen_board")
                raise PermissionDenied
        return self.get_response(request)

    @staticmethod
    def _allowed_roles(path):
        if path.startswith("/kitchen/"):
            return KITCHEN_ROLES
        if path.startswith("/reports/"):
            return REPORT_ROLES
        if path.startswith("/stock/"):
            return STOCK_ROLES
        if path.startswith("/manage/"):
            return MANAGE_ROLES
        if PAY_RE.match(path):
            return PAYMENT_ROLES
        return SERVICE_ROLES
