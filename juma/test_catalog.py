import datetime
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from menu.models import Category, Dish, Modifier, Promotion

from . import catalog
from .models import JumaState
from .test_client import JUMA, fake_call


def make_menu():
    soups = Category.objects.create(name="Супы", name_uz="Sho'rvalar", sort_order=1)
    drinks = Category.objects.create(name="Напитки", sort_order=2)
    soup = Dish.objects.create(category=soups, name="Борщ", name_uz="Borsh", description="Со сметаной", price=25000, prep_minutes=12)
    tea = Dish.objects.create(category=drinks, name="Чай", price=5000, is_available=False)
    return soups, drinks, soup, tea


class BuildCatalogTests(TestCase):
    def test_names_prices_availability_and_ids(self):
        soups, _drinks, soup, tea = make_menu()
        data = catalog.build_catalog()
        self.assertEqual([c["external_id"] for c in data["categories"]], [f"c-{soups.id}", f"c-{_drinks.id}"])
        self.assertEqual(data["categories"][0]["name"], {"uz": "Sho'rvalar", "ru": "Супы"})
        self.assertEqual(data["categories"][1]["name"], {"uz": "Напитки", "ru": "Напитки"})  # нет узбекского названия
        by_id = {i["external_id"]: i for i in data["items"]}
        item = by_id[f"d-{soup.id}"]
        self.assertEqual(item["category_id"], f"c-{soups.id}")
        self.assertEqual(item["name"], {"uz": "Borsh", "ru": "Борщ"})
        self.assertEqual((item["price"], item["available"], item["prep_minutes"]), ("25000", True, 12))
        self.assertEqual(item["description"], {"uz": "Со сметаной", "ru": "Со сметаной"})
        self.assertNotIn("discount_price", item)
        stopped = by_id[f"d-{tea.id}"]
        self.assertFalse(stopped["available"])
        self.assertNotIn("prep_minutes", stopped)  # 0 — общий порог из настроек, в Juma не передаём

    def test_hidden_weighed_and_inactive_category_are_not_sent(self):
        soups, _drinks, soup, _tea = make_menu()
        Dish.objects.create(category=soups, name="Скрытое", price=1000, is_hidden=True)
        Dish.objects.create(category=soups, name="Весовое", price=60000, sold_by_weight=True)
        hidden_cat = Category.objects.create(name="Старое", is_active=False)
        Dish.objects.create(category=hidden_cat, name="Из старой категории", price=1000)
        names = {i["name"]["ru"] for i in catalog.build_catalog()["items"]}
        self.assertEqual(names, {"Борщ", "Чай"})
        self.assertNotIn("Старое", {c["name"]["ru"] for c in catalog.build_catalog()["categories"]})

    def test_modifier_groups_named_group_is_single_choice_extras_are_free(self):
        _s, _d, soup, _t = make_menu()
        small = Modifier.objects.create(name="Маленькая", group="Порция", price_delta=0)
        large = Modifier.objects.create(name="Большая", name_uz="Katta", group="Порция", price_delta=7000)
        sour = Modifier.objects.create(name="Сметана", price_delta=2000)
        bread = Modifier.objects.create(name="Хлеб", price_delta=1000)
        soup.modifiers.set([small, large, sour, bread])
        groups = {g["name"]["ru"]: g for g in catalog.build_catalog()["items"][0]["modifier_groups"]}
        portion, extras = groups["Порция"], groups["Добавки"]
        self.assertEqual((portion["min"], portion["max"]), (0, 1))
        self.assertEqual({o["external_id"]: o["price_delta"] for o in portion["options"]}, {f"m-{small.id}": "0", f"m-{large.id}": "7000"})
        self.assertEqual(portion["options"][1]["name"]["uz"], "Katta")
        self.assertEqual((extras["min"], extras["max"], len(extras["options"])), (0, 2, 2))
        self.assertEqual(extras["external_id"], "g-extra")
        self.assertRegex(portion["external_id"], r"^g-[0-9a-f]{10}$")

    def test_promotion_becomes_discount_price_only_while_it_runs(self):
        _s, _d, soup, _t = make_menu()
        promo = Promotion.objects.create(name="Обед", percent=20, weekdays="")
        promo.dishes.add(soup)
        item = catalog.build_catalog()["items"][0]
        self.assertEqual(item["discount_price"], "20000")
        better = Promotion.objects.create(name="Супер", percent=30, weekdays="")
        better.dishes.add(soup)
        self.assertEqual(catalog.build_catalog()["items"][0]["discount_price"], "17500")
        Promotion.objects.update(is_active=False)
        self.assertNotIn("discount_price", catalog.build_catalog()["items"][0])
        Promotion.objects.update(is_active=True, date_to=timezone.localdate() - datetime.timedelta(days=1))
        self.assertNotIn("discount_price", catalog.build_catalog()["items"][0])


@override_settings(**JUMA)
class SyncCatalogTests(TestCase):
    def sync(self, call, **kwargs):
        with mock.patch("juma.client._call", call):
            return catalog.sync_catalog(**kwargs)

    def test_first_sync_sends_catalog_and_remembers_it(self):
        make_menu()
        call = fake_call({("PUT", "/catalog"): (200, {"applied": True})})
        self.assertTrue(self.sync(call))
        method, path, payload = call.calls[0]
        self.assertEqual((method, path), ("PUT", "/catalog"))
        self.assertEqual(len(payload["items"]), 2)
        state = JumaState.get()
        self.assertEqual(state.catalog_version, payload["version"])
        self.assertEqual(len(state.catalog_hash), 64)
        self.assertEqual(state.last_error, "")

    def test_unchanged_menu_is_not_sent_again_changed_menu_gets_higher_version(self):
        _s, _d, soup, _t = make_menu()
        call = fake_call()
        self.sync(call)
        first = call.calls[0][2]["version"]
        self.sync(call)
        self.assertEqual(len(call.calls), 1)
        soup.price = 27000
        soup.save()
        self.sync(call)
        self.assertEqual(len(call.calls), 2)
        self.assertGreater(call.calls[1][2]["version"], first)
        self.sync(call, force=True)
        self.assertEqual(len(call.calls), 3)

    def test_stop_list_change_is_picked_up(self):
        _s, _d, soup, _t = make_menu()
        call = fake_call()
        self.sync(call)
        Dish.objects.filter(pk=soup.pk).update(is_available=False)
        self.sync(call)
        sent = {i["external_id"]: i for i in call.calls[1][2]["items"]}
        self.assertFalse(sent[f"d-{soup.id}"]["available"])

    def test_failure_is_recorded_and_retried_next_time(self):
        make_menu()
        self.assertFalse(self.sync(fake_call(default=(0, {}))))
        state = JumaState.get()
        self.assertEqual(state.catalog_hash, "")
        self.assertIn("Нет связи", state.last_error)
        self.assertTrue(self.sync(fake_call()))
        self.assertNotEqual(JumaState.get().catalog_hash, "")

    def test_juma_with_newer_version_is_caught_up_with(self):
        make_menu()
        answers = iter([(200, {"applied": False, "version": 9_999_999_999_999}), (200, {"applied": True})])
        calls = []

        def call(method, path, payload=None, timeout=10):
            calls.append(payload["version"])
            return next(answers)

        self.assertTrue(self.sync(call))
        self.assertGreater(calls[1], 9_999_999_999_999)
        self.assertEqual(JumaState.get().catalog_version, calls[1])

    def test_nothing_is_sent_when_juma_is_not_configured(self):
        make_menu()
        call = fake_call()
        with override_settings(JUMA_URL="", JUMA_MERCHANT_KEY=""):
            self.assertFalse(self.sync(call))
        self.assertEqual(call.calls, [])
