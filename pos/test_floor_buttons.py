from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from menu.models import Category, Dish

from .models import Order, OrderItem
from . import services

User = get_user_model()


class FloorButtonsTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="owner", password="x", role="owner")
        self.waiter = User.objects.create_user(username="w1", password="x", role="waiter")
        self.other = User.objects.create_user(username="w2", password="x", role="waiter")
        category = Category.objects.create(name="Горячее")
        self.dish = Dish.objects.create(category=category, name="Плов", price=40000)

    def _order(self, user, status):
        order = services.create_order(user, Order.OrderType.TAKEAWAY)
        order.items.create(dish=self.dish, quantity=1, price=self.dish.price, status=status)
        return order

    def test_quick_add_without_order_never_touches_foreign_order(self):
        foreign = self._order(self.other, OrderItem.Status.NEW)
        self.client.force_login(self.waiter)
        self.client.post(reverse("quick_add", args=[self.dish.id]))
        self.assertEqual(foreign.items.count(), 1)
        mine = Order.objects.get(waiter=self.waiter)
        self.assertEqual(mine.items.count(), 1)

    def test_quick_add_to_foreign_order_is_forbidden(self):
        foreign = self._order(self.other, OrderItem.Status.NEW)
        self.client.force_login(self.waiter)
        response = self.client.post(reverse("quick_add", args=[self.dish.id]), {"order_id": foreign.id})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(foreign.items.count(), 1)

    def test_floor_hides_foreign_orders_from_waiter(self):
        self._order(self.other, OrderItem.Status.NEW)
        self.client.force_login(self.waiter)
        response = self.client.get(reverse("floor"))
        self.assertEqual(len(response.context["current_orders"]), 0)

    def test_served_order_offers_payment_instead_of_dead_stage_button(self):
        order = self._order(self.owner, OrderItem.Status.SERVED)
        self.client.force_login(self.owner)
        response = self.client.get(reverse("floor"))
        card = response.context["current_orders"][0]
        self.assertTrue(card.ready_to_pay)
        self.assertContains(response, reverse("pay", args=[order.id]))

    def test_stage_button_advances_new_to_cooking(self):
        order = self._order(self.owner, OrderItem.Status.NEW)
        self.client.force_login(self.owner)
        self.client.post(reverse("toggle_order_stage", args=[order.id]))
        self.assertEqual(order.items.get().status, OrderItem.Status.COOKING)
