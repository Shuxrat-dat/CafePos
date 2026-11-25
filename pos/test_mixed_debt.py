import uuid
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import CafeSettings
from menu.models import Category, Dish
from pos import services
from pos.models import Customer, DebtEntry, Order, OrderItem

User = get_user_model()


class MixedPaymentWithDebtTests(TestCase):
    """Смешанная оплата: наличные + карта + часть счёта в долг (счёт 280 000, гость даёт 200 000)."""

    def setUp(self):
        self.cashier = User.objects.create_user(username="kass_mixdebt", password="x", role="cashier")
        self.waiter = User.objects.create_user(username="ofi_mixdebt", password="x", role="waiter")
        cafe = CafeSettings.get()
        cafe.accept_debt = cafe.accept_mixed = cafe.accept_cash = cafe.accept_card = True
        cafe.service_percent = 0
        cafe.save()
        self.dish = Dish.objects.create(category=Category.objects.create(name="Еда"), name="Плов", price=Decimal("140000"))
        self.shift = services.open_shift(self.cashier, initial_cash=Decimal("100000"))
        self.client.force_login(self.cashier)

    def order(self, name="Акмаль", phone="+998 90 123 45 67"):
        order = services.create_order(self.waiter, Order.OrderType.TAKEAWAY)
        OrderItem.objects.create(order=order, dish=self.dish, quantity=2, price=self.dish.price, status=OrderItem.Status.SERVED)
        order.customer_name, order.customer_phone = name, phone
        order.save()
        return order

    def pay(self, order, cash="200000", card="", debt="80000", **extra):
        data = {"method": "mixed", "received": cash, "card": card, "debt": debt,
                "debt_name": "Акмаль", "debt_phone": "+998 90 123 45 67", "idempotency_key": str(uuid.uuid4())}
        data.update(extra)
        return self.client.post(reverse("pay", args=[order.id]), data)

    def test_cash_plus_debt(self):
        order = self.order()
        response = self.pay(order)
        self.assertEqual(response.status_code, 302)
        order = Order.objects.get(pk=order.pk)
        self.assertEqual(order.status, Order.Status.PAID)
        self.assertEqual(order.cash_amount, Decimal("200000"))
        self.assertEqual(order.debt_amount, Decimal("80000"))
        self.assertEqual(Customer.objects.get(phone="901234567").debt, Decimal("80000"))
        self.shift.refresh_from_db()
        self.assertEqual(self.shift.cash_sales, Decimal("200000"))
        self.assertEqual(self.shift.expected_cash, Decimal("300000"))

    def test_cash_card_and_debt_together(self):
        order = self.order()
        self.pay(order, cash="150000", card="50000", debt="80000")
        order = Order.objects.get(pk=order.pk)
        self.assertEqual((order.cash_amount, order.card_paid, order.debt_amount),
                         (Decimal("150000"), Decimal("50000"), Decimal("80000")))
        self.assertEqual(Customer.objects.get().debt, Decimal("80000"))
        self.shift.refresh_from_db()
        self.assertEqual((self.shift.cash_sales, self.shift.card_sales), (Decimal("150000"), Decimal("50000")))

    def test_change_is_given_only_for_the_cash_part(self):
        order = self.order()
        self.pay(order, cash="250000", debt="80000")
        self.assertEqual(Order.objects.get(pk=order.pk).change, Decimal("50000"))

    def test_debt_needs_guest_name_and_phone(self):
        order = self.order(name="", phone="")
        self.pay(order, debt_name="", debt_phone="")
        self.assertEqual(Order.objects.get(pk=order.pk).status, Order.Status.OPEN)
        self.assertFalse(DebtEntry.objects.exists())

    def test_not_enough_cash_for_the_rest(self):
        order = self.order()
        self.pay(order, cash="100000", debt="80000")
        self.assertEqual(Order.objects.get(pk=order.pk).status, Order.Status.OPEN)

    def test_whole_bill_in_debt_must_use_debt_method(self):
        order = self.order()
        self.pay(order, cash="", debt="280000")
        self.assertEqual(Order.objects.get(pk=order.pk).status, Order.Status.OPEN)

    def test_debt_bigger_than_bill_is_refused(self):
        order = self.order()
        self.pay(order, cash="", card="100000", debt="200000")
        self.assertEqual(Order.objects.get(pk=order.pk).status, Order.Status.OPEN)

    def test_debt_part_is_refused_when_debt_is_switched_off(self):
        cafe = CafeSettings.get()
        cafe.accept_debt = False
        cafe.save()
        order = self.order()
        self.pay(order)
        self.assertEqual(Order.objects.get(pk=order.pk).status, Order.Status.OPEN)

    def test_mixed_without_debt_works_as_before(self):
        order = self.order()
        self.pay(order, cash="180000", card="100000", debt="")
        order = Order.objects.get(pk=order.pk)
        self.assertEqual(order.status, Order.Status.PAID)
        self.assertEqual((order.cash_amount, order.card_paid), (Decimal("180000"), Decimal("100000")))
        self.assertFalse(DebtEntry.objects.exists())

    def test_refund_removes_the_debt_and_returns_only_money_taken(self):
        order = services.pay_order(self.order(), self.cashier, "mixed", Decimal("200000"), Decimal(0), debt=Decimal("80000"))
        services.refund_order(order, self.cashier, "ошибка")
        self.assertEqual(Customer.objects.get().debt, Decimal(0))
        self.shift.refresh_from_db()
        self.assertEqual(self.shift.cash_refunds, Decimal("200000"))

    def test_receipt_shows_debt_line(self):
        order = self.order()
        self.pay(order)
        page = self.client.get(reverse("receipt", args=[order.id]))
        from core.templatetags.ui import num

        self.assertContains(page, num(80000))
        self.assertContains(page, "В долг")
