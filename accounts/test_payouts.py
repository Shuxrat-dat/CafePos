from decimal import Decimal
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from core.models import AuditLog, CafeSettings
from menu.models import Category, Dish
from pos import services
from pos.models import CashMovement, Hall, Order, OrderItem, Table
from reports.models import Expense

from . import payouts
from .models import SalaryPayment, User


class Base(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="owner_p", password="pass12345", role="owner")
        self.cashier = User.objects.create_user(username="kassa_p", password="pass12345", role="cashier")
        self.shoh = User.objects.create_user(username="shoh_p", password="pass12345", role="waiter", first_name="Shoh",
                                             sales_percent=Decimal("3"))
        self.ikrom = User.objects.create_user(username="ikrom_p", password="pass12345", role="waiter", first_name="Ikrom",
                                              sales_percent=Decimal("3"))
        cafe = CafeSettings.get()
        cafe.service_percent = Decimal("0")
        cafe.save()
        hall = Hall.objects.create(name="TEPA", service_percent=Decimal("10"))
        self.table = Table.objects.create(hall=hall, number="5")
        self.dish = Dish.objects.create(category=Category.objects.create(name="Горячее"), name="Шашлык",
                                        price=Decimal("280000"))
        self.shift = services.open_shift(self.cashier, 1_000_000)

    def open_order(self, waiter=None):
        order = services.create_order(waiter or self.shoh, Order.OrderType.DINE_IN, self.table)
        OrderItem.objects.create(order=order, dish=self.dish, quantity=2, price=self.dish.price)
        return order

    def paid(self, waiter=None):
        return services.pay_order(self.open_order(waiter), self.cashier, Order.PaymentMethod.CARD, None, None)


class AssignWaiterTests(Base):
    def test_cashier_passes_table_and_new_waiter_gets_the_share(self):
        order = self.open_order()
        self.client.force_login(self.cashier)
        self.assertEqual(self.client.get(reverse("assign_waiter", args=[order.id])).status_code, 200)
        self.client.post(reverse("assign_waiter", args=[order.id]), {"waiter_id": self.ikrom.id})
        order.refresh_from_db()
        self.assertEqual(order.waiter, self.ikrom)
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.WAITER_CHANGE).exists())
        paid = services.pay_order(order, self.cashier, Order.PaymentMethod.CARD, None, None)
        self.assertEqual(paid.waiter_earning, Decimal("16800"))
        self.assertEqual(payouts.balance(self.ikrom), Decimal("16800"))
        self.assertEqual(payouts.balance(self.shoh), Decimal("0"))

    def test_waiter_cannot_pass_tables(self):
        order = self.open_order()
        self.client.force_login(self.shoh)
        response = self.client.post(reverse("assign_waiter", args=[order.id]), {"waiter_id": self.ikrom.id})
        self.assertEqual(response.status_code, 403)
        order.refresh_from_db()
        self.assertEqual(order.waiter, self.shoh)

    def test_paid_order_cannot_be_reassigned(self):
        order = self.paid()
        self.client.force_login(self.cashier)
        response = self.client.post(reverse("assign_waiter", args=[order.id]), {"waiter_id": self.ikrom.id})
        self.assertEqual(response.status_code, 404)

    def test_button_is_on_cashier_order_screen_only(self):
        order = self.open_order()
        self.client.force_login(self.cashier)
        self.assertIn("data-assign-waiter", self.client.get(reverse("order_detail", args=[order.id])).content.decode())
        self.client.force_login(self.shoh)
        self.assertNotIn("data-assign-waiter", self.client.get(reverse("order_detail", args=[order.id])).content.decode())

    def test_table_map_has_waiter_filter_for_cashier(self):
        self.open_order()
        self.client.force_login(self.cashier)
        page = self.client.get(reverse("table_map"))
        self.assertEqual([w["user"] for w in page.context["floor_waiters"]], [self.shoh])
        self.assertIn(f'data-waiter="{self.shoh.id}"', page.content.decode())


class PayoutTests(Base):
    def test_payout_from_cash_reduces_balance_and_records_expense(self):
        self.paid()  # +16 800
        payouts.pay(self.shoh, "10 000", SalaryPayment.Kind.PAYOUT, self.owner, from_cash=True)
        self.assertEqual(payouts.balance(self.shoh), Decimal("6800"))
        self.assertEqual(Expense.objects.get().amount, Decimal("10000"))
        self.assertEqual(CashMovement.objects.get().kind, CashMovement.Kind.WITHDRAWAL)

    def test_payout_bigger_than_balance_needs_advance(self):
        self.paid()
        with self.assertRaises(payouts.PayoutError):
            payouts.pay(self.shoh, "50000", SalaryPayment.Kind.PAYOUT, self.owner)
        payouts.pay(self.shoh, "50000", SalaryPayment.Kind.ADVANCE, self.owner)
        self.assertEqual(payouts.balance(self.shoh), Decimal("-33200"))

    def test_from_cash_without_open_shift_fails_and_writes_nothing(self):
        self.paid()
        services.close_shift(self.shift, "1000000", user=self.cashier)
        with self.assertRaises(payouts.PayoutError):
            payouts.pay(self.shoh, "1000", SalaryPayment.Kind.PAYOUT, self.owner, from_cash=True)
        self.assertFalse(SalaryPayment.objects.exists())
        self.assertFalse(Expense.objects.exists())

    def test_bad_amounts(self):
        for bad in ("", "abc", "0", "-5"):
            with self.assertRaises(payouts.PayoutError):
                payouts.pay(self.shoh, bad, SalaryPayment.Kind.ADVANCE, self.owner)

    def test_owner_pays_from_payroll_page(self):
        self.paid()
        self.client.force_login(self.owner)
        row = next(r for r in self.client.get(reverse("payroll")).context["rows"] if r["user"] == self.shoh)
        self.assertEqual(row["balance"], Decimal("16800"))
        self.client.post(reverse("payroll"), {"action": "payout", "user_id": self.shoh.id, "amount": "16800",
                                              "kind": "payout", "from_cash": "1"})
        row = next(r for r in self.client.get(reverse("payroll")).context["rows"] if r["user"] == self.shoh)
        self.assertEqual((row["paid"], row["balance"]), (Decimal("16800"), Decimal("0")))

    def test_cashier_cannot_pay_salary(self):
        self.client.force_login(self.cashier)
        self.client.post(reverse("payroll"), {"action": "payout", "user_id": self.shoh.id, "amount": "1000", "kind": "advance"})
        self.assertFalse(SalaryPayment.objects.exists())

    def test_waiter_sees_balance_and_payouts(self):
        self.paid()
        payouts.pay(self.shoh, "6800", SalaryPayment.Kind.PAYOUT, self.owner)
        self.client.force_login(self.shoh)
        page = self.client.get(reverse("my_earnings"))
        self.assertEqual(page.context["balance"], Decimal("10000"))
        self.assertEqual(len(page.context["payments"]), 1)

    def test_shift_report_lists_waiters(self):
        from core import telegram

        self.paid()
        sent = []
        with mock.patch.object(telegram, "send", side_effect=lambda text, event: sent.append(text) or True):
            telegram.notify_shift_closed(self.shift)
        self.assertIn("Shoh", sent[0])
        self.assertIn("16 800", sent[0])
