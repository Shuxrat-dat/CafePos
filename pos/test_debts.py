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


class GuestDebtTests(TestCase):
    def setUp(self):
        self.cashier = User.objects.create_user(username="cashier_debt", password="pass12345", role="cashier")
        self.waiter = User.objects.create_user(username="waiter_debt", password="pass12345", role="waiter")
        settings = CafeSettings.get()
        settings.accept_debt = True
        settings.service_percent = 0
        settings.save()
        category = Category.objects.create(name="Горячее")
        self.dish = Dish.objects.create(category=category, name="Плов", price=Decimal("50000"))
        self.shift = services.open_shift(self.cashier, initial_cash=Decimal("100000"))

    def order(self, name="Акмаль", phone="+998 90 123 45 67"):
        order = services.create_order(self.waiter, Order.OrderType.TAKEAWAY)
        OrderItem.objects.create(order=order, dish=self.dish, quantity=2, price=self.dish.price, status=OrderItem.Status.SERVED)
        order.customer_name, order.customer_phone = name, phone
        order.save()
        return order

    def test_debt_needs_name_and_phone(self):
        order = self.order(name="", phone="")
        with self.assertRaises(services.PaymentError):
            services.pay_order(order, self.cashier, "debt", None, None)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.OPEN)
        self.assertFalse(DebtEntry.objects.exists())

    def test_order_in_debt_is_paid_without_cash(self):
        expected_before = self.shift.expected_cash
        order = services.pay_order(self.order(), self.cashier, "debt", None, None)
        self.assertEqual(order.status, Order.Status.PAID)
        customer = Customer.objects.get(phone="901234567")
        self.assertEqual(customer.debt, Decimal("100000"))
        self.shift.refresh_from_db()
        self.assertEqual(self.shift.expected_cash, expected_before)
        self.assertEqual(order.cash_amount, 0)

    def test_repay_in_cash_goes_to_cashbox(self):
        services.pay_order(self.order(), self.cashier, "debt", None, None)
        customer = Customer.objects.get()
        services.repay_debt(customer, Decimal("60000"), "cash", self.cashier)
        self.assertEqual(customer.debt, Decimal("40000"))
        self.assertEqual(self.shift.debt_cash_repaid, Decimal("60000"))
        self.assertEqual(self.shift.expected_cash, Decimal("160000"))

    def test_repay_by_card_does_not_change_cash(self):
        services.pay_order(self.order(), self.cashier, "debt", None, None)
        services.repay_debt(Customer.objects.get(), Decimal("100000"), "card", self.cashier)
        self.assertEqual(self.shift.expected_cash, Decimal("100000"))
        self.assertEqual(Customer.objects.get().debt, 0)

    def test_cannot_repay_more_than_debt(self):
        services.pay_order(self.order(), self.cashier, "debt", None, None)
        with self.assertRaises(services.PaymentError):
            services.repay_debt(Customer.objects.get(), Decimal("100001"), "cash", self.cashier)

    def test_refund_of_debt_order_cancels_debt(self):
        order = services.pay_order(self.order(), self.cashier, "debt", None, None)
        services.refund_order(order, self.cashier, "Ошибка кассира", return_to_stock=False)
        self.assertEqual(Customer.objects.get().debt, 0)
        self.assertEqual(self.shift.expected_cash, Decimal("100000"))

    def test_debt_split_is_rejected(self):
        order = self.order()
        item = order.items.get()
        with self.assertRaises(services.PaymentError):
            services.pay_order(order, self.cashier, "debt", None, None, selected={item.id: 1})

    def test_pay_view_saves_guest_and_writes_debt(self):
        order = self.order(name="", phone="")
        self.client.force_login(self.cashier)
        response = self.client.post(reverse("pay", args=[order.id]), {
            "method": "debt", "debt_name": "Шерзод", "debt_phone": "998 93 555 44 33",
            "idempotency_key": str(uuid.uuid4()),
        })
        self.assertRedirects(response, reverse("receipt", args=[order.id]), fetch_redirect_response=False)
        self.assertEqual(Customer.objects.get(phone="935554433").debt, Decimal("100000"))

    def test_debt_method_hidden_when_disabled(self):
        settings = CafeSettings.get()
        settings.accept_debt = False
        settings.save()
        order = self.order()
        self.client.force_login(self.cashier)
        self.client.post(reverse("pay", args=[order.id]), {"method": "debt", "idempotency_key": str(uuid.uuid4())})
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.OPEN)

    def test_debts_pages_and_access(self):
        services.pay_order(self.order(), self.cashier, "debt", None, None)
        customer = Customer.objects.get()
        self.client.force_login(self.cashier)
        self.assertContains(self.client.get(reverse("debts")), "Акмаль")
        response = self.client.post(reverse("debt_detail", args=[customer.id]), {"amount": "100000", "method": "cash"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(customer.debt, 0)
        self.client.force_login(self.waiter)
        self.assertEqual(self.client.get(reverse("debts")).status_code, 403)
