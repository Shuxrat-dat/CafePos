from decimal import Decimal, InvalidOperation

from django import forms
from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import F, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from accounts.permissions import STOCK_ROLES, role_required
from core.forms import StyledModelForm

from urllib.parse import quote

from django.utils import timezone

from .forecast import build_forecast, shopping_list
from .models import Product, RecipeItem, StockMovement

MAX_VALUE = Decimal(10_000_000)

stock_only = role_required(STOCK_ROLES)


def _decimal(raw):
    """Число из текста (запятая тоже подходит) или None, если это не число."""
    try:
        value = Decimal(raw.strip().replace(",", ".").replace(" ", "").replace(" ", ""))
    except (InvalidOperation, AttributeError):
        return None
    return value if value.is_finite() else None


def _movement_form(request, kind):
    """Общая проверка формы прихода и списания. Возвращает (ошибка, продукт, количество, цена, комментарий)."""
    pid = request.POST.get("product", "")
    product = Product.objects.filter(id=pid).first() if pid.isdigit() else None
    quantity = _decimal(request.POST.get("quantity", ""))
    cost_raw = request.POST.get("cost", "").strip()
    cost = _decimal(cost_raw) if cost_raw else None
    comment = request.POST.get("comment", "").strip()[:200]

    error = ""
    if product is None:
        error = _("Выберите продукт.")
    elif quantity is None or quantity <= 0 or quantity > MAX_VALUE:
        error = _("Введите количество числом больше нуля.")
    elif kind == StockMovement.Kind.INCOME and cost_raw and (cost is None or cost < 0 or cost > MAX_VALUE):
        error = _("Цена должна быть числом не меньше нуля.")
    elif kind == StockMovement.Kind.WRITEOFF and not comment:
        error = _("Укажите причину списания.")
    return error, product, quantity, cost, comment


@stock_only
def stock(request):
    """Остатки и приход товара."""
    error = ""
    form = {}

    if request.method == "POST":
        form = request.POST
        error, product, quantity, cost, comment = _movement_form(request, StockMovement.Kind.INCOME)
        if not error:
            with transaction.atomic():
                if cost is not None:
                    product.cost_per_unit = cost.quantize(Decimal("0.01"))
                    product.save(update_fields=["cost_per_unit"])
                StockMovement.objects.create(
                    product=product,
                    kind=StockMovement.Kind.INCOME,
                    quantity=quantity.quantize(Decimal("0.001")),
                    comment=comment,
                    created_by=request.user,
                )
            return redirect(f"{reverse('stock')}?ok=1")

    search = request.GET.get("q", "").strip()[:50]
    only_low = request.GET.get("low") == "1"
    products = Product.objects.all()
    if search:
        products = products.filter(Q(name__icontains=search) | Q(name_uz__icontains=search))
    if only_low:
        products = products.filter(stock__lte=F("min_stock"))
    forecast = build_forecast()
    products = list(products)
    for product in products:
        product.fc = forecast[product.id]
    context = {
        "products": products,
        "all_products": Product.objects.all(),
        "low_products": Product.objects.filter(stock__lte=F("min_stock")).order_by("stock"),
        "movements": StockMovement.objects.select_related("product", "created_by")[:8],
        "stock_value": sum(p.stock * p.cost_per_unit for p in Product.objects.all() if p.stock > 0),
        "error": error,
        "form": form,
        "ok": request.GET.get("ok") == "1",
        "search": search,
        "only_low": only_low,
    }
    return render(request, "inventory/stock.html", context)


@stock_only
def shopping(request):
    """Что докупить: срочные продукты с подсказкой количества и готовым текстом для поставщика."""
    rows = shopping_list()
    total = sum((r["cost"] for r in rows), Decimal(0))
    lines = [f"• {r['product'].name} — {r['suggest']} {r['product'].get_unit_display()}" for r in rows]
    share_text = _("Закупка на %(date)s:") % {"date": timezone.localdate().strftime("%d.%m")}
    share_text = "\n".join([share_text, *lines]) if lines else ""
    blocked = (
        RecipeItem.objects.filter(product__stock__lt=F("quantity"))
        .select_related("dish", "product")
        .order_by("dish__name")
    )
    blocked_dishes = {}
    for recipe in blocked:
        blocked_dishes.setdefault(recipe.dish, []).append(recipe.product)
    return render(request, "inventory/shopping.html", {
        "rows": rows,
        "total": total,
        "share_text": share_text,
        "telegram_url": "https://t.me/share/url?url=%20&text=" + quote(share_text) if share_text else "",
        "blocked_dishes": blocked_dishes.items(),
    })


@stock_only
def stock_writeoff(request):
    """Списание: порча, потери, бой. Причина обязательна."""
    error = ""
    form = {}
    if request.method == "POST":
        form = request.POST
        error, product, quantity, _cost, comment = _movement_form(request, StockMovement.Kind.WRITEOFF)
        if not error:
            StockMovement.objects.create(
                product=product,
                kind=StockMovement.Kind.WRITEOFF,
                quantity=quantity.quantize(Decimal("0.001")),
                comment=comment,
                created_by=request.user,
            )
            messages.success(request, _("Списание сохранено."))
            return redirect("stock_movements")
    return render(
        request, "inventory/writeoff.html", {"products": Product.objects.all(), "error": error, "form": form}
    )


@stock_only
def stock_movements(request):
    movements = StockMovement.objects.select_related("product", "created_by", "order")
    kind = request.GET.get("kind", "")
    product_id = request.GET.get("product", "")
    if kind in StockMovement.Kind.values:
        movements = movements.filter(kind=kind)
    if product_id.isdigit():
        movements = movements.filter(product_id=int(product_id))
    page = Paginator(movements, 40).get_page(request.GET.get("page"))
    return render(
        request,
        "inventory/movements.html",
        {"page": page, "kinds": StockMovement.Kind.choices, "kind": kind, "product_id": product_id,
         "products": Product.objects.all()},
    )


class ProductForm(StyledModelForm):
    class Meta:
        model = Product
        fields = ["name", "name_uz", "unit", "min_stock", "cost_per_unit"]
        labels = {"cost_per_unit": gettext_lazy("Цена за 1 единицу (сум)")}

    def clean_min_stock(self):
        value = self.cleaned_data["min_stock"]
        if value < 0:
            raise forms.ValidationError(_("Не может быть меньше нуля."))
        return value


@stock_only
def product_form(request, product_id=None):
    product = get_object_or_404(Product, id=product_id) if product_id else None
    form = ProductForm(request.POST or None, instance=product)
    if request.method == "POST":
        if form.is_valid():
            form.save()
            messages.success(request, _("Продукт сохранён."))
            return redirect("stock")
        messages.error(request, _("Проверьте поля формы."))
    return render(request, "inventory/product_form.html", {"form": form, "product": product})


@stock_only
def stock_count(request):
    error = ""
    products = list(Product.objects.all())
    for product in products:
        product.entered = request.POST.get(f"fact_{product.id}", "") if request.method == "POST" else ""

    if request.method == "POST":
        changes = []
        filled = 0
        for product in products:
            raw = product.entered.strip()
            if not raw:
                continue
            filled += 1
            fact = _decimal(raw)
            if fact is None or fact < 0 or fact > MAX_VALUE:
                error = _("%(name)s: введите число не меньше нуля.") % {"name": product.name}
                break
            fact = fact.quantize(Decimal("0.001"))
            diff = fact - product.stock
            if diff != 0:
                changes.append((product, fact, diff))
        if not error and filled == 0:
            error = _("Заполните хотя бы одно поле.")
        if not error:
            with transaction.atomic():
                for product, fact, diff in changes:
                    StockMovement.objects.create(
                        product=product,
                        kind=StockMovement.Kind.ADJUST,
                        quantity=diff,
                        comment=f"Инвентаризация: было {product.stock:f}, стало {fact:f}"[:200],
                        created_by=request.user,
                    )
            return redirect(f"{reverse('stock_count')}?done={len(changes)}")

    done = request.GET.get("done", "")
    context = {
        "products": products,
        "error": error,
        "done": done if done.isdigit() else "",
    }
    return render(request, "inventory/stock_count.html", context)
