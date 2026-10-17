import re
from io import StringIO

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.urls import reverse
from django.utils import translation

from core import i18n_tools
from inventory.models import Product
from menu.models import Category, Dish
from pos import services
from pos.models import Hall, Order, Table

User = get_user_model()
CYRILLIC = re.compile(r"[А-Яа-яЁё]+")


class TranslationCatalogTests(TestCase):
    def test_every_interface_string_has_an_uzbek_translation(self):
        try:
            call_command("i18n_sync", "--check", stdout=StringIO())
        except CommandError as exc:
            self.fail(str(exc))

    def test_compiled_catalog_matches_the_po_file(self):
        po = i18n_tools.parse_po(settings.BASE_DIR / "locale" / "uz" / "LC_MESSAGES" / "django.po")
        with translation.override("uz"):
            stale = [k for k, v in po.items() if v and translation.gettext(k) != v]
        self.assertEqual(stale[:5], [], "django.mo устарел: выполните python manage.py i18n_sync")

    def test_russian_catalog_maps_every_string_to_itself(self):
        """Иначе Django на русском экране подставит запасной узбекский перевод (LANGUAGE_CODE у проекта uz)."""
        import gettext

        locale_dir = settings.BASE_DIR / "locale"
        uz = i18n_tools.parse_po(locale_dir / "uz" / "LC_MESSAGES" / "django.po")
        with open(locale_dir / "ru" / "LC_MESSAGES" / "django.mo", "rb") as handle:
            catalog = gettext.GNUTranslations(handle)
        wrong = [k for k in uz if CYRILLIC.search(k) and catalog.gettext(k) != k]
        self.assertEqual(wrong[:5], [], "python manage.py i18n_sync")

    def test_no_hardcoded_russian_text_in_templates(self):
        found = i18n_tools.find_hardcoded_template_text(settings.BASE_DIR)
        self.assertEqual(found, [], "Оберните текст в {% translate %}")

    def test_format_placeholders_match_between_languages(self):
        po = i18n_tools.parse_po(settings.BASE_DIR / "locale" / "uz" / "LC_MESSAGES" / "django.po")
        pattern = re.compile(r"%\([a-z_]+\)s")
        broken = [k for k, v in po.items() if v and sorted(pattern.findall(k)) != sorted(pattern.findall(v))]
        self.assertEqual(broken[:5], [])


class UzbekInterfaceHasNoRussianTests(TestCase):
    """Узбекский интерфейс показывает только узбекский: русские остаются лишь в данных, введённых пользователем."""

    def setUp(self):
        self.owner = User.objects.create_user(username="owner_i", password="x", role="owner")
        hall = Hall.objects.create(name="Основной зал", name_uz="Asosiy zal")
        self.table = Table.objects.create(hall=hall, number="1", seats=4)
        category = Category.objects.create(name="Горячее", name_uz="Issiq taomlar")
        self.dish = Dish.objects.create(category=category, name="Плов", name_uz="Palov", price=40000,
                                        allergens="gluten", prep_minutes=15)
        Product.objects.create(name="Рис", name_uz="Guruch", unit="g", min_stock=100)
        self.order = services.create_order(self.owner, Order.OrderType.DINE_IN, self.table)
        self.order.items.create(dish=self.dish, quantity=1, price=self.dish.price)

    def visible_text(self, response):
        html = response.content.decode()
        text = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S | re.I)
        text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
        text = re.sub(r"<[^>]+>", " ", text)
        attrs = " ".join(re.findall(r'(?:placeholder|title|aria-label|alt)="([^"]*)"', html))
        # Название языка пишется на нём самом: «Русский» на кнопке выбора языка допустимо в любом интерфейсе
        return (text + " " + attrs).replace("Русский", "")

    # Страницы категорий и залов сознательно показывают оба названия (русское и узбекское): их правит владелец.
    def test_pages_in_uzbek_have_no_russian_words(self):
        self.client.force_login(self.owner)
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "uz"
        urls = [
            reverse("floor"), reverse("orders"), reverse("kitchen_board"), reverse("reports"),
            reverse("stock"), reverse("stock_shopping"), reverse("stock_count"), reverse("stock_writeoff"),
            reverse("product_create"), reverse("manage_menu"), reverse("dish_create"), reverse("manage_modifiers"), reverse("manage_settings"),
            reverse("manage_telegram"), reverse("manage_audit"), reverse("setup_wizard"), reverse("manage_backup"),
            reverse("manage_staff"), reverse("staff_create"), reverse("menu_qr"),
            reverse("order_detail", args=[self.order.id]), reverse("pay", args=[self.order.id]),
            reverse("precheck", args=[self.order.id]), reverse("shift_current"),
        ]
        problems = {}
        for url in urls:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
            words = sorted(set(CYRILLIC.findall(self.visible_text(response))))
            if words:
                problems[url] = words[:12]
        self.assertEqual(problems, {})

    def test_guest_pages_in_uzbek_have_no_russian_words(self):
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "uz"
        problems = {}
        for url in (reverse("login"), reverse("public_menu"), reverse("table_menu", args=[self.table.id])):
            words = sorted(set(CYRILLIC.findall(self.visible_text(self.client.get(url)))))
            if words:
                problems[url] = words[:12]
        self.assertEqual(problems, {})

    def test_russian_pages_have_no_uzbek_sentences(self):
        self.client.force_login(self.owner)
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "ru"
        for url in (reverse("floor"), reverse("reports"), reverse("setup_wizard")):
            text = self.visible_text(self.client.get(url))
            for marker in ("Buyurtma", "Sozlamalar", "Hisobotlar", "Ombor"):
                self.assertNotIn(marker, text, f"{marker} на русской странице {url}")
