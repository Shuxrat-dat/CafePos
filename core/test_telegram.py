import urllib.error
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from inventory.models import Product, RecipeItem, StockMovement
from inventory.services import write_off_order
from menu.models import Category, Dish
from pos import services
from pos.models import Order

from . import telegram
from .models import CafeSettings

User = get_user_model()
TOKEN = "123456789:" + "A" * 35


def configure(**extra):
    cafe = CafeSettings.get()
    cafe.telegram_bot_token = TOKEN
    cafe.telegram_chat_id = "555111"
    for key, value in extra.items():
        setattr(cafe, key, value)
    cafe.save()
    return cafe


@override_settings(TELEGRAM_SYNC=True)
class TelegramNotifyTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="own", password="x", role="owner")
        self.waiter = User.objects.create_user(username="wt", password="x", role="waiter")
        category = Category.objects.create(name="Еда")
        self.dish = Dish.objects.create(category=category, name="Плов", price=40000)

    def _order(self, with_item=True):
        order = services.create_order(self.waiter, Order.OrderType.TAKEAWAY)
        if with_item:
            order.items.create(dish=self.dish, quantity=2, price=self.dish.price)
        return order

    def test_nothing_is_sent_when_not_configured(self):
        with mock.patch("core.telegram._post") as post:
            telegram.notify_order_cancelled(self._order(), self.owner)
        post.assert_not_called()

    def test_cancel_message_contains_amount_and_reason(self):
        configure()
        order = self._order()
        order.cancel_reason = "Гость ушёл"
        with mock.patch("core.telegram._post", return_value=(True, "")) as post:
            telegram.notify_order_cancelled(order, self.owner)
        token, chat, text = post.call_args.args
        self.assertEqual((token, chat), (TOKEN, "555111"))
        self.assertIn("80 000", text)
        self.assertIn("Гость ушёл", text)

    def test_empty_draft_cancel_is_ignored(self):
        configure()
        with mock.patch("core.telegram._post") as post:
            telegram.notify_order_cancelled(self._order(with_item=False), self.owner)
        post.assert_not_called()

    def test_toggle_turns_event_off(self):
        configure(tg_notify_cancel=False)
        with mock.patch("core.telegram._post") as post:
            telegram.notify_order_cancelled(self._order(), self.owner)
        post.assert_not_called()

    def test_discount_below_threshold_is_not_reported(self):
        configure(tg_discount_percent=20)
        order = self._order()
        services.apply_discount(order, "percent", "10", "акция")
        with mock.patch("core.telegram._post") as post:
            telegram.notify_discount(order, self.owner)
        post.assert_not_called()

    def test_big_discount_is_reported(self):
        configure(tg_discount_percent=20)
        order = self._order()
        services.apply_discount(order, "percent", "30", "знакомый")
        with mock.patch("core.telegram._post", return_value=(True, "")) as post:
            telegram.notify_discount(order, self.owner)
        self.assertIn("30%", post.call_args.args[2])
        self.assertIn("знакомый", post.call_args.args[2])

    def test_shift_close_message_shows_difference(self):
        configure()
        shift = services.open_shift(self.owner, initial_cash=0)
        closed = services.close_shift(shift, actual_cash="5000", comment="недостача")
        with mock.patch("core.telegram._post", return_value=(True, "")) as post:
            telegram.notify_shift_closed(closed)
        text = post.call_args.args[2]
        self.assertIn(f"№{closed.id}", text)
        self.assertIn("Расхождение", text)

    def test_network_failure_never_raises(self):
        configure()
        with mock.patch("core.telegram.urllib.request.urlopen", side_effect=OSError("down")):
            ok, _reason = telegram.send_now("hello")
            self.assertFalse(ok)
            telegram.send("hello", "cancel")  # не должно выбросить исключение

    def test_error_text_never_contains_token(self):
        configure()
        error = urllib.error.HTTPError("https://api.telegram.org/bot%s/sendMessage" % TOKEN, 401, "bad", {}, None)
        with mock.patch("core.telegram.urllib.request.urlopen", side_effect=error):
            ok, reason = telegram.send_now("hello")
        self.assertFalse(ok)
        self.assertNotIn(TOKEN, reason)

    def test_low_stock_message_only_for_products_that_just_crossed(self):
        configure()
        flour = Product.objects.create(name="Мука", unit="g", min_stock=1000)
        salt = Product.objects.create(name="Соль", unit="g", min_stock=100)
        RecipeItem.objects.create(dish=self.dish, product=flour, quantity=500)
        RecipeItem.objects.create(dish=self.dish, product=salt, quantity=1)
        StockMovement.objects.create(product=flour, kind="income", quantity=2000)
        StockMovement.objects.create(product=salt, kind="income", quantity=5000)
        order = self._order()
        with mock.patch("core.telegram._post", return_value=(True, "")) as post:
            with self.captureOnCommitCallbacks(execute=True):
                write_off_order(order)
        text = post.call_args.args[2]
        self.assertIn("Мука", text)
        self.assertNotIn("Соль", text)


class TelegramSettingsScreenTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="own", password="x", role="owner")
        self.waiter = User.objects.create_user(username="wt", password="x", role="waiter")

    def test_waiter_has_no_access(self):
        self.client.force_login(self.waiter)
        self.assertEqual(self.client.get(reverse("manage_telegram")).status_code, 403)

    def test_saved_token_is_not_rendered_back(self):
        configure()
        self.client.force_login(self.owner)
        response = self.client.get(reverse("manage_telegram"))
        self.assertNotContains(response, TOKEN)
        self.assertContains(response, "Подключено")

    def test_blank_token_keeps_saved_one(self):
        configure()
        self.client.force_login(self.owner)
        self.client.post(reverse("manage_telegram"), {
            "action": "save", "telegram_bot_token": "", "telegram_chat_id": "777000",
            "tg_notify_shift": "on", "tg_discount_percent": "25",
        })
        cafe = CafeSettings.get()
        self.assertEqual(cafe.telegram_bot_token, TOKEN)
        self.assertEqual(cafe.telegram_chat_id, "777000")
        self.assertEqual(cafe.tg_discount_percent, 25)
        self.assertFalse(cafe.tg_notify_cancel)

    def test_invalid_token_and_chat_are_rejected(self):
        self.client.force_login(self.owner)
        response = self.client.post(reverse("manage_telegram"), {
            "action": "save", "telegram_bot_token": "not-a-token", "telegram_chat_id": "hello world",
            "tg_discount_percent": "20",
        })
        self.assertContains(response, "Токен выглядит неверно")
        self.assertContains(response, "Укажите номер чата")
        self.assertEqual(CafeSettings.get().telegram_bot_token, "")

    def test_test_button_reports_success_and_failure(self):
        self.client.force_login(self.owner)
        data = {"action": "test", "telegram_bot_token": TOKEN, "telegram_chat_id": "555111", "tg_discount_percent": "20"}
        with mock.patch("core.telegram._post", return_value=(True, "")):
            response = self.client.post(reverse("manage_telegram"), data, follow=True)
        self.assertContains(response, "Тестовое сообщение отправлено")
        with mock.patch("core.telegram._post", return_value=(False, "Неверный токен бота.")):
            response = self.client.post(reverse("manage_telegram"), data, follow=True)
        self.assertContains(response, "Неверный токен бота.")

    def test_disconnect_clears_credentials(self):
        configure()
        self.client.force_login(self.owner)
        self.client.post(reverse("manage_telegram"), {"action": "disconnect"})
        cafe = CafeSettings.get()
        self.assertEqual((cafe.telegram_bot_token, cafe.telegram_chat_id), ("", ""))
