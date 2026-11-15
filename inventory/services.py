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
        dishes = {item.dish_id: item.dish for item in order.items.select_related("dish")}
        product_ids = sorted({product.id for dish in dishes.values() for product, _qty in recipe_lines(dish)})
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
            if item.by_weight:
                comment = _("%(name)s, %(g)s г") % {"name": item.dish.name, "g": item.weight_grams or 0}
            else:
                comment = f"{item.dish.name} × {item.quantity}"
            if factor != 1:
                comment += _(", расход ×%(f)s") % {"f": f"{factor:.2f}"}
            for product, per_unit in recipe_lines(item.dish):
                StockMovement.objects.create(
                    product=product,
                    kind=StockMovement.Kind.SALE,
                    quantity=per_unit * item.quantity * item.weight_factor * factor,
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
        for product, per_unit in recipe_lines(item.dish):
            need[product] = need.get(product, Decimal(0)) + per_unit * qty * item.weight_factor * factor
    return [
        {"name": p.name, "need": n, "stock": p.stock, "unit": p.get_unit_display()}
        for p, n in need.items()
        if p.stock < n
    ]

def recipe_lines(dish):
    """Расход продуктов на одну порцию блюда: [(продукт, количество)].
    У комбо-набора — его собственная техкарта (например, упаковка) плюс техкарты входящих блюд."""
    lines = [(r.product, r.quantity) for r in RecipeItem.objects.filter(dish=dish).select_related("product")]
    for part in dish.combo_items.select_related("component"):
        for r in RecipeItem.objects.filter(dish=part.component).select_related("product"):
            lines.append((r.product, r.quantity * part.quantity))
    return lines
