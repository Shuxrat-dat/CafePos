"""Меню CafePOS -> каталог Juma (MERCHANT_API.md, раздел 2). Источник правды — касса: Juma меню не редактирует.

Что отправляется: активные категории и блюда, которые не скрыты. Блюда на вес пока не отправляются (пересчёт по
фактическому весу в Juma появится позже). Акции превращаются в цену со скидкой. Модификаторы CafePOS: именованная
группа — выбор не более одного (размер), пустая группа — добавки.
"""
import hashlib
import json
import logging
import time

from django.utils import timezone

from menu.models import Category, Dish, Promotion, promo_price

from . import client
from .models import JumaState

log = logging.getLogger(__name__)

MAX_CATEGORIES, MAX_ITEMS, MAX_GROUPS, MAX_OPTIONS = 200, 5000, 20, 50


def _title(uz, ru):
    ru = (ru or "").strip()
    return {"uz": (uz or "").strip() or ru, "ru": ru}


def _best_percent(dish, promotions, moment):
    return max((p.percent for p in promotions if p.runs_at(moment) and p.applies_to(dish)), default=0)


def _group_id(name):
    return "g-" + hashlib.sha1(name.encode()).hexdigest()[:10] if name else "g-extra"


def _modifier_groups(dish):
    groups = {}
    for modifier in dish.modifiers.all():
        groups.setdefault(modifier.group.strip(), []).append(modifier)
    result = []
    for name, modifiers in sorted(groups.items(), key=lambda kv: (kv[0] == "", kv[0])):
        options = [
            {"external_id": f"m-{m.id}", "name": _title(m.name_uz, m.name), "price_delta": str(m.price_delta)}
            for m in modifiers[:MAX_OPTIONS]
        ]
        result.append({
            "external_id": _group_id(name),
            "name": _title("", name) if name else {"uz": "Qo'shimchalar", "ru": "Добавки"},
            # Именованная группа — взаимоисключающий выбор, добавки можно брать все
            "min": 0, "max": 1 if name else len(options), "options": options,
        })
    return result[:MAX_GROUPS]


def build_catalog(moment=None):
    """Каталог в формате Juma без версии: {"categories": [...], "items": [...]}."""
    moment = timezone.localtime(moment) if moment else timezone.localtime()
    promotions = list(Promotion.objects.filter(is_active=True).prefetch_related("categories", "dishes"))
    categories = list(Category.objects.filter(is_active=True).order_by("sort_order", "id")[:MAX_CATEGORIES])
    category_ids = {c.id for c in categories}
    dishes = (
        Dish.objects.filter(is_hidden=False, sold_by_weight=False, category_id__in=category_ids)
        .select_related("category").prefetch_related("modifiers").order_by("category__sort_order", "sort_order", "id")[:MAX_ITEMS]
    )
    items = []
    for dish in dishes:
        item = {
            "external_id": f"d-{dish.id}",
            "category_id": f"c-{dish.category_id}",
            "name": _title(dish.name_uz, dish.name),
            "description": {"uz": (dish.description_uz or dish.description)[:2000], "ru": dish.description[:2000]},
            "price": str(dish.price),
            "available": dish.is_available,
            "modifier_groups": _modifier_groups(dish),
        }
        percent = _best_percent(dish, promotions, moment)
        if percent:
            discounted = promo_price(dish.price, percent)
            if 0 <= discounted < dish.price:
                item["discount_price"] = str(discounted)
        if dish.prep_minutes:
            item["prep_minutes"] = dish.prep_minutes
        items.append(item)
    return {
        "categories": [{"external_id": f"c-{c.id}", "name": _title(c.name_uz, c.name), "sort": i} for i, c in enumerate(categories)],
        "items": items,
    }


def _digest(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def sync_catalog(force=False):
    """Отправляет каталог, если он изменился с прошлого раза. True — в Juma актуальный каталог (или Juma не настроена)."""
    if not client.enabled():
        return False
    state = JumaState.get()
    payload = build_catalog()
    digest = _digest(payload)
    if digest == state.catalog_hash and not force:
        return True

    version = max(int(time.time()), state.catalog_version + 1)
    for _attempt in range(2):
        code, body = client.put_catalog({"version": version, **payload})
        if not client.ok(code):
            state.last_error = client.reason(code, body)[:255]
            state.save(update_fields=["last_error"])
            return False
        if body.get("applied", True):
            break
        # В Juma версия выше нашей (например, после переустановки кассы): продолжаем с неё
        version = max(version, int(body.get("version", 0))) + 1
    else:
        return False
    state.catalog_version, state.catalog_hash = version, digest
    state.last_catalog_at, state.last_error = timezone.now(), ""
    state.save(update_fields=["catalog_version", "catalog_hash", "last_catalog_at", "last_error"])
    return True
