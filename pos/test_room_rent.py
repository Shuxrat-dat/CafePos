from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import CafeSettings
from menu.models import Category, Dish
from pos import services
from pos.models import Hall, Order, OrderItem, Table

User = get_user_model()


class RoomRentTests(TestCase):
    """Кабины: аренда по времени добавляется к счёту, начатый шаг считается целиком, при оплате сумма фиксируется."""

    def setUp(self):
        settings = CafeSettings.get()
        settings.service_percent = 0
        settings.save()
        self.cashier = User.objects.create_user(username="c_room", password="pass12345", role="cashier")
        self.owner = User.objects.create_user(username="o_room", password="pass12345", role="owner")
        hall = Hall.objects.create(name="Кабины")
        self.room = Table.objects.create(hall=hall, number="K1", hourly_price=Decimal("60000"), billing_step_minutes=30)
        self.plain = Table.objects.create(hall=hall, number="T1")
        category = Category.objects.create(name="Горячее")
        self.dish = Dish.objects.create(category=category, name="Плов", price=Decimal("50000"))
        self.shift = services.open_shift(self.cashier, initial_cash=Decimal("0"))

    def order(self, table, minutes_ago):
        order = services.create_order(self.cashier, Order.OrderType.DINE_IN, table)
        OrderItem.objects.create(order=order, dish=self.dish, quantity=1, price=self.dish.price, status=OrderItem.Status.SERVED)
        Order.objects.filter(pk=order.pk).update(created_at=timezone.now() - timedelta(minutes=minutes_ago))
        return Order.objects.get(pk=order.pk)

    def test_started_step_is_charged_in_full(self):
        order = self.order(self.room, 40)
        minutes, amount = order.room_live()
        self.assertEqual((minutes, amount), (60, Decimal("60000")))
        self.assertEqual(order.subtotal, Decimal("110000"))

    def test_minimum_is_one_step(self):
        order = self.order(self.room, 1)
        self.assertEqual(order.room_live(), (30, Decimal("30000")))

    def test_plain_table_and_takeaway_have_no_rent(self):
        self.assertEqual(self.order(self.plain, 120).room_amount, 0)
        takeaway = services.create_order(self.cashier, Order.OrderType.TAKEAWAY)
        self.assertEqual(takeaway.room_amount, 0)

    def test_payment_freezes_rent(self):
        order = self.order(self.room, 40)
        paid = services.pay_order(order, self.cashier, "card", None, None)
        self.assertEqual(paid.room_charge, Decimal("60000"))
        self.assertEqual(paid.room_minutes, 60)
        self.assertEqual(paid.total, Decimal("110000"))
        # Прошло ещё время — оплаченный заказ не меняется
        Order.objects.filter(pk=paid.pk).update(created_at=timezone.now() - timedelta(hours=10))
        self.assertEqual(Order.objects.get(pk=paid.pk).total, Decimal("110000"))

    def test_rent_goes_to_shift_revenue(self):
        order = self.order(self.room, 40)
        services.pay_order(order, self.cashier, "cash", Decimal("110000"), None)
        self.shift.refresh_from_db()
        self.assertEqual(self.shift.cash_sales, Decimal("110000"))

    def test_table_form_validates_and_saves_rent(self):
        self.client.force_login(self.owner)
        url = reverse("table_edit", args=[self.plain.id])
        bad = self.client.post(url, {"hall": self.plain.hall_id, "number": "T1", "seats": 4,
                                     "hourly_price": "1000", "billing_step_minutes": "1"})
        self.assertEqual(bad.status_code, 200)
        self.client.post(url, {"hall": self.plain.hall_id, "number": "T1", "seats": 4,
                               "hourly_price": "45000", "billing_step_minutes": "15"})
        self.plain.refresh_from_db()
        self.assertEqual((self.plain.hourly_price, self.plain.billing_step_minutes), (Decimal("45000"), 15))

    def test_order_panel_shows_rent_line(self):
        order = self.order(self.room, 40)
        self.client.force_login(self.cashier)
        html = self.client.get(reverse("order_panel", args=[order.id])).content.decode()
        self.assertIn("60 000", html.replace("\xa0", " "))
