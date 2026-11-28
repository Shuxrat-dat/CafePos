"""Номер заказа за день: каждый день с 00:00 заказы снова идут с №1, внутренний номер (id) не меняется."""
import importlib
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from menu.models import Category, Dish
from printing import documents
from printing.models import Printer

from . import services
from .models import Hall, Order, OrderItem, Table

User = get_user_model()


class DayNumberTests(TestCase):
    def setUp(self):
        self.waiter = User.objects.create_user(username="aziz", password="pass12345", role="waiter")
        self.cashier = User.objects.create_user(username="kassa", password="pass12345", role="cashier")
        self.table = Table.objects.create(hall=Hall.objects.create(name="Zal"), number="2")
        self.dish = Dish.objects.create(category=Category.objects.create(name="Taom"), name="Shashlik", price=20000)

    def new_order(self):
        return Order.objects.create(table=self.table, waiter=self.waiter)

    def test_orders_are_numbered_from_one_each_day(self):
        first, second = self.new_order(), self.new_order()
        self.assertEqual((first.day_number, second.day_number), (1, 2))
        self.assertEqual(second.number, 2)
        # Наступил следующий день: счёт начинается заново
        tomorrow = timezone.now() + timedelta(days=1)
        with mock.patch("django.utils.timezone.now", return_value=tomorrow):
            self.assertEqual(self.new_order().day_number, 1)

    def test_number_is_shown_on_order_screen_and_kitchen_ticket(self):
        for _n in range(4):
            self.new_order()
        order = self.new_order()
        OrderItem.objects.create(order=order, dish=self.dish, quantity=1)
        self.client.force_login(self.waiter)
        panel = self.client.get(reverse("order_panel", args=[order.id])).content.decode()
        self.assertIn("№5", panel)
        ticket = documents.kitchen(order, list(order.items.all()), Printer(name="Oshxona"), False).decode("cp866", "replace")
        self.assertIn("#5", ticket)

    def test_part_of_a_split_bill_keeps_the_order_number(self):
        services.open_shift(self.cashier, initial_cash=Decimal(0))
        self.new_order()
        order = self.new_order()
        a = OrderItem.objects.create(order=order, dish=self.dish, quantity=2, status=OrderItem.Status.SERVED)
        part = services.pay_order(order, self.cashier, Order.PaymentMethod.CASH, None, None, selected={a.id: 1})
        self.assertNotEqual(part.id, order.id)
        self.assertEqual(part.day_number, order.day_number)

    def test_migration_numbers_existing_orders_per_day(self):
        orders = [self.new_order() for _n in range(3)]
        Order.objects.update(day_number=None)
        yesterday = timezone.now() - timedelta(days=1)
        Order.objects.filter(id=orders[0].id).update(created_at=yesterday)
        migration = importlib.import_module("pos.migrations.0021_order_day_number")
        migration.number_existing_orders(django_apps, None)
        numbers = dict(Order.objects.values_list("id", "day_number"))
        self.assertEqual([numbers[o.id] for o in orders], [1, 1, 2])


class ReceiptLinesTests(TestCase):
    """Чек гостя: чай добавляли трижды по одному — в чеке одна строка «3 x Choy», сумма та же."""

    setUp = DayNumberTests.setUp
    new_order = DayNumberTests.new_order

    def test_same_dishes_are_one_line_on_the_guest_receipt(self):
        tea = Dish.objects.create(category=self.dish.category, name="Choy", price=3000)
        fish = Dish.objects.create(category=self.dish.category, name="Baliq", price=100000, sold_by_weight=True)
        order = self.new_order()
        for _n in range(3):
            OrderItem.objects.create(order=order, dish=tea, quantity=1, status=OrderItem.Status.QUEUED)
        OrderItem.objects.create(order=order, dish=self.dish, quantity=2)
        OrderItem.objects.create(order=order, dish=tea, quantity=1, takeaway=True)
        OrderItem.objects.create(order=order, dish=fish, quantity=1, weight_grams=500)
        OrderItem.objects.create(order=order, dish=fish, quantity=1, weight_grams=700)
        from .models import receipt_lines

        items = list(order.items.select_related("dish").prefetch_related("modifiers").order_by("id"))
        lines = receipt_lines(items)
        summary = [(line.dish.name, line.quantity, line.takeaway) for line in lines if not line.by_weight]
        self.assertEqual(summary, [("Choy", 3, False), ("Shashlik", 2, False), ("Choy", 1, True)])
        self.assertEqual(len([line for line in lines if line.by_weight]), 2)
        self.assertEqual(sum(line.line_total for line in lines), sum(i.line_total for i in items))
        self.assertEqual(OrderItem.objects.filter(order=order, dish=tea, takeaway=False).count(), 3)  # в базе ничего не склеилось

        ticket = documents.receipt(order, Printer(name="Kassa"), precheck=True).decode("cp866", "replace")
        self.assertIn("3 x Choy", ticket)
        self.assertEqual(ticket.count("x Choy"), 2)  # в зале 3 и с собой 1


class MoveItemsTableListTests(TestCase):
    """Окно «Перенести блюда»: просто список столов — занятый принимает блюда в свой заказ, свободный — новым заказом."""

    setUp = DayNumberTests.setUp

    def test_dialog_lists_tables_only(self):
        hall = self.table.hall
        busy = Table.objects.create(hall=hall, number="10")
        free = Table.objects.create(hall=hall, number="3")
        source = Order.objects.create(table=self.table, waiter=self.waiter)
        item = OrderItem.objects.create(order=source, dish=self.dish, quantity=2)
        other = Order.objects.create(table=busy, waiter=self.waiter)
        OrderItem.objects.create(order=other, dish=self.dish, quantity=1)
        Order.objects.create(order_type=Order.OrderType.TAKEAWAY, waiter=self.waiter).items.create(dish=self.dish)
        self.client.force_login(self.cashier)
        url = reverse("move_items", args=[source.id])
        page = self.client.get(url).content.decode()
        self.assertNotIn("<optgroup", page)
        self.assertIn(f'value="order:{other.id}"', page)
        self.assertIn(f'value="table:{free.id}"', page)
        self.assertNotIn(f'value="table:{self.table.id}"', page)  # свой стол не предлагается
        self.assertLess(page.index(f'value="table:{free.id}"'), page.index(f'value="order:{other.id}"'))  # 3 раньше 10
        self.client.post(url, {f"qty_{item.id}": "1", "target": f"order:{other.id}"})
        self.assertEqual(sum(i.quantity for i in other.items.all()), 2)
