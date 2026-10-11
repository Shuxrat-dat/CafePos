from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from .models import Product, StockMovement

User = get_user_model()


class StockScreenTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="adm_s", password="pass12345", role="admin")
        self.owner = User.objects.create_user(username="own_s", password="pass12345", role="owner")
        self.waiter = User.objects.create_user(username="wai_s", password="pass12345", role="waiter")
        self.cashier = User.objects.create_user(username="cas_s", password="pass12345", role="cashier")
        self.cook = User.objects.create_user(username="coo_s", password="pass12345", role="cook")
        self.beef = Product.objects.create(name="Говядина", unit="g", cost_per_unit=80)

    def _income(self, **data):
        payload = {"product": str(self.beef.id), "quantity": "1000"}
        payload.update(data)
        return self.client.post("/stock/", payload)

    def _stock(self):
        self.beef.refresh_from_db()
        return self.beef.stock

    def test_admin_opens_stock(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get("/stock/").status_code, 200)

    def test_owner_opens_stock(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get("/stock/").status_code, 200)

    def test_waiter_cannot_open_stock(self):
        self.client.force_login(self.waiter)
        self.assertEqual(self.client.get("/stock/").status_code, 403)

    def test_cashier_cannot_open_stock(self):
        self.client.force_login(self.cashier)
        self.assertEqual(self.client.get("/stock/").status_code, 403)

    def test_cook_cannot_open_stock(self):
        self.client.force_login(self.cook)
        self.assertEqual(self.client.get("/stock/").status_code, 403)

    def test_income_increases_stock(self):
        self.client.force_login(self.admin)
        response = self._income()
        self.assertRedirects(response, "/stock/?ok=1")
        self.assertEqual(self._stock(), Decimal("1000"))
        movement = StockMovement.objects.get(product=self.beef)
        self.assertEqual(movement.kind, "income")
        self.assertEqual(movement.created_by, self.admin)

    def test_income_with_price_updates_cost(self):
        self.client.force_login(self.admin)
        self._income(cost="95")
        self.beef.refresh_from_db()
        self.assertEqual(self.beef.cost_per_unit, Decimal("95"))
        self.assertEqual(StockMovement.objects.get(product=self.beef).unit_cost, Decimal("95"))

    def test_income_accepts_comma(self):
        self.client.force_login(self.admin)
        self._income(quantity="2,5")
        self.assertEqual(self._stock(), Decimal("2.5"))

    def test_income_rejects_zero(self):
        self.client.force_login(self.admin)
        response = self._income(quantity="0")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["error"], "Введите количество числом больше нуля.")
        self.assertFalse(StockMovement.objects.exists())

    def test_income_rejects_negative(self):
        self.client.force_login(self.admin)
        self._income(quantity="-50")
        self.assertFalse(StockMovement.objects.exists())

    def test_income_rejects_text(self):
        self.client.force_login(self.admin)
        self._income(quantity="много")
        self._income(quantity="NaN")
        self.assertFalse(StockMovement.objects.exists())