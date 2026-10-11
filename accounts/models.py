from django.contrib.auth.models import AbstractUser
from django.db import models

from .permissions import CANCEL_SENT_ROLES, PAYMENT_ROLES, SEE_ALL_ROLES


class User(AbstractUser):
    class Role(models.TextChoices):
        OWNER = "owner", "Владелец"
        ADMIN = "admin", "Администратор"
        WAITER = "waiter", "Официант"
        CASHIER = "cashier", "Кассир"
        COOK = "cook", "Повар"

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