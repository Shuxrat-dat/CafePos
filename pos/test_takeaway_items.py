"""Гость за столом берёт блюдо с собой: блюдо помечено, на кухне его упакуют, обслуживание на него не начисляется."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from menu.models import Category, Dish
from printing.models import Printer, PrintJob

from . import services
from .models import Hall, Order, OrderItem, Table
from .testing import open_shift_for_tests
from .views import split_totals

User = get_user_model()


class TakeawayItemTests(TestCase):
    def setUp(self):
        self.waiter = User.objects.create_user(username="aziz", password="pass12345", role="waiter")
        self.client.force_login(self.waiter)
        table = Table.objects.create(hall=Hall.objects.create(name="Zal"), number="3")
        food = Category.objects.create(name="Taom")
        self.shashlik = Dish.objects.create(category=food, name="Shashlik", price=20000)
        self.somsa = Dish.objects.create(category=food, name="Somsa", price=10000)
        self.order = Order.objects.create(table=table, waiter=self.waiter, service_percent=Decimal(10))
        open_shift_for_tests()

    def add(self, dish, qty=1):
        self.client.post(reverse("add_item", args=[self.order.id, dish.id]), {"quantity": str(qty)})

    def toggle_mode(self):
        self.client.post(reverse("toggle_takeaway_mode", args=[self.order.id]))

    def test_no_service_on_takeaway_dishes(self):
        OrderItem.objects.create(order=self.order, dish=self.shashlik, quantity=5)  # 100 000 в зале
        OrderItem.objects.create(order=self.order, dish=self.somsa, quantity=3, takeaway=True)  # 30 000 с собой
        totals = self.order.totals
        self.assertEqual(totals["subtotal"], Decimal(130000))
        self.assertEqual(totals["service"], Decimal(10000))  # 10% только со 100 000
        self.assertEqual(totals["total"], Decimal(140000))

    def test_discount_is_shared_between_hall_and_takeaway(self):
        self.order.discount_kind, self.order.discount_value = Order.DiscountKind.PERCENT, Decimal(10)
        OrderItem.objects.create(order=self.order, dish=self.shashlik, quantity=5)
        OrderItem.objects.create(order=self.order, dish=self.somsa, quantity=5, takeaway=True)
        totals = self.order.totals
        self.assertEqual(totals["discount"], Decimal(15000))
        self.assertEqual(totals["service"], Decimal(9000))  # 10% с (100 000 − 10%)

    def test_mode_button_marks_new_dishes_and_they_do_not_merge_with_hall_ones(self):
        self.add(self.shashlik, 5)
        self.toggle_mode()
        self.add(self.shashlik, 3)
        self.toggle_mode()
        self.add(self.shashlik, 1)
        lines = list(self.order.items.order_by("id").values_list("quantity", "takeaway"))
        self.assertEqual(lines, [(6, False), (3, True)])
        panel = self.client.get(reverse("order_panel", args=[self.order.id])).content.decode()
        self.assertIn(reverse("toggle_takeaway_mode", args=[self.order.id]), panel)

    def test_line_can_be_switched_before_sending(self):
        item = OrderItem.objects.create(order=self.order, dish=self.somsa, quantity=2)
        self.client.post(reverse("toggle_item_takeaway", args=[self.order.id, item.id]))
        item.refresh_from_db()
        self.assertTrue(item.takeaway)
        item.status = OrderItem.Status.QUEUED
        item.save()
        response = self.client.post(reverse("toggle_item_takeaway", args=[self.order.id, item.id]))
        self.assertEqual(response.status_code, 404)  # после отправки на кухню пометку не меняют

    def test_kitchen_ticket_says_takeaway(self):
        kitchen = Printer.objects.create(name="Shashlik", host="192.168.1.51", prints_receipts=False, prints_kitchen=True)
        OrderItem.objects.create(order=self.order, dish=self.shashlik, quantity=3, takeaway=True)
        with self.captureOnCommitCallbacks(execute=True):
            services.send_to_kitchen(self.order)
        text = bytes(PrintJob.objects.get(printer=kitchen).data).decode("cp866", errors="replace")
        self.assertRegex(text, r"Shashlik\s+3\s")
        self.assertIn(">> С СОБОЙ", text)

    def test_split_payment_counts_takeaway_part_without_service(self):
        totals = split_totals(self.order, Decimal(50000), Decimal(20000))
        self.assertEqual(totals["service"], Decimal(3000))
        self.assertEqual(totals["total"], Decimal(53000))

    def test_waiter_has_table_button_instead_of_payment(self):
        OrderItem.objects.create(order=self.order, dish=self.shashlik, quantity=1)
        panel = self.client.get(reverse("order_panel", args=[self.order.id])).content.decode()
        self.assertIn(reverse("move_form", args=[self.order.id]), panel)
        self.assertNotIn(reverse("pay", args=[self.order.id]), panel)

    def test_waiter_moves_guests_to_a_free_table_but_not_to_a_busy_one(self):
        hall = self.order.table.hall
        free = Table.objects.create(hall=hall, number="7")
        busy = Table.objects.create(hall=hall, number="8")
        other = Order.objects.create(table=busy, waiter=self.waiter)
        OrderItem.objects.create(order=other, dish=self.somsa, quantity=1)
        OrderItem.objects.create(order=self.order, dish=self.shashlik, quantity=2)
        form = self.client.get(reverse("move_form", args=[self.order.id])).content.decode()
        self.assertIn(reverse("move_order", args=[self.order.id, free.id]), form)
        self.assertNotIn(reverse("move_order", args=[self.order.id, busy.id]), form)
        self.client.post(reverse("move_order", args=[self.order.id, busy.id]))
        self.order.refresh_from_db()
        self.assertEqual(self.order.table.number, "3")
        self.client.post(reverse("move_order", args=[self.order.id, free.id]))
        self.order.refresh_from_db()
        self.assertEqual(self.order.table, free)
        self.assertEqual(self.order.items.count(), 1)

    def test_takeaway_order_can_be_seated_at_a_table(self):
        order = Order.objects.create(waiter=self.waiter, order_type=Order.OrderType.TAKEAWAY)
        free = Table.objects.create(hall=self.order.table.hall, number="9")
        self.client.post(reverse("move_order", args=[order.id, free.id]))
        order.refresh_from_db()
        self.assertEqual((order.order_type, order.table), (Order.OrderType.DINE_IN, free))

    def test_closed_order_is_not_moved(self):
        free = Table.objects.create(hall=self.order.table.hall, number="5")
        self.order.status = Order.Status.PAID
        self.order.save()
        self.client.post(reverse("move_order", args=[self.order.id, free.id]))
        self.order.refresh_from_db()
        self.assertEqual(self.order.table.number, "3")

    def test_takeaway_order_has_no_mode_button(self):
        order = Order.objects.create(waiter=self.waiter, order_type=Order.OrderType.TAKEAWAY)
        panel = self.client.get(reverse("order_panel", args=[order.id])).content.decode()
        self.assertNotIn("takeaway-mode", panel)
