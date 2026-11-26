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
