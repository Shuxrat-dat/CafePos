from django.db import models


class JumaState(models.Model):
    """Единственная строка: где остановилась синхронизация с Juma и в каком состоянии связь.
    Фоновый процесс juma_sync и страница «Juma» на кассе читают и пишут её."""

    catalog_version = models.BigIntegerField("Версия каталога", default=0)
    catalog_hash = models.CharField("Хеш отправленного каталога", max_length=64, blank=True)
    events_cursor = models.BigIntegerField("Курсор событий", default=0)
    accepting_orders = models.BooleanField("Принимаем заказы Juma", default=True)
    delivery_enabled = models.BooleanField("Доставка включена в Juma", default=False)
    last_heartbeat_at = models.DateTimeField("Последняя связь", null=True, blank=True)
    last_catalog_at = models.DateTimeField("Последняя отправка каталога", null=True, blank=True)
    last_error = models.CharField("Последняя ошибка", max_length=255, blank=True)

    class Meta:
        verbose_name = "Состояние Juma"
        verbose_name_plural = "Состояние Juma"

    @classmethod
    def get(cls):
        obj, _created = cls.objects.get_or_create(pk=1)
        return obj


class JumaOrder(models.Model):
    """Связка заказа Juma с заказом на кассе. Сам заказ — обычный Order типа «Доставка»; здесь то, что нужно только Juma:
    её идентификатор, номер для клиента, код выдачи водителю и статусы по версии облака."""

    class Status(models.TextChoices):
        PLACED = "PLACED", "Новый"
        SENT = "SENT", "Принят кассой"
        ACCEPTED = "ACCEPTED", "Подтверждён заведением"
        PREPARING = "PREPARING", "Готовится"
        READY = "READY", "Готов"
        PICKED_UP = "PICKED_UP", "Передан водителю"
        DELIVERED = "DELIVERED", "Доставлен"
        CANCELLED = "CANCELLED", "Отменён"

    class Payment(models.TextChoices):
        WAITING_TRANSFER = "WAITING_TRANSFER", "Ждём перевод"
        AWAITING_PAYMENT = "AWAITING_PAYMENT", "Ждём оплату"
        PAID = "PAID", "Оплачен"
        REFUND_PENDING = "REFUND_PENDING", "Нужен возврат"
        REFUNDED = "REFUNDED", "Возвращён"

    order = models.OneToOneField("pos.Order", verbose_name="Заказ", on_delete=models.PROTECT, related_name="juma")
    juma_id = models.UUIDField("Идентификатор в Juma", unique=True)
    number = models.CharField("Номер", max_length=20)
    pickup_code = models.CharField("Код выдачи", max_length=4)
    status = models.CharField("Статус в Juma", max_length=20, choices=Status.choices, default=Status.PLACED)
    payment_status = models.CharField("Оплата в Juma", max_length=20, choices=Payment.choices, default=Payment.WAITING_TRANSFER)
    items_total = models.DecimalField("Сумма за товар", max_digits=14, decimal_places=2)
    cancel_reason = models.CharField("Причина отмены", max_length=100, blank=True)
    created_at = models.DateTimeField("Получен", auto_now_add=True)
    accepted_at = models.DateTimeField("Принят", null=True, blank=True)

    class Meta:
        verbose_name = "Заказ Juma"
        verbose_name_plural = "Заказы Juma"
        ordering = ["-created_at"]

    def __str__(self):
        return self.number
