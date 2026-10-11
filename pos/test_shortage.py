from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from inventory.models import Product, RecipeItem, StockMovement
from menu.models import Category, Dish

from .models import Hall, Order, OrderItem, Table

User = get_user_model()


class ShortageWarningTests(TestCase):
    def setUp(self):
        self.cashier = User.objects.create_user(
            username="cashier_short", password="pass12345", role="cashier"
        )
        hall = Hall.objects.create(name="Основной зал")
        self.table = Table.objects.create(hall=hall, number="1")
        category = Category.objects.create(name="Горячее")
        self.plov = Dish.objects.create(category=category, name="Плов", price=25000)
        self.beef = Product.objects.create(name="Говядина", unit="g", cost_per_unit=80)
        RecipeItem.objects.create(dish=self.plov, product=self.beef, quantity=200)
        self.client.force_login(self.cashier)

    def _order(self, quantity):
        order = Order.objects.create(table=self.table, waiter=self.cashier)
        OrderItem.objects.create(
            order=order, dish=self.plov, quantity=quantity, status=OrderItem.Status.SERVED
        )
        return order

    def test_warning_when_stock_is_not_enough(self):
        StockMovement.objects.create(product=self.beef, kind="income", quantity=300)
        order = self._order(2)  # нужно 400, есть 300
        response = self.client.get(reverse("pay", args=[order.id]))
        self.assertEqual(response.status_code, 200)
        shortages = response.context["shortages"]
        self.assertEqual(len(shortages), 1)
        self.assertEqual(shortages[0]["need"], Decimal("400"))
        self.assertContains(response, "не хватает")

    def test_no_warning_when_stock_is_enough(self):
        StockMovement.objects.create(product=self.beef, kind="income", quantity=1000)
        order = self._order(2)
        response = self.client.get(reverse("pay", args=[order.id]))
        self.assertEqual(response.context["shortages"], [])
        self.assertNotContains(response, "не хватает")

    def test_payment_still_works_with_shortage(self):
        order = self._order(2)  # на складе 0
        self.client.post(reverse("pay", args=[order.id]), {"method": "cash", "received": "50000"})
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PAID)