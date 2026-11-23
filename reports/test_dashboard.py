from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from core.models import CafeSettings
from inventory.models import Product
from menu.models import Category, Dish
from pos.models import Order

User = get_user_model()


def _paid(waiter, dish, qty):
    order = Order.objects.create(waiter=waiter, status=Order.Status.PAID, order_type="takeaway", closed_at=timezone.now())
    order.items.create(dish=dish, quantity=qty, price=dish.price)
    return order


class DashboardTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="admin_rep", password="pass12345", role="admin")
        self.client.force_login(self.admin)

    def test_stock_block_is_removed_from_reports(self):
        Product.objects.create(name="Соль", unit="g", cost_per_unit=1)
        response = self.client.get("/reports/")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Мало на складе")

    def test_all_sold_dishes_are_listed_not_only_top_ten(self):
        category = Category.objects.create(name="Еда")
        for n in range(12):
            dish = Dish.objects.create(category=category, name=f"Блюдо{n:02d}", price=10000)
            _paid(self.admin, dish, n + 1)
        response = self.client.get("/reports/")
        names = [d["obj"].name for d in response.context["top_dishes"]]
        self.assertEqual(len(names), 12)
        self.assertEqual(names[0], "Блюдо11")  # больше всего продано — сверху
        self.assertEqual(response.context["dishes_total_qty"], sum(range(1, 13)))
        self.assertContains(response, "Блюдо00")


class CashierReportsTests(TestCase):
    def setUp(self):
        self.cashier = User.objects.create_user(username="kass_rep", password="pass12345", role="cashier")
        self.client.force_login(self.cashier)

    def _enable(self, value=True):
        settings = CafeSettings.get()
        settings.cashier_sees_reports = value
        settings.save()

    def test_hidden_by_default(self):
        for url in ("/reports/", "/reports/expenses/", "/reports/payroll/"):
            self.assertEqual(self.client.get(url).status_code, 403)

    def test_checkbox_opens_all_report_pages_for_cashier(self):
        self._enable()
        for url in ("/reports/", "/reports/expenses/", "/reports/payroll/"):
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_cashier_cannot_change_anything_in_reports(self):
        self._enable()
        response = self.client.post("/reports/expenses/", {"date": "2026-01-01", "category": "other", "amount": "1000"})
        self.assertEqual(response.status_code, 403)
        response = self.client.post("/reports/payroll/", {"action": "payout", "user_id": self.cashier.id, "amount": "1000"})
        self.assertEqual(response.status_code, 403)
        self.assertNotContains(self.client.get("/reports/expenses/"), "Новый расход")

    def test_waiter_still_blocked_when_checkbox_on(self):
        self._enable()
        waiter = User.objects.create_user(username="ofi_rep", password="pass12345", role="waiter")
        self.client.force_login(waiter)
        self.assertEqual(self.client.get("/reports/").status_code, 403)

    def test_checkbox_is_in_settings_form(self):
        from core.forms import CafeSettingsForm

        self.assertIn("cashier_sees_reports", CafeSettingsForm().fields)
