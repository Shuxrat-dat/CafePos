from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils.translation import gettext_lazy as _

from .permissions import CANCEL_SENT_ROLES, PAYMENT_ROLES, SEE_ALL_ROLES


class User(AbstractUser):
    class Role(models.TextChoices):
        OWNER = "owner", _("Владелец")
        ADMIN = "admin", _("Администратор")
        WAITER = "waiter", _("Официант")
        CASHIER = "cashier", _("Кассир")
        COOK = "cook", _("Повар")

    role = models.CharField("Роль", max_length=20, choices=Role.choices, default=Role.WAITER)

    @property
    def can_take_payment(self):
        return self.is_superuser or self.role in PAYMENT_ROLES

    @property
    def sees_all_orders(self):
        return self.is_superuser or self.role in SEE_ALL_ROLES

    @property
    def can_cancel_sent_orders(self):
        return self.is_superuser or self.role in CANCEL_SENT_ROLES