from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from menu.models import Category, Dish, Modifier
from pos.models import Hall, Order, OrderItem, Table

from .models import Product, RecipeItem, StockMovement
from .services import write_off_order

User = get_user_model()


class WriteOffTests(TestCase):
    def setUp(self):
        self.cashier = User.objects.create_user(
            username="cashier_stock", password="pass12345", role="cashier"
        )
        hall = Hall.objects.create(name="Основной зал")
        self.table = Table.objects.create(hall=hall, number="1")
        category = Category.objects.create(name="Горячее")
        self.plov = Dish.objects.create(category=category, name="Плов", price=25000)
        self.tea = Dish.objects.create(category=category, name="Чай", price=5000)  # без техкарты

        self.beef = Product.objects.create(name="Говядина", unit="g", cost_per_unit=80)
        StockMovement.objects.create(product=self.beef, kind="income", quantity=1000)
        # Один плов расходует 200 г говядины
        RecipeItem.objects.create(dish=self.plov, product=self.beef, quantity=200)

        self.double = Modifier.objects.create(name="Двойная порция", consumption_multiplier=2)
        self.client.force_login(self.cashier)

    def _order_with(self, dish, quantity, modifiers=(), status=OrderItem.Status.SERVED):
        order = Order.objects.create(table=self.table, waiter=self.cashier)
        item = OrderItem.objects.create(order=order, dish=dish, quantity=quantity, status=status)
        item.modifiers.set(modifiers)
        return order, item

    def _stock(self):
        self.beef.refresh_from_db()
        return self.beef.stock

    def test_payment_writes_off_by_recipe(self):
        order, _ = self._order_with(self.plov, 2)
        self.client.post(reverse("pay", args=[order.id]), {"method": "cash", "received": "50000"})
        # 1000 - 2 x 200 = 600
        self.assertEqual(self._stock(), Decimal("600"))
        movement = StockMovement.objects.get(order=order, kind="sale")
        self.assertEqual(movement.quantity, Decimal("-400"))

    def test_modifier_multiplier_doubles_consumption(self):
        order, _ = self._order_with(self.plov, 1, modifiers=[self.double])
        self.client.post(reverse("pay", args=[order.id]), {"method": "cash", "received": "25000"})
        # 1000 - 200 x 2 = 600
        self.assertEqual(self._stock(), Decimal("600"))

    def test_dish_without_recipe_writes_nothing(self):
        order, _ = self._order_with(self.tea, 3)
        write_off_order(order)
        self.assertFalse(StockMovement.objects.filter(order=order).exists())
        self.assertEqual(self._stock(), Decimal("1000"))

    def test_write_off_happens_only_once(self):
        order, _ = self._order_with(self.plov, 1)
        write_off_order(order)
        write_off_order(order)
        self.assertEqual(StockMovement.objects.filter(order=order, kind="sale").count(), 1)
        self.assertEqual(self._stock(), Decimal("800"))

    def test_split_writes_off_only_paid_part(self):
        order, item = self._order_with(self.plov, 3)
        self.client.post(
            reverse("pay", args=[order.id]),
            {"split": "1", "method": "cash", "received": "25000", f"qty_{item.id}": "1"},
        )
        # Оплачен 1 плов из 3: списано 200, а не 600
        self.assertEqual(self._stock(), Decimal("800"))
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.OPEN)
        self.assertFalse(StockMovement.objects.filter(order=order, kind="sale").exists())

    def test_cancelled_order_writes_nothing(self):
        order, _ = self._order_with(self.plov, 1, status=OrderItem.Status.NEW)
        self.client.post(reverse("cancel_order", args=[order.id]))
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.CANCELLED)
        self.assertEqual(self._stock(), Decimal("1000"))

    def test_payment_can_push_stock_negative(self):
        # Фиксируем известное ограничение: оплата проходит, даже если продукта не хватает
        order, _ = self._order_with(self.plov, 6)
        self.client.post(reverse("pay", args=[order.id]), {"method": "cash", "received": "150000"})
        # 1000 - 6 x 200 = -200
        self.assertEqual(self._stock(), Decimal("-200"))