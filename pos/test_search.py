from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from menu.models import Category, Dish

from . import services
from .models import Hall, Order, Table

User = get_user_model()


class GlobalSearchTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="owner", password="x", role="owner")
        self.w1 = User.objects.create_user(username="w1", password="x", role="waiter")
        self.w2 = User.objects.create_user(username="w2", password="x", role="waiter")
        self.cook = User.objects.create_user(username="cook", password="x", role="cook")
        hall = Hall.objects.create(name="Зал")
        self.table = Table.objects.create(hall=hall, number="7", seats=4)
        dish = Dish.objects.create(category=Category.objects.create(name="Еда"), name="Плов", price=40000)
        self.order = services.create_order(self.w1, Order.OrderType.DINE_IN, self.table)
        self.order.items.create(dish=dish, quantity=1, price=dish.price)
        self.url = reverse("global_search")

    def test_finds_order_by_number_and_table(self):
        self.client.force_login(self.w1)
        self.assertContains(self.client.get(self.url, {"q": str(self.order.id)}), f"#{self.order.id}")
        self.assertContains(self.client.get(self.url, {"q": "7"}), reverse("order_detail", args=[self.order.id]))

    def test_waiter_does_not_see_foreign_orders(self):
        self.client.force_login(self.w2)
        response = self.client.get(self.url, {"q": str(self.order.id)})
        self.assertNotContains(response, f"#{self.order.id}")

    def test_owner_sees_every_order(self):
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(self.url, {"q": str(self.order.id)}), f"#{self.order.id}")

    def test_cook_is_not_allowed(self):
        self.client.force_login(self.cook)
        self.assertEqual(self.client.get(self.url, {"q": "7"}).status_code, 403)

    def test_empty_query_returns_empty_fragment(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(self.url, {"q": ""}).content.strip(), b"")

    def test_requires_login(self):
        self.assertEqual(self.client.get(self.url, {"q": "1"}).status_code, 302)
