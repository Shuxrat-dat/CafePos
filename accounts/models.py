from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .permissions import CANCEL_SENT_ROLES, PAYMENT_ROLES, PIN_CANCEL_ROLES, SEE_ALL_ROLES


class User(AbstractUser):
    class Role(models.TextChoices):
        OWNER = "owner", _("Владелец")
        ADMIN = "admin", _("Администратор")
        WAITER = "waiter", _("Официант")
        CASHIER = "cashier", _("Кассир")
        COOK = "cook", _("Повар")

    role = models.CharField("Роль", max_length=20, choices=Role.choices, default=Role.WAITER)
    # Пароль отмены: сотрудник задаёт его сам, хранится только хеш. См. accounts/cancel_pin.py
    cancel_pin_hash = models.CharField("Хеш пароля отмены", max_length=128, blank=True)
    cancel_pin_failures = models.PositiveSmallIntegerField("Неверных попыток подряд", default=0)
    cancel_pin_locked_until = models.DateTimeField("Пароль отмены заблокирован до", null=True, blank=True)

    @property
    def needs_cancel_pin(self):
        """Нужен ли этому сотруднику пароль отмены (владельцу и администратору не нужен)."""
        return not self.is_superuser and self.role in PIN_CANCEL_ROLES

    @property
    def has_cancel_pin(self):
        return bool(self.cancel_pin_hash)

    @property
    def cancel_pin_is_locked(self):
        return bool(self.cancel_pin_locked_until) and self.cancel_pin_locked_until > timezone.now()

    @property
    def can_take_payment(self):
        return self.is_superuser or self.role in PAYMENT_ROLES

    @property
    def sees_all_orders(self):
        return self.is_superuser or self.role in SEE_ALL_ROLES

    @property
    def can_cancel_sent_orders(self):
        if self.is_superuser or self.role in CANCEL_SENT_ROLES:
            return True
        # Кассир может отменять и то, что уже на кухне, но только имея свой пароль отмены
        return self.needs_cancel_pin and self.has_cancel_pin