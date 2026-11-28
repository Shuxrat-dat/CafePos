from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from menu.models import Dish, Modifier


def round_sum(value):
    """Суммы в сумах всегда целые, округляем по правилам арифметики."""
    return Decimal(value).quantize(Decimal("1"), rounding=ROUND_HALF_UP)


class Hall(models.Model):
    name = models.CharField(_("Название"), max_length=100)
    name_uz = models.CharField(_("Название (узб.)"), max_length=100, blank=True)
    sort_order = models.PositiveIntegerField(_("Порядок"), default=0)
    service_percent = models.DecimalField(
        _("Обслуживание, %"), max_digits=5, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(30)],
        help_text=_("Пусто — брать общую ставку из настроек заведения. 0 — в этом зале не начислять."),
    )

    class Meta:
        verbose_name = "Зал"
        verbose_name_plural = "Залы"
        ordering = ["sort_order", "id"]

    def __str__(self):
        return self.name


TEXT_TABLE_KEY = 1_000_000  # столы с названием вместо номера («VIP», «T1») идут после нумерованных


def table_sort_key(number):
    """Порядок столов как у людей: 1, 2, … 10, 11, а не 1, 10, 11, 2 (так сортирует текст)."""
    number = (number or "").strip()
    return int(number) if number.isdigit() and len(number) < 7 else TEXT_TABLE_KEY


class Table(models.Model):
    hall = models.ForeignKey(Hall, verbose_name=_("Зал"), on_delete=models.CASCADE, related_name="tables")
    number = models.CharField(_("Номер или название"), max_length=20)
    seats = models.PositiveSmallIntegerField(_("Мест"), default=4)
    reserved_for = models.CharField(_("Бронь на имя"), max_length=100, blank=True)
    reserved_at = models.DateTimeField(_("Время брони"), null=True, blank=True)
    # Кабины и VIP: аренда по времени. 0 — обычный стол без почасовой оплаты
    hourly_price = models.DecimalField(_("Аренда за час (сум)"), max_digits=12, decimal_places=0, default=0,
                                       help_text=_("0 — обычный стол. Время считается от открытия заказа и добавляется к счёту."))
    billing_step_minutes = models.PositiveSmallIntegerField(_("Шаг расчёта, мин"), default=30,
                                                            help_text=_("Начатый шаг оплачивается полностью: при шаге 30 минут 40 минут считаются как 60."))
    sort_key = models.PositiveIntegerField(default=TEXT_TABLE_KEY, editable=False, db_index=True)

    class Meta:
        verbose_name = "Стол"
        verbose_name_plural = "Столы"
        unique_together = ("hall", "number")
        ordering = ["hall", "sort_key", "number"]

    def __str__(self):
        return f"{self.hall.name}, стол {self.number}"

    def save(self, *args, **kwargs):
        self.sort_key = table_sort_key(self.number)
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and "number" in update_fields:
            kwargs["update_fields"] = {*update_fields, "sort_key"}
        super().save(*args, **kwargs)

    @property
    def is_reserved(self):
        return bool(self.reserved_for or self.reserved_at)

    @property
    def menu_key(self):
        """Ключ из QR-кода стола. Без него заказать нельзя: номера столов идут подряд и их легко перебрать."""
        from django.utils.crypto import salted_hmac

        return salted_hmac("table-menu", str(self.pk)).hexdigest()[:16]

    def menu_path(self):
        from django.urls import reverse

        return f"{reverse('table_menu', args=[self.pk])}?k={self.menu_key}"


class CashShift(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", _("Открыта")
        CLOSED = "closed", _("Закрыта")

    cashier = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("Кассир"),
        on_delete=models.PROTECT,
        related_name="cash_shifts",
    )
    opened_at = models.DateTimeField(_("Время открытия"), default=timezone.now)
    closed_at = models.DateTimeField(_("Время закрытия"), null=True, blank=True)
    initial_cash = models.DecimalField(_("Начальный остаток (размен)"), max_digits=14, decimal_places=0, default=0)
    actual_cash = models.DecimalField(_("Фактический остаток"), max_digits=14, decimal_places=0, null=True, blank=True)
    closing_comment = models.CharField(_("Комментарий к закрытию"), max_length=255, blank=True)
    status = models.CharField(_("Статус"), max_length=10, choices=Status.choices, default=Status.OPEN)

    class Meta:
        verbose_name = "Кассовая смена"
        verbose_name_plural = "Кассовые смены"
        ordering = ["-opened_at"]

    def __str__(self):
        cashier_name = self.cashier.get_full_name() or self.cashier.username
        return f"Смена #{self.id} ({cashier_name}) - {self.get_status_display()}"

    @property
    def is_open(self):
        return self.status == self.Status.OPEN

    @property
    def cash_sales(self):
        """Сумма поступлений наличными за заказы в этой смене."""
        from django.db.models import Sum
        val = self.payments.filter(is_refund=False).aggregate(total=Sum("cash_amount"))["total"]
        if val is None:
            val = sum((o.cash_amount for o in self.orders.filter(status=Order.Status.PAID)), Decimal(0))
        return val or Decimal(0)

    @property
    def card_sales(self):
        """Оплаты картой. Click хранится в тех же безналичных суммах платежа, но считается отдельно."""
        from django.db.models import Sum
        payments = self.payments.filter(is_refund=False).exclude(payment_method=Order.PaymentMethod.CLICK)
        val = payments.aggregate(total=Sum("card_amount"))["total"]
        if val is None:
            val = sum((o.card_paid for o in self.orders.filter(status=Order.Status.PAID)), Decimal(0))
        return val or Decimal(0)

    @property
    def click_sales(self):
        """Click: оплаты целиком через Click (сумма в card_amount) и часть Click в смешанной оплате (click_amount)."""
        from django.db.models import Sum
        payments = self.payments.filter(is_refund=False)
        whole = payments.filter(payment_method=Order.PaymentMethod.CLICK).aggregate(total=Sum("card_amount"))["total"]
        part = payments.aggregate(total=Sum("click_amount"))["total"]
        return (whole or Decimal(0)) + (part or Decimal(0))

    @property
    def total_revenue(self):
        return self.cash_sales + self.card_sales + self.click_sales

    @property
    def cash_refunds(self):
        from django.db.models import Sum
        val = self.payments.filter(is_refund=True).aggregate(total=Sum("cash_amount"))["total"]
        return val or Decimal(0)

    @property
    def card_refunds(self):
        from django.db.models import Sum
        val = self.payments.filter(is_refund=True).aggregate(total=Sum("card_amount"))["total"]
        return val or Decimal(0)

    @property
    def cash_deposits(self):
        from django.db.models import Sum
        val = self.cash_movements.filter(kind=CashMovement.Kind.DEPOSIT).aggregate(total=Sum("amount"))["total"]
        return val or Decimal(0)

    @property
    def cash_withdrawals(self):
        from django.db.models import Sum
        val = self.cash_movements.filter(kind=CashMovement.Kind.WITHDRAWAL).aggregate(total=Sum("amount"))["total"]
        return val or Decimal(0)

    @property
    def debt_cash_repaid(self):
        """Наличные, которые гости принесли в эту смену в счёт долга."""
        from django.db.models import Sum
        val = self.debt_entries.filter(amount__lt=0, method=DebtEntry.Method.CASH).aggregate(total=Sum("amount"))["total"]
        return -(val or Decimal(0))

    @property
    def debt_card_repaid(self):
        from django.db.models import Sum
        val = self.debt_entries.filter(amount__lt=0, method=DebtEntry.Method.CARD).aggregate(total=Sum("amount"))["total"]
        return -(val or Decimal(0))

    @property
    def deposits_cash_in(self):
        """Наличные депозиты броней, принятые в эту смену."""
        from django.db.models import Sum
        return self.deposits.filter(method="cash").aggregate(s=Sum("amount"))["s"] or Decimal(0)

    @property
    def deposits_cash_out(self):
        """Наличные, возвращённые гостям из депозитов в эту смену."""
        from django.db.models import Sum
        return self.deposit_refunds.filter(method="cash").aggregate(s=Sum("refund_amount"))["s"] or Decimal(0)

    @property
    def expected_cash(self):
        """Ожидаемые наличные в кассе по формуле инварианта:
        Начальный остаток + Наличная выручка - Возвраты наличными + Внесения - Изъятия + Погашение долгов наличными
        """
        return (self.initial_cash + self.cash_sales - self.cash_refunds + self.cash_deposits - self.cash_withdrawals
                + self.debt_cash_repaid + self.deposits_cash_in - self.deposits_cash_out)

    @property
    def difference(self):
        if self.actual_cash is None:
            return None
        return self.actual_cash - self.expected_cash


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
        CLICK = "click", "Click"
        MIXED = "mixed", _("Смешанная")
        DEBT = "debt", _("В долг")

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
    # Доля официанта фиксируется при оплате: процент и сумма. Смена процента потом старые дни не пересчитывает.
    # Считается от суммы блюд после скидки, без обслуживания. Пусто — заказ оплачен до появления этих полей.
    waiter_percent = models.DecimalField(_("Процент официанта"), max_digits=5, decimal_places=2, null=True, blank=True)
    waiter_earning = models.DecimalField(_("Заработок официанта"), max_digits=12, decimal_places=0, null=True, blank=True)

    payment_method = models.CharField(_("Способ оплаты"), max_length=10, choices=PaymentMethod.choices, blank=True)
    cash_received = models.DecimalField(_("Получено наличными"), max_digits=12, decimal_places=0, null=True, blank=True)
    card_amount = models.DecimalField(_("Оплачено картой"), max_digits=12, decimal_places=0, null=True, blank=True)
    # Часть счёта через Click при смешанной оплате (наличные + карта + Click + долг)
    click_amount = models.DecimalField(_("Оплачено через Click"), max_digits=12, decimal_places=0, null=True, blank=True)
    # Часть счёта, записанная в долг при смешанной оплате (наличные + карта + долг)
    debt_amount = models.DecimalField(_("Записано в долг"), max_digits=12, decimal_places=0, null=True, blank=True)
    cashier = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name=_("Кассир"), on_delete=models.SET_NULL,
        null=True, blank=True, related_name="cashed_orders",
    )
    shift = models.ForeignKey(
        CashShift, verbose_name=_("Кассовая смена"), on_delete=models.SET_NULL,
        null=True, blank=True, related_name="orders",
    )

    bill_requested_at = models.DateTimeField(_("Пречек выдан"), null=True, blank=True)
    # Аренда кабины фиксируется в момент оплаты; до этого считается по часам заново
    room_charge = models.DecimalField(_("Аренда кабины"), max_digits=12, decimal_places=0, null=True, blank=True)
    room_minutes = models.PositiveIntegerField(_("Минут аренды"), null=True, blank=True)
    # Депозит брони, зачтённый в счёт этого заказа: гость уже заплатил эту часть раньше
    deposit_applied = models.DecimalField(_("Депозит в счёт заказа"), max_digits=12, decimal_places=0, default=0)
    cancel_reason = models.CharField(_("Причина отмены"), max_length=200, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name=_("Кто отменил"), on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    created_at = models.DateTimeField(_("Создан"), auto_now_add=True)
    closed_at = models.DateTimeField(_("Закрыт"), null=True, blank=True)
    # Номер заказа за день: каждый день с 00:00 заказы снова идут с №1. Внутренний номер (id) не меняется
    day_number = models.PositiveIntegerField(_("Номер за день"), null=True, blank=True, editable=False)

    class Meta:
        verbose_name = "Заказ"
        verbose_name_plural = "Заказы"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "closed_at"])]

    def __str__(self):
        return f"Заказ №{self.pk}"

    @property
    def number(self):
        """Номер для людей: на экране, кухонном чеке и чеке. Старые заказы без номера за день — по id."""
        return self.day_number or self.pk

    def save(self, *args, **kwargs):
        if self._state.adding and not self.day_number:
            from django.db import transaction

            # Подсчёт номера и запись — одной транзакцией: два заказа в одну секунду не получат один номер
            with transaction.atomic():
                start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
                last = Order.objects.filter(created_at__gte=start).aggregate(n=models.Max("day_number"))["n"]
                self.day_number = (last or 0) + 1
                super().save(*args, **kwargs)
            return
        super().save(*args, **kwargs)

    # ---------- суммы ----------

    def room_live(self, now=None):
        """(оплачиваемые минуты, сумма) аренды кабины на сейчас. Начатый шаг оплачивается целиком."""
        if not self.table_id or not self.is_dine_in or self.status != self.Status.OPEN:
            return 0, Decimal(0)
        table = self.table
        if not table.hourly_price:
            return 0, Decimal(0)
        step = max(1, table.billing_step_minutes)
        elapsed = ((now or timezone.now()) - self.created_at).total_seconds() / 60
        steps = max(1, -int(-elapsed // step))  # округление вверх, минимум один шаг
        minutes = steps * step
        return minutes, round_sum(table.hourly_price * minutes / 60)

    @property
    def room_amount(self):
        if self.room_charge is not None:
            return self.room_charge
        return self.room_live()[1]

    @property
    def subtotal(self):
        return sum((item.line_total for item in self.items.all()), Decimal(0)) + self.room_amount

    def discount_for(self, subtotal):
        if self.discount_kind == self.DiscountKind.PERCENT:
            return min(subtotal, round_sum(subtotal * self.discount_value / 100))
        if self.discount_kind == self.DiscountKind.AMOUNT:
            return min(subtotal, round_sum(self.discount_value))
        return Decimal(0)

    def service_for(self, subtotal, no_service=Decimal(0)):
        """Обслуживание с суммы после скидки. no_service — сумма блюд «с собой»: на неё обслуживание не начисляется,
        скидка делится между частями пропорционально."""
        if not self.service_percent:
            return Decimal(0)
        base = subtotal - self.discount_for(subtotal)
        if no_service and subtotal:
            base = base * (subtotal - min(no_service, subtotal)) / subtotal
        return round_sum(base * self.service_percent / 100)

    @property
    def takeaway_amount(self):
        """Сумма блюд «с собой» в заказе за столом."""
        return sum((item.line_total for item in self.items.all() if item.takeaway), Decimal(0))

    def totals_for(self, subtotal, no_service=Decimal(0)):
        """Подытог, скидка, обслуживание и итог для заданной суммы позиций."""
        discount = self.discount_for(subtotal)
        service = self.service_for(subtotal, no_service)
        return {"subtotal": subtotal, "discount": discount, "service": service, "total": subtotal - discount + service}

    @property
    def totals(self):
        return self.totals_for(self.subtotal, self.takeaway_amount)

    @property
    def total(self):
        return self.totals["total"]

    @property
    def amount_due(self):
        """Сколько гость платит сейчас: итог минус депозит, внесённый при брони."""
        return max(Decimal(0), self.total - (self.deposit_applied or 0))

    @property
    def waiter_base(self):
        """От чего считается процент официанта: блюда (и аренда) после скидки, без обслуживания.
        Счёт 560 000 + 10% обслуживания: официант с 3% получает 16 800 из этих 56 000."""
        totals = self.totals
        return totals["subtotal"] - totals["discount"]

    def fix_waiter_earning(self):
        """Записывает процент и сумму официанта на момент оплаты. Вызывается из pay_order."""
        percent = self.waiter.sales_percent or Decimal(0)
        self.waiter_percent = percent
        self.waiter_earning = round_sum(self.waiter_base * percent / 100) if percent else Decimal(0)

    @property
    def earned_by_waiter(self):
        """Заработок официанта по этому заказу. Для заказов, оплаченных до фиксации, — по текущему проценту."""
        if self.waiter_earning is not None:
            return self.waiter_earning
        percent = self.waiter.sales_percent or Decimal(0)
        return round_sum(self.waiter_base * percent / 100) if percent else Decimal(0)

    @property
    def cash_amount(self):
        """Сколько из итога оплачено наличными (без сдачи и без депозита)."""
        if self.payment_method == self.PaymentMethod.CASH:
            return self.amount_due
        if self.payment_method == self.PaymentMethod.MIXED:
            return self.amount_due - (self.card_amount or 0) - (self.click_amount or 0) - (self.debt_amount or 0)
        return Decimal(0)

    @property
    def card_paid(self):
        if self.payment_method == self.PaymentMethod.CARD:
            return self.amount_due
        if self.payment_method == self.PaymentMethod.MIXED:
            return self.card_amount or Decimal(0)
        return Decimal(0)

    @property
    def click_paid(self):
        if self.payment_method == self.PaymentMethod.CLICK:
            return self.amount_due
        if self.payment_method == self.PaymentMethod.MIXED:
            return self.click_amount or Decimal(0)
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
    # Блюдо на вес: price — цена за 1 кг, сумма считается по weight_grams. Признак фиксируется при заказе.
    by_weight = models.BooleanField(_("На вес"), default=False)
    # kg — граммы, l — миллилитры, pc — штуки из пачки (сигареты): в weight_grams число штук, цена за пачку
    measure = models.CharField(_("Единица"), max_length=2, default="kg")
    weight_grams = models.PositiveIntegerField(_("Вес, г"), null=True, blank=True)
    pack_size = models.PositiveSmallIntegerField(_("Штук в пачке"), null=True, blank=True)
    modifiers = models.ManyToManyField(Modifier, verbose_name=_("Модификаторы"), blank=True)
    comment = models.CharField(_("Комментарий"), max_length=200, blank=True)
    # Гость за столом берёт блюдо с собой: на кухне его упаковывают, обслуживание на него не начисляется
    takeaway = models.BooleanField(_("С собой"), default=False)
    promo_label = models.CharField(_("Акция"), max_length=100, blank=True)
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
            if self._state.adding:
                # Акция или «счастливые часы»: скидка фиксируется в момент добавления блюда
                from menu.models import best_promotion, promo_price

                promo = best_promotion(self.dish)
                if promo:
                    self.price = promo_price(self.dish.price, promo.percent)
                    self.promo_label = f"{promo.name} −{promo.percent}%"[:100]
        if self._state.adding and self.dish.sold_by_weight:
            self.by_weight = True
            self.measure = self.dish.measure
        super().save(*args, **kwargs)

    @property
    def weight_factor(self):
        """Доля от цены и техкарты: для блюда на вес это килограммы (450 г -> 0.45), для штучного 1."""
        if not self.by_weight:
            return Decimal(1)
        if self.is_pieces:
            return Decimal(self.weight_grams or 0) / (self.pack_size or 1)  # 3 сигареты из 20 -> 0.15 пачки
        return Decimal(self.weight_grams or 0) / 1000

    @property
    def is_pieces(self):
        """Продажа поштучно из пачки (сигареты)."""
        return self.by_weight and self.measure == "pc"

    @property
    def needs_weight(self):
        """Блюдо на вес без указанного веса (например, из заявки гостя): оплатить его нельзя."""
        return self.by_weight and not self.weight_grams

    @property
    def unit_total(self):
        """Цена одной штуки с учётом доплат за модификаторы. У блюда на вес — цена за его вес."""
        extras = sum(m.price_delta for m in self.modifiers.all())
        if self.by_weight:
            return round_sum(self.price * self.weight_factor) + extras
        return self.price + extras

    @property
    def line_total(self):
        return self.unit_total * self.quantity


def receipt_lines(items):
    """Строки для чека гостя: одинаковые блюда одной строкой с общим количеством.
    Блюдо добавляли в несколько приёмов (чай 1 + 1 + 1) — в заказе и на кухонных чеках это разные позиции,
    а гостю печатается «3 x Чай». Склеиваются только блюда с той же ценой, теми же вариантами и тем же «с собой»;
    блюда на вес не склеиваются — у каждого свой вес. Возвращает копии позиций, в базу ничего не пишется."""
    import copy

    lines, by_key = [], {}
    for item in items:
        if item.by_weight:
            lines.append(item)
            continue
        key = (item.dish_id, item.unit_total, tuple(sorted(m.id for m in item.modifiers.all())), item.takeaway)
        line = by_key.get(key)
        if line is None:
            line = copy.copy(item)
            by_key[key] = line
            lines.append(line)
        else:
            line.quantity += item.quantity
    return lines


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


class CashMovement(models.Model):
    class Kind(models.TextChoices):
        DEPOSIT = "deposit", _("Внесение")
        WITHDRAWAL = "withdrawal", _("Изъятие / Инкассация")

    shift = models.ForeignKey(CashShift, verbose_name=_("Смена"), on_delete=models.CASCADE, related_name="cash_movements")
    kind = models.CharField(_("Тип"), max_length=15, choices=Kind.choices)
    amount = models.DecimalField(_("Сумма"), max_digits=14, decimal_places=0)
    reason = models.CharField(_("Причина / Назначение"), max_length=255, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, verbose_name=_("Сотрудник"), on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(_("Время"), auto_now_add=True)

    class Meta:
        verbose_name = "Движение наличных в смене"
        verbose_name_plural = "Движения наличных в смене"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_kind_display()}: {self.amount} сум ({self.shift})"


class Payment(models.Model):
    order = models.ForeignKey(Order, verbose_name=_("Заказ"), on_delete=models.CASCADE, related_name="payments")
    shift = models.ForeignKey(CashShift, verbose_name=_("Кассовая смена"), on_delete=models.PROTECT, null=True, blank=True, related_name="payments")
    payment_method = models.CharField(_("Способ оплаты"), max_length=10, choices=Order.PaymentMethod.choices)
    amount = models.DecimalField(_("Сумма платежа"), max_digits=14, decimal_places=0)
    cash_amount = models.DecimalField(_("Из них наличными"), max_digits=14, decimal_places=0, default=0)
    card_amount = models.DecimalField(_("Из них картой"), max_digits=14, decimal_places=0, default=0)
    # Часть через Click при смешанной оплате. При оплате целиком через Click сумма лежит в card_amount
    click_amount = models.DecimalField(_("Из них через Click"), max_digits=14, decimal_places=0, default=0)
    cash_received = models.DecimalField(_("Получено наличных"), max_digits=14, decimal_places=0, null=True, blank=True)
    change = models.DecimalField(_("Сдача"), max_digits=14, decimal_places=0, default=0)
    cashier = models.ForeignKey(settings.AUTH_USER_MODEL, verbose_name=_("Кассир"), on_delete=models.SET_NULL, null=True, blank=True, related_name="processed_payments")
    idempotency_key = models.CharField(_("Ключ идемпотентности"), max_length=64, unique=True, null=True, blank=True)
    is_refund = models.BooleanField(_("Возврат"), default=False)
    refund_reason = models.CharField(_("Причина возврата"), max_length=255, blank=True)
    created_at = models.DateTimeField(_("Время платежа"), auto_now_add=True)

    class Meta:
        verbose_name = "Платёж"
        verbose_name_plural = "Платежи"
        ordering = ["-created_at"]

    def __str__(self):
        kind = "Возврат" if self.is_refund else "Оплата"
        return f"{kind} #{self.id} по заказу #{self.order_id}: {self.amount} сум ({self.get_payment_method_display()})"



class Review(models.Model):
    """Оценка гостя после оплаты. Один отзыв на заказ."""

    order = models.OneToOneField(Order, verbose_name=_("Заказ"), on_delete=models.CASCADE, related_name="review")
    rating = models.PositiveSmallIntegerField(_("Оценка"))
    comment = models.CharField(_("Комментарий"), max_length=500, blank=True)
    created_at = models.DateTimeField(_("Когда"), auto_now_add=True)

    class Meta:
        verbose_name = "Отзыв"
        verbose_name_plural = "Отзывы"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Заказ #{self.order_id}: {self.rating}/5"


class Customer(models.Model):
    """Гость, которого узнаём по номеру телефона. Копит баллы за оплаченные заказы."""

    phone = models.CharField(_("Телефон"), max_length=20, unique=True, help_text="Только последние 9 цифр номера")
    name = models.CharField(_("Имя"), max_length=100, blank=True)
    points = models.IntegerField(_("Баллы"), default=0)
    visits = models.PositiveIntegerField(_("Визиты"), default=0)
    spent = models.DecimalField(_("Потрачено всего"), max_digits=14, decimal_places=0, default=0)
    created_at = models.DateTimeField(_("Первый визит"), auto_now_add=True)

    class Meta:
        verbose_name = "Гость"
        verbose_name_plural = "Гости"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name or 'Гость'} {self.phone}"

    @property
    def debt(self):
        from django.db.models import Sum
        return self.debt_entries.aggregate(s=Sum("amount"))["s"] or Decimal(0)


class DebtEntry(models.Model):
    """Долг гостя. Плюс — заказ записан в долг, минус — гость погасил (наличными или картой в смене).
    Остаток долга = сумма записей; по ним всегда видно, откуда он взялся."""

    class Method(models.TextChoices):
        CASH = "cash", _("Наличные")
        CARD = "card", _("Карта")

    customer = models.ForeignKey(Customer, verbose_name=_("Гость"), on_delete=models.PROTECT, related_name="debt_entries")
    order = models.ForeignKey(Order, verbose_name=_("Заказ"), on_delete=models.SET_NULL, null=True, blank=True, related_name="debt_entries")
    amount = models.DecimalField(_("Сумма"), max_digits=14, decimal_places=0)
    method = models.CharField(_("Способ погашения"), max_length=10, choices=Method.choices, blank=True)
    shift = models.ForeignKey(CashShift, verbose_name=_("Смена"), on_delete=models.PROTECT, null=True, blank=True, related_name="debt_entries")
    cashier = models.ForeignKey(settings.AUTH_USER_MODEL, verbose_name=_("Кассир"), on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    comment = models.CharField(_("Комментарий"), max_length=200, blank=True)
    created_at = models.DateTimeField(_("Время"), auto_now_add=True)

    class Meta:
        verbose_name = _("Долг гостя")
        verbose_name_plural = _("Долги гостей")
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.customer}: {self.amount}"


class Deposit(models.Model):
    """Депозит брони. Деньги приняты в смену при брони; потом они идут в счёт заказа гостя, возвращаются
    или остаются заведению, если гость не пришёл."""

    class Method(models.TextChoices):
        CASH = "cash", _("Наличные")
        CARD = "card", _("Карта")

    class Status(models.TextChoices):
        HELD = "held", _("Ждёт гостя")
        APPLIED = "applied", _("Зачтён в заказ")
        REFUNDED = "refunded", _("Возвращён")
        KEPT = "kept", _("Оставлен заведению")

    table = models.ForeignKey(Table, verbose_name=_("Стол"), on_delete=models.SET_NULL, null=True, blank=True, related_name="deposits")
    guest_name = models.CharField(_("На чьё имя"), max_length=100, blank=True)
    amount = models.DecimalField(_("Сумма"), max_digits=12, decimal_places=0)
    method = models.CharField(_("Способ"), max_length=10, choices=Method.choices, default=Method.CASH)
    status = models.CharField(_("Статус"), max_length=10, choices=Status.choices, default=Status.HELD, db_index=True)
    shift = models.ForeignKey(CashShift, verbose_name=_("Смена приёма"), on_delete=models.PROTECT, related_name="deposits")
    cashier = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    order = models.ForeignKey(Order, verbose_name=_("Заказ"), on_delete=models.SET_NULL, null=True, blank=True, related_name="deposits")
    refund_amount = models.DecimalField(_("Возвращено"), max_digits=12, decimal_places=0, default=0)
    refund_shift = models.ForeignKey(CashShift, verbose_name=_("Смена возврата"), on_delete=models.PROTECT, null=True, blank=True, related_name="deposit_refunds")
    created_at = models.DateTimeField(auto_now_add=True)
    settled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("Депозит брони")
        verbose_name_plural = _("Депозиты броней")
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.guest_name or self.table_id}: {self.amount}"


class Booking(models.Model):
    """Заявка на бронь от гостя (онлайн) с датой, временем и числом гостей. Сотрудник подтверждает её и выбирает стол;
    в день брони подтверждённая бронь сама появляется на схеме столов."""

    class Status(models.TextChoices):
        NEW = "new", _("Новая заявка")
        CONFIRMED = "confirmed", _("Подтверждена")
        REJECTED = "rejected", _("Отклонена")
        CANCELLED = "cancelled", _("Отменена")
        DONE = "done", _("Прошла")

    name = models.CharField(_("Имя"), max_length=100)
    phone = models.CharField(_("Телефон"), max_length=20)
    guests = models.PositiveSmallIntegerField(_("Гостей"), default=2)
    date = models.DateField(_("Дата"), db_index=True)
    time = models.TimeField(_("Время"))
    comment = models.CharField(_("Пожелания"), max_length=200, blank=True)
    status = models.CharField(_("Статус"), max_length=12, choices=Status.choices, default=Status.NEW, db_index=True)
    table = models.ForeignKey(Table, verbose_name=_("Стол"), on_delete=models.SET_NULL, null=True, blank=True, related_name="bookings")
    applied = models.BooleanField(_("Показана на схеме"), default=False)
    handled_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    handled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Бронь")
        verbose_name_plural = _("Брони")
        ordering = ["date", "time", "id"]

    def __str__(self):
        return f"{self.name} {self.date} {self.time:%H:%M}"

    @property
    def starts_at(self):
        return timezone.make_aware(datetime.combine(self.date, self.time))


class LoyaltyEntry(models.Model):
    """Движение баллов. По записям всегда можно проверить, откуда взялся остаток."""

    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="entries")
    order = models.ForeignKey(Order, on_delete=models.SET_NULL, null=True, blank=True, related_name="loyalty_entries")
    points = models.IntegerField(_("Баллы"))
    comment = models.CharField(_("Комментарий"), max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
