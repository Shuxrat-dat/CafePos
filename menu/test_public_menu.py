from django.contrib.auth import get_user_model
from django.test import TestCase

from .models import Category, Dish

User = get_user_model()


class PublicMenuTests(TestCase):
    def setUp(self):
        category = Category.objects.create(name="Горячее")
        Dish.objects.create(category=category, name="Плов", price=40000)
        Dish.objects.create(category=category, name="Снятое блюдо", price=1000, is_available=False)
        hidden = Category.objects.create(name="Скрытая категория", is_active=False)
        Dish.objects.create(category=hidden, name="Невидимка", price=1000)
        Category.objects.create(name="Пустая категория")
        self.admin = User.objects.create_user(username="adm_q", password="pass12345", role="admin")
        self.waiter = User.objects.create_user(username="wai_q", password="pass12345", role="waiter")
        self.cook = User.objects.create_user(username="coo_q", password="pass12345", role="cook")

    def test_guest_sees_menu_without_login(self):
        response = self.client.get("/menu/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Плов")
        self.assertContains(response, "Горячее")

    def test_unavailable_and_hidden_are_not_shown(self):
        response = self.client.get("/menu/")
        self.assertNotContains(response, "Снятое блюдо")
        self.assertNotContains(response, "Невидимка")
        self.assertNotContains(response, "Скрытая категория")
        self.assertNotContains(response, "Пустая категория")

    def test_cook_can_open_public_menu(self):
        self.client.force_login(self.cook)
        self.assertEqual(self.client.get("/menu/").status_code, 200)

    def test_guest_cannot_open_qr_page(self):
        response = self.client.get("/menu/qr/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)

    def test_waiter_cannot_open_qr_page(self):
        self.client.force_login(self.waiter)
        self.assertEqual(self.client.get("/menu/qr/").status_code, 403)

    def test_admin_sees_qr_code(self):
        self.client.force_login(self.admin)
        response = self.client.get("/menu/qr/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<svg")
        self.assertContains(response, "/menu/")