from django.contrib.auth import get_user_model
from django.test import TestCase

User = get_user_model()


class RoleMiddlewareTests(TestCase):
    def setUp(self):
        self.waiter = User.objects.create_user(username="w1", password="pass12345", role="waiter")
        self.cashier = User.objects.create_user(username="c1", password="pass12345", role="cashier")
        self.admin = User.objects.create_user(username="a1", password="pass12345", role="admin")
        self.cook = User.objects.create_user(username="k1", password="pass12345", role="cook")

    def test_waiter_cannot_open_reports(self):
        self.client.force_login(self.waiter)
        self.assertEqual(self.client.get("/reports/").status_code, 403)

    def test_cashier_cannot_open_reports(self):
        self.client.force_login(self.cashier)
        self.assertEqual(self.client.get("/reports/").status_code, 403)

    def test_cook_cannot_open_reports(self):
        self.client.force_login(self.cook)
        self.assertEqual(self.client.get("/reports/").status_code, 403)

    def test_admin_opens_reports(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get("/reports/").status_code, 200)

    def test_waiter_cannot_open_kitchen(self):
        self.client.force_login(self.waiter)
        self.assertEqual(self.client.get("/kitchen/").status_code, 403)

    def test_cook_is_not_blocked_on_kitchen(self):
        self.client.force_login(self.cook)
        self.assertNotEqual(self.client.get("/kitchen/").status_code, 403)

    def test_cook_cannot_open_service_pages(self):
        self.client.force_login(self.cook)
        self.assertEqual(self.client.get("/orders/1/").status_code, 403)