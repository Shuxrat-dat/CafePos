from decimal import Decimal, InvalidOperation

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import redirect, render
from django.urls import reverse

from accounts.permissions import STOCK_ROLES

from .models import Product, StockMovement

MAX_VALUE = Decimal(10_000_000)


def stock_only(view):
    @login_required
    def wrapper(request, *args, **kwargs):
        if not (request.user.is_superuser or request.user.role in STOCK_ROLES):
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return wrapper


def _decimal(raw):
    """Число из текста (запятая тоже подходит) или None, если это не число."""
    try:
        value = Decimal(raw.strip().replace(",", ".").replace(" ", ""))
    except (InvalidOperation, AttributeError):
        return None
    return value if value.is_finite() else None


@stock_only
def stock(request):
    error = ""
    form = {}

    if request.method == "POST":
        form = request.POST
        pid = request.POST.get("product", "")
        product = Product.objects.filter(id=pid).first() if pid.isdigit() else None
        quantity = _decimal(request.POST.get("quantity", ""))
        cost_raw = request.POST.get("cost", "").strip()
        cost = _decimal(cost_raw) if cost_raw else None
        comment = request.POST.get("comment", "").strip()[:200]

        if product is None:
            error = "Выберите продукт."
        elif quantity is None or quantity <= 0 or quantity > MAX_VALUE:
            error = "Введите количество числом больше нуля."
        elif cost_raw and (cost is None or cost < 0 or cost > MAX_VALUE):
            error = "Цена должна быть числом не меньше нуля."
        else:
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

    context = {
        "products": Product.objects.all(),
        "movements": StockMovement.objects.select_related("product", "created_by")[:10],
        "error": error,
        "form": form,
        "ok": request.GET.get("ok") == "1",
    }
    return render(request, "inventory/stock.html", context)

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
                error = f"{product.name}: введите число не меньше нуля."
                break
            fact = fact.quantize(Decimal("0.001"))
            diff = fact - product.stock
            if diff != 0:
                changes.append((product, fact, diff))
        if not error and filled == 0:
            error = "Заполните хотя бы одно поле."
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