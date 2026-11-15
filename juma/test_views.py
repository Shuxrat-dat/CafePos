from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import User

from .models import JumaOrder, JumaState
from .test_actions import make_link
from .test_client import JUMA, fake_call

S = JumaOrder.Status


@override_settings(**JUMA)
class BoardViewTests(TestCase):
    def login(self, user):
        self.client.force_login(user)

    def test_board_shows_order_and_buttons(self):
        cashier, link = make_link(S.SENT)
        self.login(cashier)
        response = self.client.get(reverse("juma_board"))
        self.assertContains(response, link.number)
        self.assertContains(response, "Принять")
        self.assertContains(response, "Отменить")

    def test_accept_button_calls_juma_and_rerenders_panel(self):
        cashier, link = make_link(S.SENT)
        self.login(cashier)
        call = fake_call(None, (200, {}))
        with mock.patch("juma.client._call", call):
            response = self.client.post(reverse("juma_accept", args=[link.id]), {"minutes": "25"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(call.calls[0][2], {"status": "ACCEPTED", "ready_in_minutes": 25})
        link.refresh_from_db()
        self.assertEqual(link.status, S.ACCEPTED)

    def test_failed_action_shows_error_text(self):
        cashier, link = make_link(S.SENT)
        self.login(cashier)
        with mock.patch("juma.client._call", fake_call(None, (0, {}))):
            response = self.client.post(reverse("juma_accept", args=[link.id]))
        self.assertContains(response, "Нет связи")

    def test_pause_toggles_accepting_orders(self):
        cashier, _link = make_link(S.SENT)
        self.login(cashier)
        self.client.post(reverse("juma_pause"))
        self.assertFalse(JumaState.get().accepting_orders)
        self.client.post(reverse("juma_pause"))
        self.assertTrue(JumaState.get().accepting_orders)

    def test_waiter_cannot_open_board(self):
        make_link(S.SENT)
        waiter = User.objects.create_user(username="waiter-juma", password="x", role="waiter")
        self.login(waiter)
        self.assertEqual(self.client.get(reverse("juma_board")).status_code, 403)
        self.assertEqual(self.client.post(reverse("juma_pause")).status_code, 403)

    def test_receipt_and_kitchen_ticket_print_for_juma_order(self):
        from printing import documents
        from printing.models import Printer

        _cashier, link = make_link(S.ACCEPTED)
        printer = Printer.objects.create(name="Касса", host="192.168.1.50")
        receipt = documents.receipt(link.order, printer)
        kitchen = documents.kitchen(link.order, list(link.order.items.all()), printer)
        for data in (receipt, kitchen):
            self.assertTrue(data)
        self.assertIn(link.pickup_code.encode("cp866", "ignore"), receipt)
        # На кухонном бланке: номер Juma и код выдачи.
        self.assertIn(f"Juma {link.number}".encode("cp866", "ignore"), kitchen)
        self.assertIn(link.pickup_code.encode("cp866", "ignore"), kitchen)

    def test_get_is_not_allowed_for_actions(self):
        cashier, link = make_link(S.SENT)
        self.login(cashier)
        self.assertEqual(self.client.get(reverse("juma_accept", args=[link.id])).status_code, 405)
