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


class MixedClickTests(ClickPaymentTests):
    """«Aralash»: наличные + карта + Click. Click попадает в отчёт «Click», в кассу не поступает."""

    def pay(self, **data):
        self.client.force_login(self.cashier)
        return self.client.post(reverse("pay", args=[self.order.id]), {"method": "mixed", **data})

    def test_card_click_and_cash_are_split_into_their_own_totals(self):
        self.pay(card="20000", click="25000", received="20000")
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)
        self.assertEqual((self.order.card_paid, self.order.click_paid, self.order.cash_amount),
                         (Decimal("20000"), Decimal("25000"), Decimal("15000")))
        self.assertEqual(self.order.change, Decimal("5000"))
        payment = Payment.objects.get(order=self.order)
        self.assertEqual((payment.card_amount, payment.click_amount, payment.cash_amount),
                         (Decimal("20000"), Decimal("25000"), Decimal("15000")))
        self.assertEqual(self.shift.card_sales, Decimal("20000"))
        self.assertEqual(self.shift.click_sales, Decimal("25000"))
        self.assertEqual(self.shift.cash_sales, Decimal("15000"))
        self.assertEqual(self.shift.total_revenue, Decimal("60000"))
        self.assertEqual(self.shift.expected_cash, Decimal("115000"))

    def test_click_and_cash_without_card(self):
        self.pay(click="40000")
        self.order.refresh_from_db()
        self.assertEqual((self.order.click_paid, self.order.cash_amount, self.order.card_paid),
                         (Decimal("40000"), Decimal("20000"), Decimal("0")))

    def test_parts_bigger_than_the_bill_are_refused(self):
        response = self.pay(card="30000", click="40000")
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.OPEN)
        self.assertContains(response, "Click")
        self.assertFalse(Payment.objects.filter(order=self.order).exists())

    def test_reports_count_mixed_click_in_click(self):
        self.pay(card="10000", click="30000")
        self.client.force_login(User.objects.create_user(username="egasi_c", password="pass12345", role="owner"))
        response = self.client.get(reverse("reports"))
        self.assertEqual(response.context["click_total"], Decimal("30000"))
        self.assertEqual(response.context["card_total"], Decimal("10000"))
