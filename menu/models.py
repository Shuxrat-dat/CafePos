from django.core.validators import FileExtensionValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from core.models import IMAGE_EXTENSIONS

# Пометки в меню для гостей: коды хранятся в блюде через запятую
ALLERGEN_CHOICES = [
    ("gluten", _("Глютен")),
    ("milk", _("Молоко")),
    ("eggs", _("Яйца")),
    ("nuts", _("Орехи")),
    ("fish", _("Рыба")),
    ("seafood", _("Морепродукты")),
    ("soy", _("Соя")),
    ("spicy", _("Острое")),
    ("vegan", _("Веган")),
]
ALLERGEN_LABELS = dict(ALLERGEN_CHOICES)


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
    class Station(models.TextChoices):
        KITCHEN = "kitchen", _("Кухня")
        HOT = "hot", _("Горячий цех")
        COLD = "cold", _("Холодный цех")
        GRILL = "grill", _("Мангал")
        BAR = "bar", _("Бар")
        PASTRY = "pastry", _("Кондитерская")

    icon = models.CharField(_("Иллюстрация"), max_length=20, choices=Icon.choices, default=Icon.PLATE)
    station = models.CharField(
        _("Цех"), max_length=20, choices=Station.choices, default=Station.KITCHEN,
        help_text=_("На экране кухни можно оставить только блюда своего цеха."),
    )
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
    sold_by_weight = models.BooleanField(
        _("Продаётся на вес или в розлив"), default=False,
        help_text=_("Цена за 1 кг или за 1 л. При заказе вводят граммы (миллилитры) или сумму, на которую берёт гость. Техкарта — расход на 1 кг или 1 л."),
    )
    class Measure(models.TextChoices):
        KG = "kg", _("Килограммы (рыба, мясо, сладости)")
        L = "l", _("Литры (разливное пиво, водка, сок)")

    measure = models.CharField(
        _("Единица"), max_length=2, choices=Measure.choices, default=Measure.KG,
        help_text=_("Только для блюд на вес или в розлив."),
    )
    pack_size = models.PositiveSmallIntegerField(
        _("Штук в пачке"), null=True, blank=True,
        help_text=_("Для сигарет: 20. Тогда на кассе можно продать пачку или поштучно; цена — за пачку."),
    )
    modifiers = models.ManyToManyField(Modifier, verbose_name=_("Модификаторы"), blank=True)
    is_available = models.BooleanField(
        _("В продаже"), default=True,
        help_text=_("Выключено — блюдо в стоп-листе: видно на кассе, но заказать нельзя."),
    )
    is_hidden = models.BooleanField(
        _("Скрыто"), default=False,
        help_text=_("Скрытое блюдо не показывается ни на кассе, ни в меню для гостей."),
    )
    allergens = models.CharField(_("Аллергены и пометки"), max_length=100, blank=True)
    prep_minutes = models.PositiveSmallIntegerField(
        _("Время приготовления, мин"), default=0,
        help_text=_("За это время блюдо должно быть готово. 0 — использовать общий порог из настроек."),
    )
    sort_order = models.PositiveIntegerField(_("Порядок"), default=0)

    class Meta:
        verbose_name = "Блюдо"
        verbose_name_plural = "Блюда"
        ordering = ["category", "sort_order", "name"]

    def __str__(self):
        return self.name

    @property
    def allergen_codes(self):
        return [code for code in self.allergens.split(",") if code in ALLERGEN_LABELS]

    @property
    def allergen_list(self):
        """Пометки для гостя: [(код, подпись)]."""
        return [(code, ALLERGEN_LABELS[code]) for code in self.allergen_codes]

    @property
    def can_order(self):
        return self.is_available and not self.is_hidden

    @property
    def sold_by_piece(self):
        """Пачка, которую можно продать и поштучно (сигареты): цена за пачку, штука — её доля."""
        return bool(self.pack_size and self.pack_size > 1 and not self.sold_by_weight)


WEEKDAY_CHOICES = [
    ("0", _("Пн")), ("1", _("Вт")), ("2", _("Ср")), ("3", _("Чт")), ("4", _("Пт")), ("5", _("Сб")), ("6", _("Вс")),
]


class Promotion(models.Model):
    """Акция или «счастливые часы»: скидка в процентах на блюда в выбранные дни и часы.
    Цена со скидкой фиксируется в позиции заказа при добавлении, поэтому конец акции не меняет уже набранные заказы."""

    name = models.CharField(_("Название"), max_length=100)
    name_uz = models.CharField(_("Название (узб.)"), max_length=100, blank=True)
    percent = models.PositiveSmallIntegerField(_("Скидка, %"))
    weekdays = models.CharField(
        _("Дни недели"), max_length=20, blank=True, default="0,1,2,3,4,5,6",
        help_text=_("Пусто — каждый день."),
    )
    start_time = models.TimeField(_("С"), null=True, blank=True)
    end_time = models.TimeField(_("До"), null=True, blank=True, help_text=_("Пусто — весь день. Можно через полночь: 22:00–02:00."))
    date_from = models.DateField(_("Действует с"), null=True, blank=True)
    date_to = models.DateField(_("Действует по"), null=True, blank=True)
    categories = models.ManyToManyField(Category, verbose_name=_("Категории"), blank=True)
    dishes = models.ManyToManyField(Dish, verbose_name=_("Блюда"), blank=True)
    is_active = models.BooleanField(_("Включена"), default=True)

    class Meta:
        verbose_name = _("Акция")
        verbose_name_plural = _("Акции")
        ordering = ["-is_active", "name"]

    def __str__(self):
        return f"{self.name} −{self.percent}%"

    def runs_at(self, moment):
        """Действует ли акция в этот момент (местное время)."""
        if not self.is_active:
            return False
        day = moment.date()
        if self.date_from and day < self.date_from or self.date_to and day > self.date_to:
            return False
        now_t = moment.time()
        start, end = self.start_time, self.end_time
        days = [d for d in self.weekdays.split(",") if d]
        weekday = moment.weekday()
        if start and end and end <= start:
            # Через полночь: после начала — сегодняшний день, до конца — день, когда акция началась (вчера)
            if now_t >= start:
                return not days or str(weekday) in days
            if now_t < end:
                return not days or str((weekday - 1) % 7) in days
            return False
        if days and str(weekday) not in days:
            return False
        if start and now_t < start or end and now_t >= end:
            return False
        return True

    def applies_to(self, dish):
        cats = {c.id for c in self.categories.all()}
        dishes = {d.id for d in self.dishes.all()}
        if not cats and not dishes:
            return True
        return dish.category_id in cats or dish.id in dishes


def best_promotion(dish, moment=None):
    """Самая выгодная для гостя действующая акция на блюдо или None."""
    from django.utils import timezone

    moment = timezone.localtime(moment) if moment else timezone.localtime()
    best = None
    for promo in Promotion.objects.filter(is_active=True).prefetch_related("categories", "dishes"):
        if promo.runs_at(moment) and promo.applies_to(dish) and (best is None or promo.percent > best.percent):
            best = promo
    return best


def promo_price(price, percent):
    """Цена со скидкой, округлённая до целых сумов."""
    from decimal import ROUND_HALF_UP, Decimal

    return (Decimal(price) * (100 - percent) / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)


class ComboItem(models.Model):
    """Состав комбо-набора: набор — обычное блюдо со своей ценой, в него входят другие блюда.
    Кухня видит состав, склад списывает продукты по техкартам входящих блюд."""

    combo = models.ForeignKey(Dish, verbose_name=_("Набор"), on_delete=models.CASCADE, related_name="combo_items")
    component = models.ForeignKey(Dish, verbose_name=_("Блюдо в наборе"), on_delete=models.PROTECT, related_name="+")
    quantity = models.PositiveSmallIntegerField(_("Количество"), default=1)

    class Meta:
        verbose_name = _("Блюдо в наборе")
        verbose_name_plural = _("Состав набора")
        unique_together = ("combo", "component")

    def __str__(self):
        return f"{self.combo.name}: {self.component.name} ×{self.quantity}"
