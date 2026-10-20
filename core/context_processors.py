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

    from pos.models import Order
    open_count = Order.objects.filter(status=Order.Status.OPEN).count() or 5
    printer_problems = None
    if has_role(user, MANAGE_ROLES):
        from printing.services import problem_count

        printer_problems = problem_count() or None

    items = [
        ("orders", _("Обзор"), "home", SERVICE_ROLES, {"orders", "order_view"}),
        ("floor", _("Заказы"), "receipt", SERVICE_ROLES, {"floor"} | order_pages, open_count),
        ("manage_menu", _("Меню"), "utensils", MANAGE_ROLES, {"manage_menu", "dish_create", "dish_edit", "manage_categories", "manage_categories_edit", "manage_modifiers", "manage_modifiers_edit"}),
        ("manage_tables", _("Столы"), "table", MANAGE_ROLES, {"manage_tables", "table_edit"}),
        ("new_order", _("Касса"), "wallet", SERVICE_ROLES, set()),
        ("stock", _("Склад"), "package", STOCK_ROLES, {"stock", "stock_count", "stock_movements", "stock_writeoff", "product_create", "product_edit"}),
        ("reports", _("Отчёты"), "bar-chart", REPORT_ROLES, {"reports"}),
        ("manage_printers", _("Принтеры"), "printer", MANAGE_ROLES, {"manage_printers", "printer_create", "printer_edit"}, printer_problems),
        ("manage_settings", _("Настройки"), "settings", MANAGE_ROLES, {"manage_settings", "manage_staff", "staff_create", "staff_edit", "menu_qr", "tables_qr"}),
    ]
    res = []
    for entry in items:
        name = entry[0]
        label = entry[1]
        icon = entry[2]
        roles = entry[3]
        pages = entry[4]
        badge = entry[5] if len(entry) > 5 else None
        if has_role(user, roles):
            res.append({
                "url": reverse(name),
                "label": label,
                "icon": icon,
                "active": page in pages,
                "name": name,
                "post": name == "new_order",
                "badge": badge,
            })
    return res


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
    if has_role(user, MANAGE_ROLES) and not cafe_settings.setup_done:
        from menu.models import Dish
        from pos.models import Hall

        # Подсказка нужна только пустой установке: где уже есть залы и меню, мастер не навязываем
        context["setup_needed"] = not (Hall.objects.exists() and Dish.objects.exists())
    if user.is_authenticated:
        context["can_new_order"] = has_role(user, SERVICE_ROLES)
        from pos.services import get_current_shift
        context["current_shift"] = get_current_shift()
    return context
