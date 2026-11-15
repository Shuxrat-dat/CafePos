from unittest import mock

from django.test import TestCase, override_settings

from pos import services
from pos.models import Order, OrderItem

from . import sync
from .models import JumaOrder, JumaState
from .test_actions import make_link, run
from .test_client import JUMA, fake_call

S, P = JumaOrder.Status, JumaOrder.Payment


def event(link, kind, to=None, actor="CUSTOMER", cursor=1, **details):
    return {"cursor": cursor, "order_id": str(link.juma_id), "order_number": link.number, "type": kind, "from": None, "to": to,
            "actor": actor, "details": details, "at": "2026-11-15T10:00:00Z"}


@override_settings(**JUMA)
class EventsTests(TestCase):
    def feed(self, events, next_cursor=None):
        body = {"events": events, "next_cursor": next_cursor if next_cursor is not None else max((e["cursor"] for e in events), default=0)}
        return run(sync.events_once, responses={("GET", "/events"): (200, body)})

    def test_customer_cancellation_cancels_the_order_on_the_register(self):
        _cashier, link = make_link(S.SENT)
        ok, call = self.feed([event(link, "CANCELLED", "CANCELLED", reason="CUSTOMER_CANCELLED", cursor=7)])
        self.assertTrue(ok)
        link.refresh_from_db()
        link.order.refresh_from_db()
        self.assertEqual((link.status, link.cancel_reason, link.order.status), (S.CANCELLED, "CUSTOMER_CANCELLED", Order.Status.CANCELLED))
        self.assertEqual(JumaState.get().events_cursor, 7)

    def test_owner_is_notified_about_cancellation_not_made_by_the_register(self):
        _cashier, link = make_link(S.SENT)
        with mock.patch("juma.sync.telegram.notify_juma_cancelled") as notify:
            self.feed([event(link, "CANCELLED", "CANCELLED", reason="CUSTOMER_CANCELLED", cursor=1)])
            self.assertEqual(notify.call_count, 1)
            self.assertEqual(notify.call_args.args[1], "CUSTOMER_CANCELLED")
            # Та же отмена повторно (событие пришло ещё раз): второго сообщения нет.
            self.feed([event(link, "CANCELLED", "CANCELLED", reason="CUSTOMER_CANCELLED", cursor=2)])
            self.assertEqual(notify.call_count, 1)

    def test_timeout_cancellation_of_a_paid_order_asks_for_refund(self):
        _cashier, link = make_link(S.ACCEPTED, P.PAID)
        self.feed([event(link, "CANCELLED", "CANCELLED", actor="SYSTEM", reason="POS_UNREACHABLE")])
        link.refresh_from_db()
        self.assertEqual((link.status, link.payment_status), (S.CANCELLED, P.REFUND_PENDING))

    def test_payment_and_status_events_are_mirrored(self):
        _cashier, link = make_link(S.ACCEPTED)
        self.feed([event(link, "PAYMENT_PAID", actor="MERCHANT", cursor=1), event(link, "DELIVERED", "DELIVERED", actor="DISPATCH", cursor=2)])
        link.refresh_from_db()
        self.assertEqual((link.payment_status, link.status), (P.PAID, S.DELIVERED))

    def test_unknown_orders_are_ignored_and_cursor_never_goes_back(self):
        _cashier, link = make_link()
        JumaState.objects.update_or_create(pk=1, defaults={"events_cursor": 50})
        stranger = {**event(link, "CANCELLED", "CANCELLED"), "order_id": "00000000-0000-4000-8000-000000000000"}
        self.feed([stranger], next_cursor=10)
        link.refresh_from_db()
        self.assertEqual((link.status, JumaState.get().events_cursor), (S.SENT, 50))

    def test_failure_keeps_cursor_and_reports_the_reason(self):
        JumaState.objects.update_or_create(pk=1, defaults={"events_cursor": 5})
        ok, _call = run(sync.events_once, default=(0, {}))
        self.assertFalse(ok)
        state = JumaState.get()
        self.assertEqual(state.events_cursor, 5)
        self.assertIn("Нет связи", state.last_error)


@override_settings(**JUMA)
class KitchenTests(TestCase):
    def test_cooking_then_ready_are_reported_once_each(self):
        _cashier, link = make_link(S.ACCEPTED)
        services.send_to_kitchen(link.order)
        services.start_cooking(link.order)
        _ok, call = run(sync.kitchen_once)
        self.assertEqual([c[2] for c in call.calls], [{"status": "PREPARING"}])
        link.refresh_from_db()
        self.assertEqual(link.status, S.PREPARING)
        _ok, call = run(sync.kitchen_once)
        self.assertEqual(call.calls, [])  # о том же дважды не сообщаем

        services.mark_items_ready(link.order.items.all())
        _ok, call = run(sync.kitchen_once)
        self.assertEqual([c[2] for c in call.calls], [{"status": "READY"}])
        link.refresh_from_db()
        self.assertEqual(link.status, S.READY)

    def test_orders_not_yet_on_the_kitchen_are_left_alone(self):
        make_link(S.ACCEPTED)
        _ok, call = run(sync.kitchen_once)
        self.assertEqual(call.calls, [])
