from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from menu.models import Category, Dish

from .models import Hall, Order, OrderItem, Table

User = get_user_model()


class ReceiptPrintTests(TestCase):
    def test_receipt_has_print_button(self):
        cashier = User.objects.create_user(username="cas_r", password="pass12345", role="cashier")
        table = Table.objects.create(hall=Hall.objects.create(name="Зал"), number="1")
        dish = Dish.objects.create(category=Category.objects.create(name="Горячее"), name="Плов", price=25000)
        order = Order.objects.create(
            table=table, waiter=cashier, status=Order.Status.PAID,
            payment_method="card", closed_at=timezone.now(),
        )
        OrderItem.objects.create(order=order, dish=dish, quantity=1, status=OrderItem.Status.SERVED)
        self.client.force_login(cashier)
        response = self.client.get(reverse("receipt", args=[order.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "window.print()")