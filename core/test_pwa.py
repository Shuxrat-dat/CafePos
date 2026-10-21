"""Установка кассы как приложения: манифест, иконки, service worker, страница «нет связи»."""
import json
import struct

from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.test import TestCase
from django.urls import reverse

from .models import CafeSettings

User = get_user_model()


def png_size(path):
    with open(path, "rb") as fh:
        header = fh.read(24)
    assert header[:8] == b"\x89PNG\r\n\x1a\n", path
    return struct.unpack(">II", header[16:24])


class ManifestTests(TestCase):
    def test_manifest_is_valid_and_installable(self):
        cafe = CafeSettings.get()
        cafe.name = "Skazka Cafe Restoran"
        cafe.save()
        response = self.client.get(reverse("pwa_manifest"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("application/manifest+json", response["Content-Type"])
        self.assertEqual(response["Cache-Control"], "no-cache, max-age=0")
        data = json.loads(response.content)
        self.assertEqual(data["name"], "Skazka Cafe Restoran")
        self.assertEqual(data["short_name"], "Skazka Cafe ")
        self.assertEqual(data["display"], "standalone")
        self.assertEqual((data["start_url"].split("?")[0], data["scope"]), ("/", "/"))
        sizes = {(i["sizes"], i["purpose"]) for i in data["icons"]}
        self.assertIn(("192x192", "any"), sizes)
        self.assertIn(("512x512", "any"), sizes)
        self.assertIn(("512x512", "maskable"), sizes)

    def test_manifest_icons_exist_with_declared_size(self):
        data = json.loads(self.client.get(reverse("pwa_manifest")).content)
        for icon in data["icons"]:
            path = finders.find(icon["src"].replace("/static/", "", 1))
            self.assertIsNotNone(path, icon["src"])
            side = int(icon["sizes"].split("x")[0])
            self.assertEqual(png_size(path), (side, side), icon["src"])

    def test_phone_and_touch_icons_exist(self):
        self.assertEqual(png_size(finders.find("icons/apple-touch-icon.png")), (180, 180))
        self.assertEqual(png_size(finders.find("icons/favicon-32.png")), (32, 32))


class ServiceWorkerTests(TestCase):
    def test_service_worker_is_served_from_root_and_not_cached(self):
        response = self.client.get(reverse("pwa_service_worker"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(reverse("pwa_service_worker"), "/sw.js")
        self.assertIn("javascript", response["Content-Type"])
        self.assertEqual(response["Service-Worker-Allowed"], "/")
        self.assertEqual(response["Cache-Control"], "no-cache, max-age=0")
        code = response.content.decode()
        self.assertNotIn("__VERSION__", code)
        self.assertIn("cafepos-", code)

    def test_cache_version_is_stable_between_requests(self):
        first = self.client.get(reverse("pwa_service_worker")).content.decode()
        again = self.client.get(reverse("pwa_service_worker")).content.decode()
        self.assertEqual(first, again)

    def test_worker_never_caches_data(self):
        code = self.client.get(reverse("pwa_service_worker")).content.decode()
        # Данные не кэшируются: только файлы оформления и страница «нет связи»
        self.assertIn("/static/", code)
        self.assertNotIn("/orders/", code)
        self.assertNotIn("/pay/", code)
        self.assertIn("request.method !== 'GET'", code)


class OfflinePageTests(TestCase):
    def test_offline_page_needs_no_login_and_has_no_external_assets(self):
        response = self.client.get(reverse("pwa_offline"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("Нет связи с кассой", html)
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)


class OpenForEveryRoleTests(TestCase):
    def test_cook_can_load_manifest_and_worker(self):
        """Роли ограничены по адресам страниц; манифест и воркер нужны любому вошедшему."""
        cook = User.objects.create_user(username="oshpaz", password="pass12345", role="cook")
        self.client.force_login(cook)
        for name in ("pwa_manifest", "pwa_service_worker", "pwa_offline"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)

    def test_pages_link_manifest_and_icons(self):
        html = self.client.get(reverse("login")).content.decode()
        self.assertIn('rel="manifest" href="/manifest.webmanifest"', html)
        self.assertIn('rel="apple-touch-icon"', html)
        self.assertIn("apple-mobile-web-app-capable", html)
        owner = User.objects.create_user(username="egasi", password="pass12345", role="owner")
        self.client.force_login(owner)
        html = self.client.get(reverse("floor")).content.decode()
        self.assertIn("data-install-app", html)
        self.assertIn("/sw.js", html)
