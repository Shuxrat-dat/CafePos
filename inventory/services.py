from decimal import Decimal

from django.db import transaction
from django.db.models import F
from django.utils.translation import gettext as _

from .models import Product, RecipeItem, StockMovement


def write_off_order(order):
    """Списывает продукты по техкартам за оплаченный заказ (один раз на заказ)."""
    if StockMovement.objects.filter(order=order, kind=StockMovement.Kind.SALE).exists():
        return
    with transaction.atomic():
        dish_ids = [item.dish_id for item in order.items.all()]
        product_ids = sorted(list(
            RecipeItem.objects.filter(dish_id__in=dish_ids).values_list("product_id", flat=True).distinct()
        ))
        if product_ids:
            list(Product.objects.filter(id__in=product_ids).select_for_update())
        # Запоминаем, кто был выше минимума: после продажи сообщим только о тех, кто опустился до него
        was_ok = set(
            Product.objects.filter(id__in=product_ids, stock__gt=F("min_stock")).values_list("id", flat=True)
        )

        for item in order.items.select_related("dish").prefetch_related("modifiers"):
            factor = Decimal(1)
            for modifier in item.modifiers.all():
                factor *= modifier.consumption_multiplier
            comment = f"{item.dish.name} × {item.quantity}"
            if factor != 1:
                comment += _(", расход ×%(f)s") % {"f": f"{factor:.2f}"}
            for recipe in RecipeItem.objects.filter(dish=item.dish).select_related("product"):
                StockMovement.objects.create(
                    product=recipe.product,
                    kind=StockMovement.Kind.SALE,
                    quantity=recipe.quantity * item.quantity * factor,
                    order=order,
                    comment=comment,
                )
        if was_ok:
            transaction.on_commit(lambda: _warn_low_stock(was_ok))


def _warn_low_stock(was_ok_ids):
    """После оплаты: продукты, которые только что опустились до минимума, уходят в Telegram."""
    from core import telegram

    crossed = list(Product.objects.filter(id__in=was_ok_ids, stock__lte=F("min_stock")).order_by("name"))
    telegram.notify_low_stock(crossed)


def find_shortages(lines):
    """lines: пары (позиция заказа, сколько штук оплачивается).
    Возвращает продукты, которых на складе меньше, чем нужно."""
    need = {}
    for item, qty in lines:
        factor = Decimal(1)
        for modifier in item.modifiers.all():
            factor *= modifier.consumption_multiplier
        for recipe in RecipeItem.objects.filter(dish=item.dish).select_related("product"):
            product = recipe.product
            need[product] = need.get(product, Decimal(0)) + recipe.quantity * qty * factor
    return [
        {"name": p.name, "need": n, "stock": p.stock, "unit": p.get_unit_display()}
        for p, n in need.items()
        if p.stock < n
    ]