from django.conf import settings
from django.db import models, transaction
from django.db.models import F
from django.utils.translation import gettext_lazy as _

from menu.models import Dish


class Product(models.Model):
    class Unit(models.TextChoices):
        GRAM = "g", _("г")
        ML = "ml", _("мл")
        PIECE = "pcs", _("шт")

    name = models.CharField(_("Название"), max_length=150, unique=True)
    name_uz = models.CharField(_("Название (узб.)"), max_length=150, blank=True)
    unit = models.CharField(_("Единица"), max_length=5, choices=Unit.choices, default=Unit.GRAM)
    stock = models.DecimalField(_("Остаток"), max_digits=14, decimal_places=3, default=0, editable=False)
    min_stock = models.DecimalField(_("Минимальный остаток"), max_digits=14, decimal_places=3, default=0)
    cost_per_unit = models.DecimalField(
        _("Цена за 1 единицу (сум)"), max_digits=12, decimal_places=2, default=0,
        help_text=_("Например, говядина 80 000 сум за кг это 80 сум за 1 г"),
    )

    class Meta:
        verbose_name = _("Продукт")
        verbose_name_plural = _("Продукты")
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.get_unit_display()})"

    @property
    def is_low(self):
        return self.stock <= self.min_stock


class RecipeItem(models.Model):
    dish = models.ForeignKey(Dish, verbose_name=_("Блюдо"), on_delete=models.CASCADE, related_name="recipe")
    product = models.ForeignKey(Product, verbose_name=_("Продукт"), on_delete=models.PROTECT)
    quantity = models.DecimalField(_("Расход на порцию"), max_digits=12, decimal_places=3)

    class Meta:
        verbose_name = _("Ингредиент")
        verbose_name_plural = _("Техкарта")
        unique_together = ("dish", "product")

    def __str__(self):
        return f"{self.dish.name}: {self.product.name} {self.quantity}"


class StockMovement(models.Model):
    class Kind(models.TextChoices):
        INCOME = "income", _("Приход")
        WRITEOFF = "writeoff", _("Списание (порча, потери)")
        SALE = "sale", _("Продажа")
        ADJUST = "adjust", _("Корректировка (инвентаризация)")

    product = models.ForeignKey(Product, verbose_name=_("Продукт"), on_delete=models.PROTECT, related_name="movements")
    kind = models.CharField(_("Тип"), max_length=10, choices=Kind.choices)
    quantity = models.DecimalField(
        _("Количество"), max_digits=14, decimal_places=3,
        help_text=_("Для прихода и списания вводите положительное число. Для корректировки: плюс добавляет, минус убавляет"),
    )
    order = models.ForeignKey("pos.Order", verbose_name=_("Заказ"), null=True, blank=True, on_delete=models.SET_NULL)
    comment = models.CharField(_("Комментарий"), max_length=200, blank=True)
    created_at = models.DateTimeField(_("Дата"), auto_now_add=True)
    unit_cost = models.DecimalField(
        _("Цена за единицу на момент движения"), max_digits=12, decimal_places=2,
        null=True, blank=True, editable=False,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name=_("Кто"), null=True, blank=True, on_delete=models.SET_NULL
    )

    class Meta:
        verbose_name = _("Движение склада")
        verbose_name_plural = _("Движения склада")
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_kind_display()}: {self.product.name} {self.quantity}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            return super().save(*args, **kwargs)
        # Приводим знак к нужному и меняем остаток продукта одной транзакцией
        if self.kind == self.Kind.INCOME:
            self.quantity = abs(self.quantity)
        elif self.kind in (self.Kind.WRITEOFF, self.Kind.SALE):
            self.quantity = -abs(self.quantity)
        if self.unit_cost is None:
            self.unit_cost = self.product.cost_per_unit
        with transaction.atomic():
            super().save(*args, **kwargs)
            list(Product.objects.filter(pk=self.product_id).select_for_update())
            Product.objects.filter(pk=self.product_id).update(stock=F("stock") + self.quantity)
