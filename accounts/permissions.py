# Какие роли что могут. Чтобы изменить права, правьте только этот файл.
SERVICE_ROLES = {"owner", "admin", "waiter", "cashier"}  # зал и заказы
PAYMENT_ROLES = {"owner", "admin", "cashier"}            # оплата и чеки
DISCOUNT_ROLES = {"owner", "admin", "cashier"}           # скидки на заказ
KITCHEN_ROLES = {"owner", "admin", "cook"}               # экран кухни
REPORT_ROLES = {"owner", "admin"}                        # отчёты
SEE_ALL_ROLES = {"owner", "admin", "cashier"}            # видят заказы всех официантов
CANCEL_SENT_ROLES = {"owner", "admin"}                   # могут отменить заказ, уже ушедший на кухню
STOCK_ROLES = {"owner", "admin"}                         # склад: приход и пересчёт
MANAGE_ROLES = {"owner", "admin"}                        # меню, персонал, столы, настройки


def has_role(user, roles):
    return user.is_authenticated and (user.is_superuser or user.role in roles)


def role_required(roles):
    """Декоратор вью: пускает только вошедших пользователей с нужной ролью."""
    from functools import wraps

    from django.contrib.auth.decorators import login_required
    from django.core.exceptions import PermissionDenied

    def decorator(view):
        @login_required
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if not has_role(request.user, roles):
                raise PermissionDenied
            return view(request, *args, **kwargs)

        return wrapper

    return decorator
