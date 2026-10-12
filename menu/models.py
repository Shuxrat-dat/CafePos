from django.core.validators import FileExtensionValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from core.models import IMAGE_EXTENSIONS


class Category(models.Model):
    class Icon(models.TextChoices):
        BOWL = "bowl", _("Национальные блюда")
        PLATE = "plate", _("Горячее")
        BURGER = "burger", _("Фастфуд")
        PIZZA = "pizza", _("Пицца")
        SALAD = "salad", _("Салаты")
        CAKE = "cake", _("Десерты")
        COFFEE = "coffee", _("Кофе")
        DRINK = "drink", _("Напитки")

    name = models.CharField(_("Название"), max_length=100)
    name_uz = models.CharField(_("Название (узб.)"), max_length=100, blank=True)
    icon = models.CharField(_("Иллюстрация"), max_length=20, choices=Icon.choices, default=Icon.PLATE)
    sort_order = models.PositiveIntegerField(_("Порядок"), default=0)
    is_active = models.BooleanField(_("Активна"), default=True)

    class Meta:
        verbose_name = "Категория"
        verbose_name_plural = "Категории"
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.name


class Modifier(models.Model):
    name = models.CharField(_("Название"), max_length=100)
    name_uz = models.CharField(_("Название (узб.)"), max_length=100, blank=True)
    group = models.CharField(
        _("Группа выбора"), max_length=50, blank=True,
        help_text=_("Модификаторы с одинаковой группой взаимоисключающие (например, «Размер»). Пусто — добавка."),
    )
    price_delta = models.DecimalField(_("Доплата (сум)"), max_digits=12, decimal_places=0, default=0)
    consumption_multiplier = models.DecimalField(
        _("Множитель расхода продуктов"), max_digits=5, decimal_places=2, default=1,
        help_text=_("1 = обычный расход, 2 = двойная порция"),
    )

    class Meta:
        verbose_name = "Модификатор"
        verbose_name_plural = "Модификаторы"
        ordering = ["group", "price_delta", "name"]

    def __str__(self):
        return f"{self.group}: {self.name}" if self.group else self.name


class Dish(models.Model):
    category = models.ForeignKey(Category, verbose_name=_("Категория"), on_delete=models.PROTECT, related_name="dishes")
    name = models.CharField(_("Название"), max_length=150)
    name_uz = models.CharField(_("Название (узб.)"), max_length=150, blank=True)
    description = models.TextField(_("Описание"), blank=True)
    description_uz = models.TextField(_("Описание (узб.)"), blank=True)
    photo = models.FileField(
        _("Фото"), upload_to="dishes/", blank=True,
        validators=[FileExtensionValidator(IMAGE_EXTENSIONS)],
    )
    price = models.DecimalField(_("Цена (сум)"), max_digits=12, decimal_places=0)
    modifiers = models.ManyToManyField(Modifier, verbose_name=_("Модификаторы"), blank=True)
    is_available = models.BooleanField(
        _("В продаже"), default=True,
        help_text=_("Выключено — блюдо в стоп-листе: видно на кассе, но заказать нельзя."),
    )
    is_hidden = models.BooleanField(
        _("Скрыто"), default=False,
        help_text=_("Скрытое блюдо не показывается ни на кассе, ни в меню для гостей."),
    )
    sort_order = models.PositiveIntegerField(_("Порядок"), default=0)

    class Meta:
        verbose_name = "Блюдо"
        verbose_name_plural = "Блюда"
        ordering = ["category", "sort_order", "name"]

    def __str__(self):
        return self.name

    @property
    def can_order(self):
        return self.is_available and not self.is_hidden
