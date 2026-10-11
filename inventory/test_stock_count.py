from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from .models import Product, StockMovement

User = get_user_model()


class StockCountTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="adm_c", password="pass12345", role="admin")
        self.waiter = User.objects.create_user(username="wai_c", password="pass12345", role="waiter")
        self.beef = Product.objects.create(name="Говядина", unit="g", cost_per_unit=80)
        self.rice = Product.objects.create(name="Рис", unit="g", cost_per_unit=15)
        StockMovement.objects.create(product=self.beef, kind="income", quantity=1000)
        StockMovement.objects.create(product=self.rice, kind="income", quantity=500)

    def _count(self, **fields):
        return self.client.post("/stock/count/", fields)

    def _stock(self, product):
        product.refresh_from_db()
        return product.stock

    def test_admin_opens_count(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get("/stock/count/").status_code, 200)

    def test_waiter_cannot_open_count(self):
        self.client.force_login(self.waiter)
        self.assertEqual(self.client.get("/stock/count/").status_code, 403)
        response = self._count(**{f"fact_{self.beef.id}": "0"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self._stock(self.beef), Decimal("1000"))

    def test_count_decreases_stock(self):
        self.client.force_login(self.admin)
        response = self._count(**{f"fact_{self.beef.id}": "900"})
        self.assertRedirects(response, "/stock/count/?done=1")
        self.assertEqual(self._stock(self.beef), Decimal("900"))
        movement = StockMovement.objects.filter(product=self.beef, kind="adjust").get()
        self.assertEqual(movement.quantity, Decimal("-100"))
        self.assertEqual(movement.created_by, self.admin)

    def test_count_increases_stock(self):
        self.client.force_login(self.admin)
        self._count(**{f"fact_{self.beef.id}": "1250,5"})
        self.assertEqual(self._stock(self.beef), Decimal("1250.5"))

    def test_same_value_creates_no_movement(self):
        self.client.force_login(self.admin)
        response = self._count(**{f"fact_{self.beef.id}": "1000"})
        self.assertRedirects(response, "/stock/count/?done=0")
        self.assertFalse(StockMovement.objects.filter(kind="adjust").exists())

    def test_blank_fields_are_skipped(self):
        self.client.force_login(self.admin)
        self._count(**{f"fact_{self.beef.id}": "900", f"fact_{self.rice.id}": ""})
        self.assertEqual(self._stock(self.beef), Decimal("900"))
        self.assertEqual(self._stock(self.rice), Decimal("500"))

    def test_invalid_value_saves_nothing(self):
        self.client.force_login(self.admin)
        response = self._count(**{f"fact_{self.beef.id}": "900", f"fact_{self.rice.id}": "много"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._stock(self.beef), Decimal("1000"))
        self.assertFalse(StockMovement.objects.filter(kind="adjust").exists())

    def test_empty_form_is_rejected(self):
        self.client.force_login(self.admin)
        response = self._count()
        self.assertEqual(response.context["error"], "Заполните хотя бы одно поле.")

    def test_count_fixes_negative_stock(self):
        StockMovement.objects.create(product=self.rice, kind="sale", quantity=800)  # 500 - 800 = -300
        self.client.force_login(self.admin)
        self._count(**{f"fact_{self.rice.id}": "0"})
        self.assertEqual(self._stock(self.rice), Decimal("0"))