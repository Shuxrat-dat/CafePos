from django.conf import settings
from django.core.validators import FileExtensionValidator, MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

# SVG не принимаем: в нём может быть скрипт, а файлы отдаются с того же адреса
IMAGE_EXTENSIONS = ["jpg", "jpeg", "png", "webp"]


def default_language():
    return settings.LANGUAGE_CODE


class AuditLog(models.Model):
    """Кто и что сделал в системе. Записи только добавляются, из интерфейса их не изменить и не удалить."""

    class Action(models.TextChoices):
        ORDER_CANCEL = "order_cancel", _("Отмена заказа")
        ITEM_REMOVE = "item_remove", _("Удалено блюдо с кухни")
        DISCOUNT = "discount", _("Скидка")
        REFUND = "refund", _("Возврат")
        SHIFT_OPEN = "shift_open", _("Открытие смены")
        SHIFT_CLOSE = "shift_close", _("Закрытие смены")
        CASH_MOVE = "cash_move", _("Внесение или изъятие наличных")
        STOCK_WRITEOFF = "stock_writeoff", _("Списание продуктов")
        STOCK_COUNT = "stock_count", _("Инвентаризация")
        PRICE_CHANGE = "price_change", _("Изменение цены")
        LOGIN_FAILED = "login_failed", _("Неудачный вход")

    created_at = models.DateTimeField(_("Время"), auto_now_add=True, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name=_("Сотрудник"), null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+",
    )
    user_name = models.CharField(_("Имя сотрудника"), max_length=150, blank=True)
    action = models.CharField(_("Действие"), max_length=20, choices=Action.choices, db_index=True)
    order_id = models.PositiveIntegerField(_("Заказ"), null=True, blank=True)
    amount = models.DecimalField(_("Сумма"), max_digits=14, decimal_places=0, null=True, blank=True)
    detail = models.CharField(_("Подробности"), max_length=300, blank=True)

    class Meta:
        verbose_name = "Запись журнала"
        verbose_name_plural = "Журнал действий"
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.created_at:%d.%m %H:%M} {self.user_name} {self.get_action_display()}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise PermissionError("Записи журнала не изменяются.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionError("Записи журнала не удаляются.")


class CafeSettings(models.Model):
    """Настройки заведения. В базе всегда одна запись, берите её через CafeSettings.get()."""

    class Language(models.TextChoices):
        UZ = "uz", "Oʻzbekcha"
        RU = "ru", "Русский"

    name = models.CharField(_("Название заведения"), max_length=100, default="CafePOS")
    logo = models.FileField(
        _("Логотип"), upload_to="logo/", blank=True,
        validators=[FileExtensionValidator(IMAGE_EXTENSIONS)],
    )
    address = models.CharField(_("Адрес"), max_length=200, blank=True)
    phone = models.CharField(_("Телефон"), max_length=50, blank=True)
    default_language = models.CharField(
        _("Язык по умолчанию"), max_length=5, choices=Language.choices, default=default_language
    )
    service_percent = models.DecimalField(
        _("Обслуживание, %"), max_digits=5, decimal_places=2, default=0,
        validators=[MinValueValidator(0), MaxValueValidator(30)],
        help_text=_("Добавляется к заказам в зале. 0 — не начислять."),
    )
    vat_percent = models.DecimalField(
        _("НДС в цене, %"), max_digits=5, decimal_places=2, default=0,
        validators=[MinValueValidator(0), MaxValueValidator(30)],
        help_text=_("Только для строки «в т.ч. НДС» в чеке. Цены уже включают налог."),
    )
    receipt_header = models.CharField(_("Текст в начале чека"), max_length=200, blank=True)
    receipt_footer = models.CharField(_("Текст в конце чека"), max_length=200, blank=True)
    accept_cash = models.BooleanField(_("Наличные"), default=True)
    accept_card = models.BooleanField(_("Банковская карта"), default=True)
    accept_mixed = models.BooleanField(_("Смешанная оплата"), default=True)
    kitchen_warn_minutes = models.PositiveSmallIntegerField(
        _("Предупреждать кухню через, мин"), default=15,
        validators=[MinValueValidator(1), MaxValueValidator(180)],
    )

    telegram_bot_token = models.CharField(_("Токен Telegram-бота"), max_length=100, blank=True)
    telegram_chat_id = models.CharField(_("Чат для уведомлений"), max_length=40, blank=True)
    tg_notify_shift = models.BooleanField(_("Закрытие смены"), default=True)
    tg_notify_cancel = models.BooleanField(_("Отмена заказов"), default=True)
    tg_notify_discount = models.BooleanField(_("Крупные скидки"), default=True)
    tg_notify_stock = models.BooleanField(_("Заканчиваются продукты"), default=True)
    tg_discount_percent = models.PositiveSmallIntegerField(
        _("Скидка считается крупной от, %"), default=20,
        validators=[MinValueValidator(1), MaxValueValidator(100)],
    )

    class Meta:
        verbose_name = "Настройки заведения"
        verbose_name_plural = "Настройки заведения"

    def __str__(self):
        return self.name

    @classmethod
    def get(cls):
        obj = cls.objects.order_by("id").first()
        if obj is None:
            obj = cls.objects.create()
        return obj

    @property
    def payment_methods(self):
        methods = []
        if self.accept_cash:
            methods.append("cash")
        if self.accept_card:
            methods.append("card")
        if self.accept_mixed:
            methods.append("mixed")
        return methods or ["cash"]
