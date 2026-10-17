import re

from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.test import TestCase
from django.urls import reverse

from menu.models import Category, Dish
from pos import services
from pos.models import Hall, Order, Table

User = get_user_model()

# Внешний ресурс на странице: скрипт, стиль, картинка, шрифт или iframe с чужого адреса
EXTERNAL = re.compile(
    r"""(?:<script[^>]+src|<link[^>]+href|<img[^>]+src|<iframe[^>]+src|@import\s+url\(|url\()\s*=?\s*["']?https?://""",
    re.I,
)


class PingTests(TestCase):
    def test_ping_answers_without_login_and_is_not_cached(self):
        response = self.client.get(reverse("ping"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})
        self.assertIn("no-store", response["Cache-Control"])

    def test_every_role_can_ping(self):
        for role in ("owner", "admin", "waiter", "cashier", "cook"):
            user = User.objects.create_user(username=f"u_{role}", password="x", role=role)
            self.client.force_login(user)
            self.assertEqual(self.client.get(reverse("ping")).status_code, 200, role)

    def test_header_shows_live_connection_pill_instead_of_static_label(self):
        owner = User.objects.create_user(username="own_o", password="x", role="owner")
        self.client.force_login(owner)
        html = self.client.get(reverse("floor")).content.decode()
        self.assertIn('id="conn-pill"', html)
        self.assertIn('id="conn-banner"', html)
        self.assertIn("Касса на связи", html)
        self.assertNotIn(">Онлайн<", html)


class WorksWithoutInternetTests(TestCase):
    """Касса должна открываться в сети без интернета: ни одной страницы с внешними скриптами, стилями и шрифтами."""

    def setUp(self):
        self.owner = User.objects.create_user(username="own_w", password="x", role="owner")
        hall = Hall.objects.create(name="Зал")
        self.table = Table.objects.create(hall=hall, number="1")
        dish = Dish.objects.create(category=Category.objects.create(name="Еда"), name="Плов", price=40000)
        order = services.create_order(self.owner, Order.OrderType.DINE_IN, self.table)
        order.items.create(dish=dish, quantity=1, price=dish.price)
        self.order = order

    def test_pages_load_no_external_assets(self):
        self.client.force_login(self.owner)
        urls = [
            reverse("floor"), reverse("orders"), reverse("kitchen_board"), reverse("reports"),
            reverse("stock"), reverse("stock_shopping"), reverse("manage_menu"), reverse("manage_tables"),
            reverse("manage_settings"), reverse("manage_telegram"), reverse("manage_audit"),
            reverse("setup_wizard"), reverse("manage_backup"), reverse("order_detail", args=[self.order.id]),
            reverse("pay", args=[self.order.id]),
        ]
        for url in urls:
            html = self.client.get(url).content.decode()
            self.assertIsNone(EXTERNAL.search(html), f"внешний ресурс на {url}")

    def test_guest_pages_and_login_load_no_external_assets(self):
        for url in (reverse("login"), reverse("public_menu"), reverse("table_menu", args=[self.table.id])):
            html = self.client.get(url).content.decode()
            self.assertIsNone(EXTERNAL.search(html), f"внешний ресурс на {url}")

    def test_fonts_and_scripts_are_shipped_with_the_project(self):
        for path in ("js/tailwind.js", "js/htmx.min.js", "fonts/Manrope-400.ttf", "fonts/Manrope-700.ttf"):
            self.assertIsNotNone(finders.find(path), path)
