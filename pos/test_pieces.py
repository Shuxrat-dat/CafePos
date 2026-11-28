"""Сигареты: пачкой или поштучно. Цена за пачку, штука — её доля (25 000 / 20 = 1 250)."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from menu.models import Category, Dish
from printing import documents
from printing.models import Printer

from .models import Hall, Order, OrderItem, Table

User = get_user_model()


class PieceSaleTests(TestCase):
    def setUp(self):
        self.cashier = User.objects.create_user(username="kassa_pc", password="pass12345", role="cashier")
        self.waiter = User.objects.create_user(username="ofi_pc", password="pass12345", role="waiter")
        table = Table.objects.create(hall=Hall.objects.create(name="Zal"), number="1")
        self.dish = Dish.objects.create(category=Category.objects.create(name="Sigaret"), name="Kent", price=25000,
                                        pack_size=20)
        self.order = Order.objects.create(table=table, waiter=self.waiter)
        self.client.force_login(self.waiter)
        self.add_url = reverse("add_item", args=[self.order.id, self.dish.id])

    def test_dish_window_offers_pack_or_pieces(self):
        page = self.client.get(reverse("item_form", args=[self.order.id, self.dish.id])).content.decode()
        self.assertIn('value="piece"', page)
        self.assertIn("1 250", page)  # цена штуки: 25 000 / 20

    def test_pack_is_a_normal_line(self):
        self.client.post(self.add_url, {"sell": "pack", "quantity": "2"})
        item = self.order.items.get()
        self.assertEqual((item.quantity, item.by_weight, item.line_total), (2, False, Decimal(50000)))

    def test_pieces_are_one_line_priced_from_the_pack(self):
        self.client.post(self.add_url, {"sell": "piece", "quantity": "3"})
        self.client.post(self.add_url, {"sell": "piece", "quantity": "2"})
        item = self.order.items.get()
        self.assertTrue(item.is_pieces)
        self.assertEqual((item.weight_grams, item.pack_size), (5, 20))
        self.assertEqual(item.line_total, Decimal(6250))
        panel = self.client.get(reverse("order_panel", args=[self.order.id])).content.decode()
        self.assertIn("5 шт", panel)
        ticket = documents.receipt(self.order, Printer(name="Kassa"), precheck=True).decode("cp866", "replace")
        self.assertIn("Kent 5 шт", ticket)

    def test_twenty_pieces_make_a_pack(self):
        self.client.post(self.add_url, {"sell": "piece", "quantity": "23"})
        lines = sorted((i.by_weight, i.quantity, i.weight_grams) for i in self.order.items.all())
        self.assertEqual(lines, [(False, 1, None), (True, 1, 3)])

    def test_pieces_are_edited_before_sending_only(self):
        self.client.post(self.add_url, {"sell": "piece", "quantity": "3"})
        item = self.order.items.get()
        url = reverse("edit_item", args=[self.order.id, item.id])
        self.client.post(url, {"sell": "piece", "quantity": "4"})
        item.refresh_from_db()
        self.assertEqual(item.weight_grams, 4)
        item.status = OrderItem.Status.QUEUED
        item.save()
        self.assertEqual(self.client.post(url, {"sell": "piece", "quantity": "1"}).status_code, 404)
        item.refresh_from_db()
        self.assertEqual(item.weight_grams, 4)

    def test_moving_pieces_keeps_the_unit(self):
        from . import services

        self.client.post(self.add_url, {"sell": "piece", "quantity": "3"})
        item = self.order.items.get()
        other = Order.objects.create(table=Table.objects.create(hall=Hall.objects.first(), number="2"), waiter=self.waiter)
        OrderItem.objects.create(order=other, dish=self.dish, quantity=1)
        services.move_items(self.order, other, {item.id: 1})
        moved = other.items.get(by_weight=True)
        self.assertEqual((moved.measure, moved.pack_size, moved.weight_grams, moved.line_total), ("pc", 20, 3, Decimal(3750)))
