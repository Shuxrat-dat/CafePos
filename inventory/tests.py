from decimal import Decimal

from django.test import TestCase

from .models import Product, StockMovement


class StockMovementTests(TestCase):
    def setUp(self):
        self.product = Product.objects.create(name="Говядина", unit="g", cost_per_unit=80)

    def _move(self, kind, quantity):
        StockMovement.objects.create(product=self.product, kind=kind, quantity=quantity)
        self.product.refresh_from_db()

    def test_income_increases_stock(self):
        self._move("income", 1000)
        self.assertEqual(self.product.stock, Decimal("1000"))

    def test_income_ignores_sign(self):
        self._move("income", -500)
        self.assertEqual(self.product.stock, Decimal("500"))

    def test_sale_decreases_stock(self):
        self._move("income", 1000)
        self._move("sale", 300)
        self.assertEqual(self.product.stock, Decimal("700"))

    def test_writeoff_decreases_stock(self):
        self._move("income", 1000)
        self._move("writeoff", 100)
        self.assertEqual(self.product.stock, Decimal("900"))

    def test_adjust_can_add_and_subtract(self):
        self._move("income", 1000)
        self._move("adjust", -200)
        self.assertEqual(self.product.stock, Decimal("800"))
        self._move("adjust", 50)
        self.assertEqual(self.product.stock, Decimal("850"))

    def test_stock_can_go_negative(self):
        # Фиксируем известное ограничение: склад не запрещает уход в минус
        self._move("sale", 100)
        self.assertEqual(self.product.stock, Decimal("-100"))