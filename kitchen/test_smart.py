from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from menu.models import Category, Dish
from pos.models import Hall, Order, OrderItem, Table

from .views import READY_WARN_MINUTES, _tickets_context

User = get_user_model()


class SmartKitchenTests(TestCase):
    def setUp(self):
        self.cook = User.objects.create_user(username="cook_s", password="pass12345", role="cook")
        self.waiter = User.objects.create_user(username="waiter_s", password="pass12345", role="waiter")
        hall = Hall.objects.create(name="Зал")
        self.table = Table.objects.create(hall=hall, number="1")
        category = Category.objects.create(name="Горячее")
        self.fast = Dish.objects.create(category=category, name="Салат", price=10000, prep_minutes=3)
        self.slow = Dish.objects.create(category=category, name="Плов", price=30000, prep_minutes=20)
        self.plain = Dish.objects.create(category=category, name="Чай", price=5000)

    def _order(self, dish, status, minutes_ago, ready_minutes_ago=None):
        order = Order.objects.create(table=self.table, waiter=self.waiter)
        sent = timezone.now() - timedelta(minutes=minutes_ago)
        ready = timezone.now() - timedelta(minutes=ready_minutes_ago) if ready_minutes_ago is not None else None
        OrderItem.objects.create(order=order, dish=dish, quantity=1, status=status, sent_at=sent, ready_at=ready)
        return order

    def _find(self, order):
        columns = _tickets_context()["columns"]
        for tickets in columns.values():
            for ticket in tickets:
                if ticket.id == order.id:
                    return ticket
        self.fail("ticket not on board")

    def test_fast_dish_turns_late_sooner_than_slow_one(self):
        fast = self._order(self.fast, OrderItem.Status.COOKING, 7)
        slow = self._order(self.slow, OrderItem.Status.COOKING, 7)
        self.assertEqual(self._find(fast).level, "late")
        self.assertEqual(self._find(slow).level, "ok")

    def test_dish_without_prep_time_uses_global_threshold(self):
        order = self._order(self.plain, OrderItem.Status.COOKING, 1)
        self.assertEqual(self._find(order).level, "ok")

    def test_ready_dish_cools_down(self):
        fresh = self._order(self.plain, OrderItem.Status.READY, 5, ready_minutes_ago=0)
        cold = self._order(self.plain, OrderItem.Status.READY, 15, ready_minutes_ago=READY_WARN_MINUTES * 2 + 1)
        self.assertEqual(self._find(fresh).level, "ok")
        self.assertEqual(self._find(cold).level, "late")
        self.assertGreaterEqual(self._find(cold).ready_wait, READY_WARN_MINUTES * 2)

    def test_partly_ready_order_shows_progress(self):
        order = self._order(self.slow, OrderItem.Status.COOKING, 2)
        OrderItem.objects.create(
            order=order, dish=self.fast, quantity=1, status=OrderItem.Status.READY,
            sent_at=timezone.now(), ready_at=timezone.now(),
        )
        ticket = self._find(order)
        self.assertTrue(ticket.partly_ready)
        self.assertEqual((ticket.ready_count, ticket.total_count), (1, 2))

    def test_board_renders_progress_and_sound_toggle(self):
        order = self._order(self.slow, OrderItem.Status.COOKING, 2)
        OrderItem.objects.create(order=order, dish=self.fast, quantity=1, status=OrderItem.Status.READY)
        self.client.force_login(self.cook)
        response = self.client.get(reverse("kitchen_board"))
        self.assertContains(response, "Готово 1 из 2")
        self.assertContains(response, 'id="kds-sound"')
        self.assertContains(response, f'data-ticket="{order.id}"')
