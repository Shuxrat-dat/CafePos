from django.contrib.auth import get_user_model
from django.test import TestCase

from inventory.models import Product, StockMovement

User = get_user_model()


class DashboardStockTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="admin_rep", password="pass12345", role="admin")
        self.client.force_login(self.admin)

    def test_negative_stock_is_marked(self):
        beef = Product.objects.create(name="Говядина", unit="g", cost_per_unit=80)
        StockMovement.objects.create(product=beef, kind="sale", quantity=50)
        response = self.client.get("/reports/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Говядина")
        self.assertContains(response, "в минусе")

    def test_zero_stock_is_low_but_not_negative(self):
        Product.objects.create(name="Соль", unit="g", cost_per_unit=1)
        response = self.client.get("/reports/")
        self.assertContains(response, "Соль")
        self.assertNotContains(response, "в минусе")