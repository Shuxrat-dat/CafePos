"""Прогноз расхода склада: на сколько дней хватит продукта и сколько докупить.

Считается по реальным продажам (движения типа «Продажа») за последние недели. Чем больше история,
тем точнее прогноз. Если данных мало, показываем только минимальный остаток.
"""
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from math import ceil

from django.utils import timezone

from .models import Product, StockMovement

WINDOW_DAYS = 14        # за какой период смотрим продажи
MIN_DAYS_OF_DATA = 3    # меньше дней с продажами — прогноз не строим
COVER_DAYS = 7          # на сколько дней вперёд предлагаем закупить
WEEKEND = (4, 5, 6)     # пятница, суббота, воскресенье: в кафе обычно загруженнее
BOOST_SHOWN_FROM = 20   # показываем «по выходным больше» от этого процента


def _round_up(value, unit):
    """Округляет количество вверх до удобного для закупки значения."""
    value = float(value)
    if value <= 0:
        return 0
    if unit == Product.Unit.PIECE:
        return ceil(value)
    step = 1000 if value >= 10000 else 500 if value >= 2000 else 100
    return int(ceil(value / step) * step)


def build_forecast(now=None):
    """Возвращает словарь {id продукта: прогноз}. Прогноз — словарь с полями:
    usage (расход в день), days_left, level, weekend_boost, suggest, nodata."""
    now = now or timezone.now()
    since = now - timedelta(days=WINDOW_DAYS)
    used = defaultdict(float)
    weekend_used, weekday_used = defaultdict(float), defaultdict(float)
    operating_days, weekend_days, weekday_days = set(), set(), set()

    rows = StockMovement.objects.filter(kind=StockMovement.Kind.SALE, created_at__gte=since).values_list(
        "product_id", "quantity", "created_at"
    )
    for product_id, quantity, created_at in rows:
        local = timezone.localtime(created_at)
        day = local.date()
        amount = -float(quantity)
        operating_days.add(day)
        used[product_id] += amount
        if local.weekday() in WEEKEND:
            weekend_used[product_id] += amount
            weekend_days.add(day)
        else:
            weekday_used[product_id] += amount
            weekday_days.add(day)

    enough = len(operating_days) >= MIN_DAYS_OF_DATA
    result = {}
    for product in Product.objects.all():
        usage = used[product.id] / len(operating_days) if enough else 0.0
        stock = float(product.stock)
        days_left = stock / usage if usage > 0 else None

        boost = None
        if enough and len(weekend_days) >= 2 and len(weekday_days) >= 2:
            weekend_avg = weekend_used[product.id] / len(weekend_days)
            weekday_avg = weekday_used[product.id] / len(weekday_days)
            if weekday_avg > 0:
                percent = round((weekend_avg / weekday_avg - 1) * 100)
                boost = percent if percent >= BOOST_SHOWN_FROM else None

        if stock <= 0:
            level = "out"
        elif stock <= float(product.min_stock) or (days_left is not None and days_left <= 2):
            level = "critical"
        elif days_left is not None and days_left <= 5:
            level = "soon"
        else:
            level = "ok"

        need = usage * COVER_DAYS * (1 + (boost or 0) / 300) - stock  # выходные учитываем мягко: они 3 дня из 7
        if float(product.min_stock) > 0:
            need = max(need, float(product.min_stock) * 2 - stock)
        suggest = _round_up(need, product.unit) if level != "ok" else 0

        result[product.id] = {
            "usage": usage,
            "days_left": days_left,
            "level": level,
            "weekend_boost": boost,
            "suggest": suggest,
            "nodata": not enough,
            "cost": Decimal(suggest) * product.cost_per_unit if suggest else Decimal(0),
        }
    return result


def shopping_list(now=None):
    """Продукты, которые пора докупить, самые срочные первыми."""
    forecast = build_forecast(now)
    rows = []
    for product in Product.objects.all():
        info = forecast[product.id]
        if info["level"] == "ok" or not info["suggest"]:
            continue
        rows.append({"product": product, **info})
    order = {"out": 0, "critical": 1, "soon": 2}
    rows.sort(key=lambda r: (order[r["level"]], r["days_left"] if r["days_left"] is not None else 1e9))
    return rows
