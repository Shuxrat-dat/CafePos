from decimal import Decimal

from django.db import transaction

from .models import RecipeItem, StockMovement


def write_off_order(order):
    """Списывает продукты по техкартам за оплаченный заказ (один раз на заказ)."""
    if StockMovement.objects.filter(order=order, kind=StockMovement.Kind.SALE).exists():
        return
    with transaction.atomic():
        for item in order.items.select_related("dish").prefetch_related("modifiers"):
            factor = Decimal(1)
            for modifier in item.modifiers.all():
                factor *= modifier.consumption_multiplier
            comment = f"{item.dish.name} × {item.quantity}"
            if factor != 1:
                comment += f", расход ×{factor:.2f}"
            for recipe in RecipeItem.objects.filter(dish=item.dish).select_related("product"):
                StockMovement.objects.create(
                    product=recipe.product,
                    kind=StockMovement.Kind.SALE,
                    quantity=recipe.quantity * item.quantity * factor,
                    order=order,
                    comment=comment,
                )


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