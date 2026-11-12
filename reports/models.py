from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class Expense(models.Model):
    """Расход заведения, которого нет в себестоимости блюд: аренда, коммуналка, зарплата и прочее.
    Нужен, чтобы отчёт показывал чистую прибыль, а не только валовую."""

    class Category(models.TextChoices):
        RENT = "rent", _("Аренда")
        UTILITIES = "utilities", _("Коммунальные услуги")
        SALARY = "salary", _("Зарплата")
        SUPPLIES = "supplies", _("Хозтовары и расходники")
        MARKETING = "marketing", _("Реклама")
        REPAIR = "repair", _("Ремонт и обслуживание")
        TAXES = "taxes", _("Налоги и сборы")
        OTHER = "other", _("Прочее")

    date = models.DateField(_("Дата"), default=timezone.localdate, db_index=True)
    category = models.CharField(_("Статья"), max_length=20, choices=Category.choices, default=Category.OTHER)
    amount = models.DecimalField(_("Сумма (сум)"), max_digits=14, decimal_places=0)
    comment = models.CharField(_("Комментарий"), max_length=200, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name=_("Кто внёс"), null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+",
    )
    created_at = models.DateTimeField(_("Внесено"), auto_now_add=True)

    class Meta:
        verbose_name = _("Расход")
        verbose_name_plural = _("Расходы")
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"{self.date} · {self.get_category_display()} · {self.amount}"
