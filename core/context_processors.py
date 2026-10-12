from django.conf import settings
from django.db.models import F
from django.urls import reverse
from django.utils.translation import gettext as _

from accounts.permissions import KITCHEN_ROLES, MANAGE_ROLES, REPORT_ROLES, SERVICE_ROLES, STOCK_ROLES, has_role


def _nav(request):
    user = request.user
    if not user.is_authenticated:
        return []
    match = request.resolver_match
    page = getattr(match, "url_name", "") or ""
    # Экран заказа относится к «Залу», если заказ за столом, и к «Кассе» — если навынос или доставка
    order_pages = {"order_detail", "pay", "receipt", "precheck"}
    counter = False
    if page in order_pages and match and match.kwargs.get("order_id"):
        from pos.models import Order

        counter = Order.objects.filter(id=match.kwargs["order_id"]).exclude(order_type="dine_in").exists()
    items = [
        ("floor", _("Зал"), "layout-grid", SERVICE_ROLES, {"floor"} | (set() if counter else order_pages)),
        ("orders", _("Заказы"), "receipt", SERVICE_ROLES, {"orders", "order_view"}),
        # «Касса» сразу открывает новый заказ навынос (POST), отдельной страницы не нужно
        ("new_order", _("Касса"), "wallet", SERVICE_ROLES, order_pages if counter else set()),
        ("manage_menu", _("Меню"), "book-open", MANAGE_ROLES, {"manage_menu", "dish_create", "dish_edit", "manage_categories", "manage_categories_edit", "manage_modifiers", "manage_modifiers_edit"}),
        ("kitchen_board", _("Кухня"), "chef-hat", KITCHEN_ROLES, {"kitchen_board"}),
        ("stock", _("Склад"), "package", STOCK_ROLES, {"stock", "stock_count", "stock_movements", "stock_writeoff", "product_create", "product_edit"}),
        ("reports", _("Отчёты"), "bar-chart", REPORT_ROLES, {"reports"}),
        ("manage_staff", _("Сотрудники"), "users", MANAGE_ROLES, {"manage_staff", "staff_create", "staff_edit"}),
        ("manage_settings", _("Настройки"), "settings", MANAGE_ROLES, {"manage_settings", "manage_tables", "table_edit", "menu_qr", "tables_qr"}),
    ]
    return [
        {"url": reverse(name), "label": label, "icon": icon, "active": page in pages, "name": name, "post": name == "new_order"}
        for name, label, icon, roles, pages in items
        if has_role(user, roles)
    ]


def cafe(request):
    cafe_settings = getattr(request, "cafe", None)
    if cafe_settings is None:
        from .models import CafeSettings

        cafe_settings = CafeSettings.get()
    context = {
        "cafe": cafe_settings,
        "DEMO_MODE": settings.DEMO_MODE,
        "nav_items": _nav(request),
        "low_stock_count": 0,
    }
    if settings.DEMO_MODE:
        context["demo_roles"] = [
            ("owner", _("Владелец"), "store"),
            ("admin", _("Администратор"), "settings"),
            ("kassir", _("Кассир"), "wallet"),
            ("ofitsiant", _("Официант"), "utensils"),
            ("oshpaz", _("Повар"), "chef-hat"),
        ]
    user = request.user
    if has_role(user, STOCK_ROLES):
        from inventory.models import Product

        context["low_stock_count"] = Product.objects.filter(stock__lte=F("min_stock")).count()
    if user.is_authenticated:
        context["can_new_order"] = has_role(user, SERVICE_ROLES)
    return context
