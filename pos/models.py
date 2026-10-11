from django.conf import settings
from django.db import models

from menu.models import Dish, Modifier


class Hall(models.Model):
    name = models.CharField("Название", max_length=100)

    class Meta:
        verbose_name = "Зал"
        verbose_name_plural = "Залы"

    def __str__(self):
        return self.name


class Table(models.Model):
    hall = models.ForeignKey(Hall, verbose_name="Зал", on_delete=models.CASCADE, related_name="tables")
    number = models.CharField("Номер или название", max_length=20)
    seats = models.PositiveSmallIntegerField("Мест", default=4)

    class Meta:
        verbose_name = "Стол"
        verbose_name_plural = "Столы"
        unique_together = ("hall", "number")
        ordering = ["hall", "number"]

    def __str__(self):
        return f"{self.hall.name}, стол {self.number}"


class Order(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "Открыт"
        PAID = "paid", "Оплачен"
        CANCELLED = "cancelled", "Отменён"

    class PaymentMethod(models.TextChoices):
        CASH = "cash", "Наличные"
        CARD = "card", "Карта"

    table = models.ForeignKey(Table, verbose_name="Стол", on_delete=models.PROTECT, related_name="orders")
    waiter = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="Официант", on_delete=models.PROTECT, related_name="orders"
    )
    status = models.CharField("Статус", max_length=20, choices=Status.choices, default=Status.OPEN)
    payment_method = models.CharField("Способ оплаты", max_length=10, choices=PaymentMethod.choices, blank=True)
    cash_received = models.DecimalField("Получено наличными", max_digits=12, decimal_places=0, null=True, blank=True)
    created_at = models.DateTimeField("Создан", auto_now_add=True)
    closed_at = models.DateTimeField("Закрыт", null=True, blank=True)

    class Meta:
        verbose_name = "Заказ"
        verbose_name_plural = "Заказы"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Заказ №{self.pk}"

    @property
    def total(self):
        return sum(item.line_total for item in self.items.all())

    @property
    def has_new_items(self):
        return self.items.filter(status="new").exists()

    @property
    def has_sent_items(self):
        return self.items.exclude(status="new").exists()

    @property
    def can_pay(self):
        return self.items.exists() and not self.has_new_items
    @property
    def pending_guests(self):
        return self.table.guest_orders.filter(status="pending").prefetch_related("items__dish")


class OrderItem(models.Model):
    class Status(models.TextChoices):
        NEW = "new", "Новый"
        COOKING = "cooking", "Готовится"
        READY = "ready", "Готово"
        SERVED = "served", "Подано"

    order = models.ForeignKey(Order, verbose_name="Заказ", on_delete=models.CASCADE, related_name="items")
    dish = models.ForeignKey(Dish, verbose_name="Блюдо", on_delete=models.PROTECT)
    quantity = models.PositiveSmallIntegerField("Количество", default=1)
    price = models.DecimalField("Цена на момент заказа", max_digits=12, decimal_places=0, null=True, blank=True)
    modifiers = models.ManyToManyField(Modifier, verbose_name="Модификаторы", blank=True)
    comment = models.CharField("Комментарий", max_length=200, blank=True)
    status = models.CharField("Статус", max_length=20, choices=Status.choices, default=Status.NEW)

    class Meta:
        verbose_name = "Позиция заказа"
        verbose_name_plural = "Позиции заказа"

    def __str__(self):
        return f"{self.dish.name} x{self.quantity}"

    def save(self, *args, **kwargs):
        # Фиксируем цену блюда на момент заказа, чтобы старые заказы не менялись при смене цен в меню
        if self.price is None:
            self.price = self.dish.price
        super().save(*args, **kwargs)

    @property
    def unit_total(self):
        """Цена одной штуки с учётом доплат за модификаторы."""
        return self.price + sum(m.price_delta for m in self.modifiers.all())

    @property
    def line_total(self):
        return self.unit_total * self.quantity

class GuestOrder(models.Model):
    """Заявка гостя с телефона. Становится заказом только после подтверждения официантом."""

    class Status(models.TextChoices):
        PENDING = "pending", "Ожидает подтверждения"
        ACCEPTED = "accepted", "Принята"
        REJECTED = "rejected", "Отклонена"

    table = models.ForeignKey(Table, verbose_name="Стол", on_delete=models.CASCADE, related_name="guest_orders")
    status = models.CharField("Статус", max_length=20, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField("Создана", auto_now_add=True)

    class Meta:
        verbose_name = "Заявка гостя"
        verbose_name_plural = "Заявки гостей"
        ordering = ["created_at"]

    def __str__(self):
        return f"Заявка №{self.pk} ({self.table})"


class GuestOrderItem(models.Model):
    guest_order = models.ForeignKey(GuestOrder, verbose_name="Заявка", on_delete=models.CASCADE, related_name="items")
    dish = models.ForeignKey(Dish, verbose_name="Блюдо", on_delete=models.PROTECT)
    quantity = models.PositiveSmallIntegerField("Количество", default=1)

    class Meta:
        verbose_name = "Позиция заявки"
        verbose_name_plural = "Позиции заявки"

    def __str__(self):
        return f"{self.dish.name} x{self.quantity}"