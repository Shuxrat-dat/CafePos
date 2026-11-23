from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import CafeSettings
from menu.models import Category, Dish
from pos import services
from pos.models import Hall, Order, OrderItem, Payment, Table

User = get_user_model()


class ClickPaymentTests(TestCase):
    def setUp(self):
        cafe = CafeSettings.get()
        cafe.service_percent = Decimal("0")
        cafe.save()
        self.cashier = User.objects.create_user(username="kassa_click", password="pass12345", role="cashier")
        self.waiter = User.objects.create_user(username="waiter_click", password="pass12345", role="waiter")
        self.table = Table.objects.create(hall=Hall.objects.create(name="ZAL"), number="3")
        dish = Dish.objects.create(category=Category.objects.create(name="Чай"), name="Чай", price=Decimal("30000"))
        self.shift = services.open_shift(self.cashier, initial_cash=Decimal("100000"))
        self.order = services.create_order(self.waiter, Order.OrderType.DINE_IN, self.table)
        OrderItem.objects.create(order=self.order, dish=dish, quantity=2, price=dish.price, status=OrderItem.Status.SERVED)

    def test_click_is_offered_on_pay_screen(self):
        self.client.force_login(self.cashier)
        html = self.client.get(reverse("pay", args=[self.order.id])).content.decode()
        self.assertIn('value="click"', html)

    def test_click_payment_is_counted_apart_from_cash_and_card(self):
        self.client.force_login(self.cashier)
        self.client.post(reverse("pay", args=[self.order.id]), {"method": "click"})
        self.order.refresh_from_db()
        self.assertEqual((self.order.status, self.order.payment_method), (Order.Status.PAID, "click"))
        payment = Payment.objects.get(order=self.order)
        self.assertEqual((payment.amount, payment.cash_amount), (Decimal("60000"), Decimal("0")))
        self.assertEqual(self.shift.click_sales, Decimal("60000"))
        self.assertEqual(self.shift.card_sales, Decimal("0"))
        self.assertEqual(self.shift.cash_sales, Decimal("0"))
        self.assertEqual(self.shift.total_revenue, Decimal("60000"))
        self.assertEqual(self.shift.expected_cash, Decimal("100000"))  # в кассу наличные не пришли

    def test_refund_of_click_order_does_not_take_cash(self):
        paid = services.pay_order(self.order, self.cashier, Order.PaymentMethod.CLICK, None, None)
        services.refund_order(paid, self.cashier, "ошибка")
        refund = Payment.objects.get(order=paid, is_refund=True)
        self.assertEqual((refund.cash_amount, refund.card_amount), (Decimal("0"), Decimal("60000")))
        self.assertEqual(self.shift.expected_cash, Decimal("100000"))

    def test_click_can_be_switched_off(self):
        cafe = CafeSettings.get()
        cafe.accept_click = False
        cafe.save()
        self.assertNotIn("click", cafe.payment_methods)
