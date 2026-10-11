from decimal import Decimal

from django.test import TestCase

from .models import Product, StockMovement


class UnitCostTests(TestCase):
    def setUp(self):
        self.product = Product.objects.create(name="Говядина", unit="g", cost_per_unit=80)

    def test_cost_is_saved_with_movement(self):
        movement = StockMovement.objects.create(product=self.product, kind="income", quantity=1000)
        self.assertEqual(movement.unit_cost, Decimal("80"))

    def test_old_movement_keeps_price_after_price_change(self):
        old = StockMovement.objects.create(product=self.product, kind="income", quantity=1000)
        self.product.cost_per_unit = 100
        self.product.save()
        new = StockMovement.objects.create(product=self.product, kind="sale", quantity=10)
        old.refresh_from_db()
        self.assertEqual(old.unit_cost, Decimal("80"))
        self.assertEqual(new.unit_cost, Decimal("100"))