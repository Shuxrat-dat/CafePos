from django.db import models


class Category(models.Model):
    name = models.CharField("Название", max_length=100)
    sort_order = models.PositiveIntegerField("Порядок", default=0)
    is_active = models.BooleanField("Активна", default=True)

    class Meta:
        verbose_name = "Категория"
        verbose_name_plural = "Категории"
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.name


class Modifier(models.Model):
    name = models.CharField("Название", max_length=100)
    price_delta = models.DecimalField("Доплата (сум)", max_digits=12, decimal_places=0, default=0)
    consumption_multiplier = models.DecimalField(
        "Множитель расхода продуктов", max_digits=5, decimal_places=2, default=1,
        help_text="1 = обычный расход, 2 = двойная порция",
    )

    class Meta:
        verbose_name = "Модификатор"
        verbose_name_plural = "Модификаторы"

    def __str__(self):
        return self.name


class Dish(models.Model):
    category = models.ForeignKey(Category, verbose_name="Категория", on_delete=models.PROTECT, related_name="dishes")
    name = models.CharField("Название", max_length=150)
    description = models.TextField("Описание", blank=True)
    price = models.DecimalField("Цена (сум)", max_digits=12, decimal_places=0)
    modifiers = models.ManyToManyField(Modifier, verbose_name="Модификаторы", blank=True)
    is_available = models.BooleanField("В продаже", default=True)

    class Meta:
        verbose_name = "Блюдо"
        verbose_name_plural = "Блюда"
        ordering = ["category", "name"]

    def __str__(self):
        return self.name