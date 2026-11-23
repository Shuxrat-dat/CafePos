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
        help_text=_("Например, 3 — официант получает 3% от суммы блюд своих оплаченных заказов (без обслуживания)."),
    )
    # PIN для входа на общем терминале (моноблоке). Задаёт владелец или администратор, хранится только хеш.
    # Работает только на устройствах, которые владелец сделал терминалом. См. accounts/terminal.py
    login_pin_hash = models.CharField("Хеш PIN для терминала", max_length=128, blank=True)
    login_pin_failures = models.PositiveSmallIntegerField("Неверных PIN подряд", default=0)
    login_pin_locked_until = models.DateTimeField("PIN заблокирован до", null=True, blank=True)

    @property
    def can_use_terminal(self):
        """Входить по PIN на терминале могут только официанты. Владелец и администратор — только паролем."""
        from .permissions import PIN_LOGIN_ROLES

        return not self.is_superuser and self.role in PIN_LOGIN_ROLES

    @property
    def has_login_pin(self):
        return bool(self.login_pin_hash)

    @property
    def login_pin_is_locked(self):
        return bool(self.login_pin_locked_until) and self.login_pin_locked_until > timezone.now()

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
    def home_url_name(self):
        """Куда вести после входа и «Назад»: официанту схема столов, остальным экран заказов."""
        return "table_map" if self.simple_floor else "floor"

    @property
    def can_take_payment(self):
        return self.is_superuser or self.role in PAYMENT_ROLES

    @property
    def sees_all_orders(self):
        return self.is_superuser or self.role in SEE_ALL_ROLES

    @property
    def can_see_reports(self):
        """Отчёты видят владелец и администратор; кассир, если владелец включил галочку в настройках."""
        from .permissions import REPORT_ROLES

        if self.is_superuser or self.role in REPORT_ROLES:
            return True
        if self.role == self.Role.CASHIER:
            from core.models import CafeSettings

            return CafeSettings.get().cashier_sees_reports
        return False

    @property
    def can_remove_sent_items(self):
        """Убрать из заказа блюдо, уже ушедшее на кухню. Официанту можно: каждое такое удаление пишется в журнал
        и уходит уведомлением владельцу. Целый заказ с кухни отменяют только те, кто вправе (can_cancel_sent_orders)."""
        return self.can_cancel_sent_orders or (not self.is_superuser and self.role == self.Role.WAITER)

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


class SalaryPayment(models.Model):
    """Выдача денег сотруднику: выплата заработанного или аванс. Остаток = начислено − все выдачи.
    Каждая выдача сразу записывается в расходы (статья «Зарплата»); если деньги взяты из кассы —
    ещё и изъятием в открытой кассовой смене, чтобы наличные сходились."""

    class Kind(models.TextChoices):
        PAYOUT = "payout", _("Выплата")
        ADVANCE = "advance", _("Аванс")

    user = models.ForeignKey(User, verbose_name=_("Сотрудник"), on_delete=models.PROTECT, related_name="salary_payments")
    kind = models.CharField(_("Вид"), max_length=10, choices=Kind.choices, default=Kind.PAYOUT)
    amount = models.DecimalField(_("Сумма (сум)"), max_digits=12, decimal_places=0)
    comment = models.CharField(_("Комментарий"), max_length=200, blank=True)
    from_cash = models.BooleanField(_("Из кассы"), default=False)
    cash_movement = models.ForeignKey("pos.CashMovement", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    expense = models.ForeignKey("reports.Expense", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_by = models.ForeignKey(User, verbose_name=_("Кто выдал"), null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(_("Когда"), default=timezone.now, db_index=True)

    class Meta:
        verbose_name = _("Выплата сотруднику")
        verbose_name_plural = _("Выплаты сотрудникам")
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.user} · {self.get_kind_display()} · {self.amount}"


class Terminal(models.Model):
    """Общее устройство в зале (моноблок). На нём официанты входят по плиткам и короткому PIN.
    Устройство узнаётся по долгоживущему cookie; в базе лежит только хеш его ключа."""

    name = models.CharField(_("Название"), max_length=60)
    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    is_active = models.BooleanField(_("Работает"), default=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(_("Создан"), auto_now_add=True)
    last_seen_at = models.DateTimeField(_("Последний раз"), null=True, blank=True)

    class Meta:
        verbose_name = _("Терминал")
        verbose_name_plural = _("Терминалы")
        ordering = ["name", "id"]

    def __str__(self):
        return self.name
