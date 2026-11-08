import json
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from inventory.models import Product, RecipeItem, StockMovement
from inventory.services import write_off_order
from menu.models import Category, Dish
from pos import services
from pos.models import Order

from . import hub, hub_snapshot, telegram
from .models import CafeSettings

User = get_user_model()
HUB = {"HUB_URL": "https://hub.test", "HUB_REGISTRATION_TOKEN": "secret", "TELEGRAM_SYNC": True}


def fake_call(responses):
    """Подменяет сетевой вызов: responses — {(метод, путь): (код, тело)}. Запоминает вызовы."""
    calls = []

    def call(method, path, payload=None, key="", raw=None, headers=None, timeout=8):
        calls.append((method, path, payload, key))
        return responses.get((method, path.split("?")[0]), (200, {}))

    call.calls = calls
    return call


@override_settings(**HUB)
class HubClientTests(TestCase):
    def test_connect_saves_key_and_returns_link(self):
        call = fake_call({("POST", "/v1/venues"): (200, {"venue_key": "K1", "link_url": "https://t.me/B?start=x"})})
        with mock.patch("core.hub._call", call), mock.patch("core.hub.push_soon"):
            ok, link = hub.connect(CafeSettings.get())
        self.assertTrue(ok)
        self.assertEqual(link, "https://t.me/B?start=x")
        self.assertEqual(CafeSettings.get().hub_key, "K1")
        self.assertEqual(call.calls[0][2]["registration_token"], "secret")

    def test_connect_reports_wrong_secret_and_no_network(self):
        for code, word in ((403, "HUB_REGISTRATION_TOKEN"), (0, "Нет связи")):
            with mock.patch("core.hub._call", fake_call({("POST", "/v1/venues"): (code, {})})):
                ok, reason = hub.connect(CafeSettings.get())
            self.assertFalse(ok)
            self.assertIn(word, reason)
        self.assertEqual(CafeSettings.get().hub_key, "")

    def test_disconnect_forgets_key(self):
        cafe = CafeSettings.get()
        cafe.hub_key = "K1"
        cafe.save()
        call = fake_call({})
        with mock.patch("core.hub._call", call):
            hub.disconnect(cafe)
        self.assertEqual(CafeSettings.get().hub_key, "")
        self.assertEqual(call.calls[0][1], "/v1/disconnect")


@override_settings(**HUB)
class RoutingTests(TestCase):
    def setUp(self):
        cafe = CafeSettings.get()
        cafe.hub_key = "K1"
        cafe.save()
        self.owner = User.objects.create_user(username="own", password="x", role="owner")

    def test_events_go_to_hub_not_to_telegram(self):
        call = fake_call({})
        with mock.patch("core.hub._call", call), mock.patch("core.telegram._post") as post:
            self.assertTrue(telegram.send("привет", "cancel"))
        post.assert_not_called()
        self.assertEqual(call.calls[0][:2], ("POST", "/v1/events"))
        self.assertEqual(call.calls[0][2], {"event": "cancel", "text": "привет"})

    def test_disabled_event_is_not_sent(self):
        cafe = CafeSettings.get()
        cafe.tg_notify_cancel = False
        cafe.save()
        call = fake_call({})
        with mock.patch("core.hub._call", call):
            self.assertFalse(telegram.send("x", "cancel"))
        self.assertEqual(call.calls, [])

    def test_server_error_alert_goes_to_hub(self):
        call = fake_call({})
        with mock.patch("core.hub._call", call):
            telegram.notify_server_error("/pay/", "Boom")
        self.assertEqual(call.calls[0][2]["event"], "error")

    def test_test_button_uses_hub(self):
        with mock.patch("core.hub._call", fake_call({("POST", "/v1/test"): (200, {"sent": 2})})):
            self.assertEqual(telegram.send_now("x"), (True, ""))
        with mock.patch("core.hub._call", fake_call({("POST", "/v1/test"): (200, {"sent": 0})})):
            ok, reason = telegram.send_now("x")
        self.assertFalse(ok)
        self.assertIn("получател", reason)

    def test_without_hub_url_old_token_mode_still_works(self):
        with override_settings(HUB_URL=""):
            cafe = CafeSettings.get()
            cafe.telegram_bot_token = "123456789:" + "A" * 35
            cafe.telegram_chat_id = "555"
            cafe.save()
            with mock.patch("core.telegram._post", return_value=(True, "")) as post:
                telegram.send("x", "cancel")
        post.assert_called_once()


@override_settings(**HUB)
class QuickConnectPageTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="own", password="x", role="owner")
        self.client.force_login(self.owner)
        self.url = reverse("manage_telegram")

    def test_connect_button_registers_and_page_shows_link_and_qr(self):
        responses = {
            ("POST", "/v1/venues"): (200, {"venue_key": "K1", "link_url": "https://t.me/B?start=abc"}),
            ("GET", "/v1/status"): (200, {"connected": False, "recipients": [], "link_url": "https://t.me/B?start=abc"}),
        }
        with mock.patch("core.hub._call", fake_call(responses)), mock.patch("core.hub.push_soon"):
            self.client.post(self.url, {"action": "hub_connect"})
            page = self.client.get(self.url)
        self.assertContains(page, "https://t.me/B?start=abc")
        self.assertContains(page, "<svg")
        self.assertContains(page, reverse("manage_telegram_status"))

    def test_page_offers_connect_before_registration(self):
        page = self.client.get(self.url)
        self.assertContains(page, "hub_connect")

    def test_connected_page_lists_recipients(self):
        cafe = CafeSettings.get()
        cafe.hub_key = "K1"
        cafe.save()
        state = {"connected": True, "recipients": [{"chat_id": "1", "title": "Ali"}]}
        with mock.patch("core.hub._call", fake_call({("GET", "/v1/status"): (200, state)})):
            page = self.client.get(self.url)
        self.assertContains(page, "Ali")
        self.assertContains(page, "hub_remove")

    def test_lost_key_resets_connection(self):
        cafe = CafeSettings.get()
        cafe.hub_key = "K1"
        cafe.save()
        with mock.patch("core.hub._call", fake_call({("GET", "/v1/status"): (401, {})})):
            self.client.get(self.url)
        self.assertEqual(CafeSettings.get().hub_key, "")

    def test_status_endpoint_for_polling(self):
        cafe = CafeSettings.get()
        cafe.hub_key = "K1"
        cafe.save()
        with mock.patch("core.hub._call", fake_call({("GET", "/v1/status"): (200, {"connected": True})})):
            data = self.client.get(reverse("manage_telegram_status")).json()
        self.assertEqual(data, {"connected": True})

    def test_hub_unreachable_does_not_break_page(self):
        cafe = CafeSettings.get()
        cafe.hub_key = "K1"
        cafe.save()
        with mock.patch("core.hub._call", fake_call({("GET", "/v1/status"): (0, {})})):
            self.assertContains(self.client.get(self.url), "недоступен")


class SnapshotTests(TestCase):
    def setUp(self):
        self.waiter = User.objects.create_user(username="wt", password="x", role="waiter")
        self.cashier = User.objects.create_user(username="cs", password="x", role="cashier", first_name="Aziz")
        category = Category.objects.create(name="Еда")
        self.dish = Dish.objects.create(category=category, name="Плов", name_uz="Osh", price=40000)
        self.meat = Product.objects.create(name="Мясо", name_uz="Goʻsht", unit="g", stock=0, min_stock=1000,
                                           cost_per_unit=Decimal("20"))
        RecipeItem.objects.create(dish=self.dish, product=self.meat, quantity=Decimal("300"))

    def pay_one(self):
        order = services.create_order(self.waiter, Order.OrderType.TAKEAWAY)
        order.items.create(dish=self.dish, quantity=2, price=self.dish.price)
        services.open_shift(self.cashier, 0) if not services.get_current_shift() else None
        services.pay_order(order, self.cashier, "cash", 80000, 0)
        return Order.objects.get(pk=order.pk)

    def test_snapshot_contains_numbers_for_bot(self):
        self.pay_one()
        data = hub_snapshot.build_snapshot()
        json.dumps(data)  # должно сериализоваться
        today = data["today"]
        self.assertEqual((today["revenue"], today["orders"], today["cash"]), (80000, 1, 80000))
        self.assertEqual(data["top_today"][0]["name_uz"], "Osh")
        self.assertEqual(data["top_today"][0]["qty"], 2)
        self.assertTrue(data["shift"]["open"])
        self.assertEqual(data["shift"]["cashier"], "Aziz")
        self.assertEqual(data["shopping"][0]["unit"], "g")
        self.assertEqual(data["shopping"][0]["level"], "out")
        self.assertEqual(data["tz"], "Asia/Tashkent")

    def test_profit_uses_real_stock_writeoffs(self):
        order = self.pay_one()
        write_off_order(order)
        today = hub_snapshot.build_snapshot()["today"]
        self.assertTrue(today["has_cost"])
        self.assertEqual(today["cost"], 12000)   # 2 порции × 300 г × 20 сум
        self.assertEqual(today["profit"], 68000)

    def test_empty_day_is_valid(self):
        data = hub_snapshot.build_snapshot()
        self.assertEqual(data["today"]["orders"], 0)
        self.assertIsNone(data["shift"])
        self.assertIsNone(data["week"]["best_day"])

    def test_shift_report_includes_profit_line(self):
        order = self.pay_one()
        write_off_order(order)
        shift = services.get_current_shift()
        self.assertEqual(telegram._shift_profit(shift), Decimal("68000"))
