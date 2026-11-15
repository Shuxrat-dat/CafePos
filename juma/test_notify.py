from unittest import mock

from django.test import TestCase, override_settings

from core import telegram
from core.test_telegram import configure

from .models import JumaOrder
from .test_actions import make_link

S, P = JumaOrder.Status, JumaOrder.Payment


@override_settings(TELEGRAM_SYNC=True)
class JumaOwnerNotificationTests(TestCase):
    def test_new_order_message_has_number_and_sum_but_no_personal_data(self):
        _cashier, link = make_link(S.SENT)
        configure()
        with mock.patch("core.telegram._post", return_value=(True, "")) as post:
            self.assertTrue(telegram.notify_juma_order(link))
        text = post.call_args.args[2]
        self.assertIn(link.number, text)
        self.assertIn("54 000", text)
        self.assertIn("Принять", text)
        # Имя, телефон и адрес покупателя в общий бот не уходят.
        self.assertNotIn("Aziz", text)
        self.assertNotIn("+998", text)
        self.assertNotIn(link.pickup_code, text)

    def test_nothing_is_sent_when_the_setting_is_off_or_telegram_is_not_connected(self):
        _cashier, link = make_link(S.SENT)
        with mock.patch("core.telegram._post") as post:
            self.assertFalse(telegram.notify_juma_order(link))  # Telegram не подключён
            configure(tg_notify_orders=False)
            self.assertFalse(telegram.notify_juma_order(link))
            self.assertFalse(telegram.notify_juma_cancelled(link, "CUSTOMER_CANCELLED"))
        post.assert_not_called()

    def test_cancellation_message_explains_the_reason_and_asks_for_a_refund_when_paid(self):
        _cashier, link = make_link(S.CANCELLED, P.REFUND_PENDING)
        configure()
        with mock.patch("core.telegram._post", return_value=(True, "")) as post:
            telegram.notify_juma_cancelled(link, "PAYMENT_TIMEOUT")
        text = post.call_args.args[2]
        self.assertIn(link.number, text)
        self.assertIn("вышло время оплаты", text)
        self.assertIn("Возврат выполнен", text)

    def test_unpaid_cancellation_does_not_ask_for_a_refund(self):
        _cashier, link = make_link(S.CANCELLED, P.WAITING_TRANSFER)
        configure()
        with mock.patch("core.telegram._post", return_value=(True, "")) as post:
            telegram.notify_juma_cancelled(link, "CUSTOMER_CANCELLED")
        text = post.call_args.args[2]
        self.assertIn("покупатель отменил заказ", text)
        self.assertNotIn("Возврат выполнен", text)

    def test_a_telegram_failure_never_breaks_the_register(self):
        _cashier, link = make_link(S.SENT)
        configure()
        with mock.patch("core.telegram._post", side_effect=RuntimeError("boom")):
            self.assertFalse(telegram.notify_juma_order(link))
