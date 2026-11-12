from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .permissions import CANCEL_SENT_ROLES, MANAGE_ROLES, PAYMENT_ROLES, PIN_CANCEL_ROLES, SEE_ALL_ROLES


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
    # Зарплата: часы по табелю × ставка + процент от продаж (заказы, которые вёл сотрудник)
    hourly_rate = models.DecimalField(_("Ставка в час (сум)"), max_digits=12, decimal_places=0, default=0)
    sales_percent = models.DecimalField(
        _("Процент от продаж"), max_digits=5, decimal_places=2, default=0,
        help_text=_("Например, 5 — официант получает 5% от суммы своих оплаченных заказов."),
    )

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
    def manages_cafe(self):
        """Владелец и администратор: им нужны фильтры и сортировки, рядовым сотрудникам нет."""
        return self.is_superuser or self.role in MANAGE_ROLES

    @property
    def simple_floor(self):
        """Официанту экран зала без лишнего: без быстрых продаж, сортировок и кассовых кнопок."""
        return not self.is_superuser and self.role == self.Role.WAITER

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


class WorkShift(models.Model):
    """Табель: сотрудник отметил начало и конец работы. По этим отрезкам считаются часы для зарплаты."""

    user = models.ForeignKey(User, verbose_name=_("Сотрудник"), on_delete=models.CASCADE, related_name="work_shifts")
    started_at = models.DateTimeField(_("Начало"), default=timezone.now, db_index=True)
    ended_at = models.DateTimeField(_("Конец"), null=True, blank=True)
    closed_by = models.ForeignKey(
        User, verbose_name=_("Кто закрыл"), null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )

    class Meta:
        verbose_name = _("Рабочая смена")
        verbose_name_plural = _("Табель")
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.user} {self.started_at:%d.%m %H:%M}"

    def hours_within(self, start, end, now=None):
        """Сколько часов этой смены попало в отрезок [start, end). Незакрытая смена считается до текущего момента."""
        finish = self.ended_at or now or timezone.now()
        a, b = max(self.started_at, start), min(finish, end)
        return max(0.0, (b - a).total_seconds() / 3600)
