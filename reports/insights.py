"""Выводы для владельца простыми словами: что произошло, что прибыльно, что убрать из меню.

Все функции только читают данные. Себестоимость блюда считается по техкарте и текущим ценам продуктов.
"""
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from statistics import median

from django.db.models import Sum
from django.utils import timezone
from django.utils.translation import gettext as _

from inventory.models import RecipeItem
from menu.models import Dish
from pos.models import Order, OrderItem

HISTORY_DAYS_NEEDED = 7   # меньше дней с продажами советы по меню не даём
LOW_MARGIN = 20           # наценка ниже этого процента считается почти нулевой
HIGH_MARGIN = 65          # наценка выше этого процента считается высокой
WEAK_MARGIN = 50          # блюдо с низкой наценкой и малым спросом — кандидат на удаление


def money(value):
    return f"{int(round(float(value))):,}".replace(",", " ") + " " + _("сум")


def unit_costs():
    """{id блюда: себестоимость порции}. Блюд без техкарты в словаре нет."""
    costs = defaultdict(Decimal)
    for recipe in RecipeItem.objects.select_related("product"):
        costs[recipe.dish_id] += recipe.quantity * recipe.product.cost_per_unit
    return dict(costs)


def margin_pct(price, cost):
    if cost is None or not price:
        return None
    return round((Decimal(price) - cost) / Decimal(price) * 100)


def dish_margins(sold_rows):
    """sold_rows: [{'obj': блюдо, 'qty': int, 'revenue': Decimal}]. Добавляет cost, profit и margin."""
    costs = unit_costs()
    rows = []
    for row in sold_rows:
        unit = costs.get(row["obj"].id)
        if unit is None or not row["revenue"]:
            rows.append({**row, "cost": None, "profit": None, "margin": None})
            continue
        cost = unit * row["qty"]
        profit = row["revenue"] - cost
        rows.append({**row, "cost": cost, "profit": profit, "margin": round(profit / row["revenue"] * 100)})
    return rows


def menu_advice(now=None, days=30):
    """Советы по меню за последние дни. Возвращает {enough, loss, remove, promote}."""
    now = now or timezone.now()
    since = now - timedelta(days=days)
    paid = OrderItem.objects.filter(order__status=Order.Status.PAID, order__closed_at__gte=since)
    active_days = {
        timezone.localtime(t).date()
        for t in Order.objects.filter(status=Order.Status.PAID, closed_at__gte=since).values_list("closed_at", flat=True)
    }
    result = {"enough": len(active_days) >= HISTORY_DAYS_NEEDED, "days": days, "loss": [], "remove": [], "promote": []}
    if not result["enough"]:
        return result

    qty = {r["dish_id"]: r["qty"] for r in paid.values("dish_id").annotate(qty=Sum("quantity"))}
    costs = unit_costs()
    dishes = list(Dish.objects.filter(is_hidden=False, is_available=True).select_related("category"))
    if not dishes:
        return result
    counts = sorted(qty.get(d.id, 0) for d in dishes)
    low_cut = max(2, counts[len(counts) // 4])
    mid = median(counts)

    for dish in dishes:
        q = qty.get(dish.id, 0)
        cost = costs.get(dish.id)
        pct = margin_pct(dish.price, cost)
        entry = {"dish": dish, "qty": q, "margin": pct}
        if pct is not None and pct < LOW_MARGIN and q > 0:
            entry["reason"] = _("Наценка всего %(p)s%%. Поднимите цену или пересмотрите рецепт.") % {"p": pct}
            result["loss"].append(entry)
        elif q <= low_cut and (pct is None or pct < WEAK_MARGIN):
            entry["reason"] = _("За %(d)s дней заказали %(n)s раз, прибыль небольшая.") % {"d": days, "n": q}
            result["remove"].append(entry)
        elif pct is not None and pct >= HIGH_MARGIN and q <= mid:
            entry["reason"] = _("Наценка %(p)s%%, но заказывают редко. Поставьте в начало меню.") % {"p": pct}
            result["promote"].append(entry)

    result["loss"].sort(key=lambda e: -e["qty"])
    result["remove"].sort(key=lambda e: e["qty"])
    result["promote"].sort(key=lambda e: -(e["margin"] or 0))
    for key in ("loss", "remove", "promote"):
        result[key] = result[key][:6]
    return result


def headline(*, revenue, orders_count, avg_check, single_day, compare_label, compare_change,
             best_label, best_sum, top_dish, margin, margin_pct_value, has_cost, cancelled_count, cancelled_sum,
             low_stock_count):
    """Короткая сводка списком фраз: [{'kind': 'good'|'bad'|'info', 'text': ...}]."""
    if not orders_count:
        return [{"kind": "info", "text": _("За этот период оплаченных заказов нет.")}]
    items = [{"kind": "info", "text": _("Оплачено заказов: %(n)s на %(sum)s. Средний чек %(avg)s.") % {
        "n": orders_count, "sum": money(revenue), "avg": money(avg_check)}}]

    if compare_change is not None:
        better = compare_change >= 0
        items.append({"kind": "good" if better else "bad", "text": _("%(label)s: %(sign)s%(p)s%%.") % {
            "label": compare_label, "sign": "+" if better else "−", "p": abs(compare_change)}})

    if best_label:
        what = _("Самый загруженный час") if single_day else _("Лучший день")
        items.append({"kind": "info", "text": _("%(what)s: %(when)s, выручка %(sum)s.") % {
            "what": what, "when": best_label, "sum": money(best_sum)}})

    if top_dish:
        items.append({"kind": "info", "text": _("Чаще всего заказывали: %(name)s (%(n)s шт).") % top_dish})

    if has_cost:
        kind = "good" if margin_pct_value >= 50 else "bad" if margin_pct_value < 30 else "info"
        items.append({"kind": kind, "text": _("Валовая прибыль %(m)s, это %(p)s%% выручки.") % {
            "m": money(margin), "p": margin_pct_value}})

    if cancelled_count:
        items.append({"kind": "bad", "text": _("Отменено заказов: %(n)s на %(sum)s.") % {
            "n": cancelled_count, "sum": money(cancelled_sum)}})
    if low_stock_count:
        items.append({"kind": "bad", "text": _("Заканчиваются продукты: %(n)s. Откройте «Склад → Закупка».") % {
            "n": low_stock_count}})
    return items
