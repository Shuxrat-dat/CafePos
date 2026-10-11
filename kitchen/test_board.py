from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from menu.models import Category, Dish
from pos.models import Hall, Order, OrderItem, Table

User = get_user_model()


class KitchenTests(TestCase):
    def setUp(self):
        self.cook = User.objects.create_user(username="cook_k", password="pass12345", role="cook")
        self.waiter = User.objects.create_user(username="waiter_k", password="pass12345", role="waiter")
        hall = Hall.objects.create(name="Основной зал")
        table = Table.objects.create(hall=hall, number="1")
        category = Category.objects.create(name="Горячее")
        dish = Dish.objects.create(category=category, name="Плов", price=25000)
        self.order = Order.objects.create(table=table, waiter=self.waiter)
        self.item = OrderItem.objects.create(
            order=self.order, dish=dish, quantity=1, status=OrderItem.Status.COOKING
        )

    def test_cook_opens_board(self):
        self.client.force_login(self.cook)
        self.assertEqual(self.client.get(reverse("kitchen_board")).status_code, 200)

    def test_cook_opens_tickets(self):
        self.client.force_login(self.cook)
        self.assertEqual(self.client.get(reverse("kitchen_tickets")).status_code, 200)

    def test_cook_marks_item_ready(self):
        self.client.force_login(self.cook)
        self.client.post(reverse("mark_ready", args=[self.item.id]))
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, OrderItem.Status.READY)

    def test_waiter_cannot_mark_ready(self):
        self.client.force_login(self.waiter)
        response = self.client.post(reverse("mark_ready", args=[self.item.id]))
        self.assertEqual(response.status_code, 403)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, OrderItem.Status.COOKING)

    def test_waiter_cannot_open_tickets(self):
        self.client.force_login(self.waiter)
        self.assertEqual(self.client.get(reverse("kitchen_tickets")).status_code, 403)