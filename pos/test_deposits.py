import uuid
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import CafeSettings
from menu.models import Category, Dish
from pos import services
from pos.models import Deposit, Hall, Order, OrderItem, Table

User = get_user_model()


class DepositTests(TestCase):
    """Депозит брони: принят в смену, при приходе гостя вычитается из счёта, иначе возвращается или остаётся заведению."""

    def setUp(self):
        settings = CafeSettings.get()
        settings.service_percent = 0
        settings.save()
        self.cashier = User.objects.create_user(username="c_dep", password="pass12345", role="cashier")
        hall = Hall.objects.create(name="Зал")
        self.table = Table.objects.create(hall=hall, number="5")
        category = Category.objects.create(name="Горячее")
        self.dish = Dish.objects.create(category=category, name="Плов", price=Decimal("50000"))
        self.shift = services.open_shift(self.cashier, initial_cash=Decimal("100000"))
        self.client.force_login(self.cashier)

    def reserve(self, **extra):
        data = {"name": "Азиз", "time": "19:00", "deposit": "80000", "deposit_method": "cash"}
        data.update(extra)
        return self.client.post(reverse("reserve_table", args=[self.table.id]), data)

    def seat_guests(self, qty=2):
        self.client.post(reverse("open_table", args=[self.table.id]))
        order = Order.objects.get(table=self.table, status=Order.Status.OPEN)
        OrderItem.objects.create(order=order, dish=self.dish, quantity=qty, price=self.dish.price, status=OrderItem.Status.SERVED)
        return order

    def expected_cash(self):
        self.shift.refresh_from_db()
        return self.shift.expected_cash

    def test_reserve_with_deposit_goes_to_shift_cash(self):
        self.reserve()
        deposit = Deposit.objects.get()
        self.assertEqual((deposit.amount, deposit.status, deposit.shift), (Decimal("80000"), "held", self.shift))
        self.assertEqual(self.expected_cash(), Decimal("180000"))

    def test_card_deposit_does_not_touch_cash(self):
        self.reserve(deposit_method="card")
        self.assertEqual(self.expected_cash(), Decimal("100000"))

    def test_deposit_needs_open_shift(self):
        services.close_shift(self.shift, Decimal("100000"), "", self.cashier)
        self.reserve()
        self.assertFalse(Deposit.objects.exists())
        self.table.refresh_from_db()
        self.assertFalse(self.table.is_reserved)

    def test_bad_deposit_is_rejected_and_table_not_reserved(self):
        self.reserve(deposit="abc")
        self.table.refresh_from_db()
        self.assertFalse(self.table.is_reserved)
        self.assertFalse(Deposit.objects.exists())

    def test_guests_arrive_deposit_reduces_due(self):
        self.reserve()
        order = self.seat_guests(qty=3)  # 150 000
        order.refresh_from_db()
        self.assertEqual(order.deposit_applied, Decimal("80000"))
        self.assertEqual(order.amount_due, Decimal("70000"))
        self.assertEqual(Deposit.objects.get().status, "applied")

    def test_paying_charges_only_the_rest_and_cash_adds_up(self):
        self.reserve()
        order = self.seat_guests(qty=3)
        paid = services.pay_order(order, self.cashier, "cash", Decimal("70000"), None)
        self.assertEqual(paid.total, Decimal("150000"))
        self.assertEqual(paid.cash_amount, Decimal("70000"))
        self.shift.refresh_from_db()
        self.assertEqual(self.shift.cash_sales, Decimal("70000"))
        # В кассе: размен + депозит + доплата
        self.assertEqual(self.shift.expected_cash, Decimal("250000"))

    def test_pay_view_asks_only_the_rest(self):
        self.reserve()
        order = self.seat_guests(qty=3)
        page = self.client.get(reverse("pay", args=[order.id]))
        self.assertEqual(page.context["due_total"], Decimal("70000"))
        response = self.client.post(reverse("pay", args=[order.id]), {
            "method": "cash", "received": "70000", "idempotency_key": str(uuid.uuid4()),
        })
        self.assertEqual(response.status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PAID)

    def test_deposit_larger_than_bill_is_refunded(self):
        self.reserve(deposit="200000")
        order = self.seat_guests(qty=2)  # 100 000
        paid = services.pay_order(order, self.cashier, "cash", Decimal("0"), None)
        self.assertEqual(paid.deposit_applied, Decimal("100000"))
        self.assertEqual(paid.amount_due, 0)
        deposit = Deposit.objects.get()
        self.assertEqual(deposit.refund_amount, Decimal("100000"))
        # размен 100 000 + депозит 200 000 − возврат остатка 100 000
        self.assertEqual(self.expected_cash(), Decimal("200000"))

    def test_clear_requires_choice_then_refunds(self):
        self.reserve()
        url = reverse("reserve_table", args=[self.table.id])
        self.client.post(url, {"action": "clear"})
        self.table.refresh_from_db()
        self.assertTrue(self.table.is_reserved)
        self.client.post(url, {"action": "clear", "deposit_action": "refund"})
        self.table.refresh_from_db()
        self.assertFalse(self.table.is_reserved)
        self.assertEqual(Deposit.objects.get().status, "refunded")
        self.assertEqual(self.expected_cash(), Decimal("100000"))

    def test_clear_keep_leaves_money_with_the_venue(self):
        self.reserve()
        self.client.post(reverse("reserve_table", args=[self.table.id]), {"action": "clear", "deposit_action": "keep"})
        self.assertEqual(Deposit.objects.get().status, "kept")
        self.assertEqual(self.expected_cash(), Decimal("180000"))

    def test_reservation_without_deposit_works_as_before(self):
        self.reserve(deposit="")
        self.table.refresh_from_db()
        self.assertTrue(self.table.is_reserved)
        self.assertFalse(Deposit.objects.exists())
