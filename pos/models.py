from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from menu.models import Dish, Modifier


def round_sum(value):
    """Суммы в сумах всегда целые, округляем по правилам арифметики."""
    return Decimal(value).quantize(Decimal("1"), rounding=ROUND_HALF_UP)


class Hall(models.Model):
    name = models.CharField(_("Название"), max_length=100)
    name_uz = models.CharField(_("Название (узб.)"), max_length=100, blank=True)
    sort_order = models.PositiveIntegerField(_("Порядок"), default=0)

    class Meta:
        verbose_name = "Зал"
        verbose_name_plural = "Залы"
        ordering = ["sort_order", "id"]

    def __str__(self):
        return self.name


class Table(models.Model):
    hall = models.ForeignKey(Hall, verbose_name=_("Зал"), on_delete=models.CASCADE, related_name="tables")
    number = models.CharField(_("Номер или название"), max_length=20)
    seats = models.PositiveSmallIntegerField(_("Мест"), default=4)
    reserved_for = models.CharField(_("Бронь на имя"), max_length=100, blank=True)
    reserved_at = models.DateTimeField(_("Время брони"), null=True, blank=True)

    class Meta:
        verbose_name = "Стол"
        verbose_name_plural = "Столы"
        unique_together = ("hall", "number")
        ordering = ["hall", "number"]

    def __str__(self):
        return f"{self.hall.name}, стол {self.number}"

    @property
    def is_reserved(self):
        return bool(self.reserved_for or self.reserved_at)


class Order(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", _("Открыт")
        PAID = "paid", _("Оплачен")
        CANCELLED = "cancelled", _("Отменён")

    class OrderType(models.TextChoices):
        DINE_IN = "dine_in", _("В зале")
        TAKEAWAY = "takeaway", _("Навынос")
        DELIVERY = "delivery", _("Доставка")

    class PaymentMethod(models.TextChoices):
        CASH = "cash", _("Наличные")
        CARD = "card", _("Карта")
        MIXED = "mixed", _("Смешанная")

    class DiscountKind(models.TextChoices):
        PERCENT = "percent", _("Процент")
        AMOUNT = "amount", _("Сумма")

    # Этапы заказа для экранов: считаются по позициям, в базе не хранятся
    STAGES = {
        "new": _("Новый"),
        "cooking": _("Готовится"),
        "ready": _("Готов"),
        "served": _("Подан"),
        "paid": _("Оплачен"),
        "cancelled": _("Отменён"),
    }

    order_type = models.CharField(_("Тип заказа"), max_length=10, choices=OrderType.choices, default=OrderType.DINE_IN)
    table = models.ForeignKey(
        Table, verbose_name=_("Стол"), on_delete=models.PROTECT, related_name="orders", null=True, blank=True
    )
    waiter = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name=_("Официант"), on_delete=models.PROTECT, related_name="orders"
    )
    status = models.CharField(_("Статус"), max_length=20, choices=Status.choices, default=Status.OPEN)
    customer_name = models.CharField(_("Имя клиента"), max_length=100, blank=True)
    customer_phone = models.CharField(_("Телефон клиента"), max_length=30, blank=True)
    delivery_address = models.CharField(_("Адрес доставки"), max_length=255, blank=True)
    note = models.CharField(_("Комментарий к заказу"), max_length=255, blank=True)

    discount_kind = models.CharField(_("Вид скидки"), max_length=10, choices=DiscountKind.choices, blank=True)
    discount_value = models.DecimalField(_("Размер скидки"), max_digits=12, decimal_places=2, default=0)
    discount_reason = models.CharField(_("Причина скидки"), max_length=100, blank=True)
    service_percent = models.DecimalField(_("Обслуживание, %"), max_digits=5, decimal_places=2, default=0)

    payment_method = models.CharField(_("Способ оплаты"), max_length=10, choices=PaymentMethod.choices, blank=True)
    cash_received = models.DecimalField(_("Получено наличными"), max_digits=12, decimal_places=0, null=True, blank=True)
    card_amount = models.DecimalField(_("Оплачено картой"), max_digits=12, decimal_places=0, null=True, blank=True)
    cashier = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name=_("Кассир"), on_delete=models.SET_NULL,
        null=True, blank=True, related_name="cashed_orders",
    )

    bill_requested_at = models.DateTimeField(_("Пречек выдан"), null=True, blank=True)
    cancel_reason = models.CharField(_("Причина отмены"), max_length=200, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name=_("Кто отменил"), on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    created_at = models.DateTimeField(_("Создан"), auto_now_add=True)
    closed_at = models.DateTimeField(_("Закрыт"), null=True, blank=True)

    class Meta:
        verbose_name = "Заказ"
        verbose_name_plural = "Заказы"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "closed_at"])]

    def __str__(self):
        return f"Заказ №{self.pk}"

    # ---------- суммы ----------

    @property
    def subtotal(self):
        return sum((item.line_total for item in self.items.all()), Decimal(0))

    def discount_for(self, subtotal):
        if self.discount_kind == self.DiscountKind.PERCENT:
            return min(subtotal, round_sum(subtotal * self.discount_value / 100))
        if self.discount_kind == self.DiscountKind.AMOUNT:
            return min(subtotal, round_sum(self.discount_value))
        return Decimal(0)

    def service_for(self, subtotal):
        base = subtotal - self.discount_for(subtotal)
        return round_sum(base * self.service_percent / 100) if self.service_percent else Decimal(0)

    def totals_for(self, subtotal):
        """Подытог, скидка, обслуживание и итог для заданной суммы позиций."""
        discount = self.discount_for(subtotal)
        service = self.service_for(subtotal)
        return {"subtotal": subtotal, "discount": discount, "service": service, "total": subtotal - discount + service}

    @property
    def totals(self):
        return self.totals_for(self.subtotal)

    @property
    def total(self):
        return self.totals["total"]

    @property
    def cash_amount(self):
        """Сколько из итога оплачено наличными (без сдачи)."""
        if self.payment_method == self.PaymentMethod.CASH:
            return self.total
        if self.payment_method == self.PaymentMethod.MIXED:
            return self.total - (self.card_amount or 0)
        return Decimal(0)

    @property
    def card_paid(self):
        if self.payment_method == self.PaymentMethod.CARD:
            return self.total
        if self.payment_method == self.PaymentMethod.MIXED:
            return self.card_amount or Decimal(0)
        return Decimal(0)

    @property
    def change(self):
        if self.cash_received is None or self.payment_method not in (self.PaymentMethod.CASH, self.PaymentMethod.MIXED):
            return None
        return self.cash_received - self.cash_amount

    # ---------- состояние ----------

    @property
    def has_new_items(self):
        return any(item.status == OrderItem.Status.NEW for item in self.items.all())

    @property
    def has_sent_items(self):
        return any(item.status != OrderItem.Status.NEW for item in self.items.all())

    @property
    def has_ready_items(self):
        return any(item.status == OrderItem.Status.READY for item in self.items.all())

    @property
    def can_pay(self):
        return self.status == self.Status.OPEN and bool(self.items.all())

    @property
    def stage(self):
        """Этап заказа: new, cooking, ready, served, paid, cancelled."""
        if self.status == self.Status.CANCELLED:
            return "cancelled"
        statuses = {item.status for item in self.items.all()}
        if self.status == self.Status.PAID and statuses <= {OrderItem.Status.SERVED}:
            return "paid"
        if not statuses or OrderItem.Status.NEW in statuses or OrderItem.Status.QUEUED in statuses:
            return "new"
        if OrderItem.Status.COOKING in statuses:
            return "cooking"
        if OrderItem.Status.READY in statuses:
            return "ready"
        return "served"

    @property
    def stage_label(self):
        return self.STAGES[self.stage]

    @property
    def is_dine_in(self):
        return self.order_type == self.OrderType.DINE_IN

    @property
    def place_label(self):
        """Где заказ: стол или тип (навынос/доставка)."""
        if self.is_dine_in and self.table_id:
            return _("Стол %(n)s") % {"n": self.table.number}
        return self.get_order_type_display()

    @property
    def awaiting_payment(self):
        return self.status == self.Status.OPEN and self.bill_requested_at is not None

    @property
    def pending_guests(self):
        if not self.table_id:
            return GuestOrder.objects.none()
        return self.table.guest_orders.filter(status="pending").prefetch_related("items__dish")


class OrderItem(models.Model):
    class Status(models.TextChoices):
        NEW = "new", _("Не отправлено")
        QUEUED = "queued", _("В очереди")
        COOKING = "cooking", _("Готовится")
        READY = "ready", _("Готово")
        SERVED = "served", _("Подано")

    order = models.ForeignKey(Order, verbose_name=_("Заказ"), on_delete=models.CASCADE, related_name="items")
    dish = models.ForeignKey(Dish, verbose_name=_("Блюдо"), on_delete=models.PROTECT)
    quantity = models.PositiveSmallIntegerField(_("Количество"), default=1)
    price = models.DecimalField(_("Цена на момент заказа"), max_digits=12, decimal_places=0, null=True, blank=True)
    modifiers = models.ManyToManyField(Modifier, verbose_name=_("Модификаторы"), blank=True)
    comment = models.CharField(_("Комментарий"), max_length=200, blank=True)
    status = models.CharField(_("Статус"), max_length=20, choices=Status.choices, default=Status.NEW)
    sent_at = models.DateTimeField(_("Отправлено на кухню"), null=True, blank=True)
    ready_at = models.DateTimeField(_("Готово в"), null=True, blank=True)

    class Meta:
        verbose_name = "Позиция заказа"
        verbose_name_plural = "Позиции заказа"
        ordering = ["id"]

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
        PENDING = "pending", _("Ожидает подтверждения")
        ACCEPTED = "accepted", _("Принята")
        REJECTED = "rejected", _("Отклонена")

    table = models.ForeignKey(Table, verbose_name=_("Стол"), on_delete=models.CASCADE, related_name="guest_orders")
    status = models.CharField(_("Статус"), max_length=20, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(_("Создана"), auto_now_add=True)

    class Meta:
        verbose_name = "Заявка гостя"
        verbose_name_plural = "Заявки гостей"
        ordering = ["created_at"]

    def __str__(self):
        return f"Заявка №{self.pk} ({self.table})"


class GuestOrderItem(models.Model):
    guest_order = models.ForeignKey(GuestOrder, verbose_name=_("Заявка"), on_delete=models.CASCADE, related_name="items")
    dish = models.ForeignKey(Dish, verbose_name=_("Блюдо"), on_delete=models.PROTECT)
    quantity = models.PositiveSmallIntegerField(_("Количество"), default=1)

    class Meta:
        verbose_name = "Позиция заявки"
        verbose_name_plural = "Позиции заявки"

    def __str__(self):
        return f"{self.dish.name} x{self.quantity}"
