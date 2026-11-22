from unittest import mock

from django.test import TestCase, override_settings

from pos import services
from pos.models import Order, OrderItem, Payment

from . import actions, intake
from .models import JumaOrder
from .test_client import JUMA, fake_call
from .test_intake import juma_order, make_world

S, P = JumaOrder.Status, JumaOrder.Payment


def make_link(status=S.SENT, payment=P.WAITING_TRANSFER):
    cashier, dish = make_world()
    link = intake.import_order(juma_order(dish))
    JumaOrder.objects.filter(pk=link.pk).update(status=status, payment_status=payment)
    link.refresh_from_db()
    return cashier, link


def run(function, *args, responses=None, default=(200, {})):
    call = fake_call(responses, default)
    with mock.patch("juma.client._call", call):
        result = function(*args)
    return result, call


@override_settings(**JUMA)
class AcceptAndPayTests(TestCase):
    def test_accept_tells_juma_the_time_and_sends_dishes_to_the_kitchen(self):
        _cashier, link = make_link()
        (ok, text), call = run(actions.accept, link, 25)
        self.assertEqual((ok, text), (True, ""))
        self.assertEqual(call.calls[0][1:], (f"/orders/{link.juma_id}/status", {"status": "ACCEPTED", "ready_in_minutes": 25}))
        link.refresh_from_db()
        self.assertEqual(link.status, S.ACCEPTED)
        self.assertIsNotNone(link.accepted_at)
        self.assertEqual(link.order.items.get().status, OrderItem.Status.QUEUED)

    def test_failed_accept_changes_nothing_on_the_register(self):
        _cashier, link = make_link()
        (ok, text), _call = run(actions.accept, link, None, default=(409, {"error": {"details": {"from": "CANCELLED"}}}))
        self.assertFalse(ok)
        self.assertIn("изменилось", text)
        link.refresh_from_db()
        self.assertEqual((link.status, link.order.items.get().status), (S.SENT, OrderItem.Status.NEW))
        (ok, text), _call = run(actions.accept, link, None, default=(0, {}))
        self.assertIn("Нет связи", text)

    def test_already_accepted_order_cannot_be_accepted_again(self):
        _cashier, link = make_link(S.ACCEPTED)
        (ok, _text), call = run(actions.accept, link)
        self.assertFalse(ok)
        self.assertEqual(call.calls, [])

    def test_paid_sends_exact_amount(self):
        _cashier, link = make_link(S.ACCEPTED)
        (ok, _text), call = run(actions.mark_paid, link)
        self.assertTrue(ok)
        self.assertEqual(call.calls[0][2], {"status": "PAID", "amount": "54000.00"})
        link.refresh_from_db()
        self.assertEqual(link.payment_status, P.PAID)

    def test_paid_before_acceptance_is_refused_by_juma_and_stays_unpaid(self):
        _cashier, link = make_link(S.SENT)
        body = {"error": {"details": {"reason": "ORDER_NOT_ACCEPTED"}}}
        (ok, _text), _call = run(actions.mark_paid, link, default=(409, body))
        self.assertFalse(ok)
        link.refresh_from_db()
        self.assertEqual(link.payment_status, P.WAITING_TRANSFER)

    def test_ready_marks_kitchen_items_ready(self):
        _cashier, link = make_link(S.ACCEPTED)
        services.send_to_kitchen(link.order)
        (ok, _text), call = run(actions.mark_ready, link)
        self.assertTrue(ok)
        self.assertEqual(call.calls[0][2], {"status": "READY"})
        self.assertEqual(link.order.items.get().status, OrderItem.Status.READY)
        link.refresh_from_db()
        self.assertEqual(link.status, S.READY)


@override_settings(**JUMA)
class HandOverTests(TestCase):
    def ready_link(self):
        cashier, link = make_link(S.READY, P.PAID)
        return cashier, link

    def test_handover_needs_payment_code_and_open_shift(self):
        cashier, link = make_link(S.READY, P.WAITING_TRANSFER)
        self.assertEqual(run(actions.hand_over, link, cashier, "4831")[0], (False, "Заказ не оплачен."))
        cashier, link = self.ready_link()
        self.assertEqual(run(actions.hand_over, link, cashier, "0000")[0], (False, "Неверный код выдачи."))
        ok, text = run(actions.hand_over, link, cashier, "4831")[0]
        self.assertFalse(ok)
        self.assertIn("смен", text.lower())
        self.assertEqual(Payment.objects.count(), 0)

    def test_handover_pays_the_order_as_card_in_the_current_shift(self):
        cashier, link = self.ready_link()
        shift = services.open_shift(cashier, 0)
        (ok, text), call = run(actions.hand_over, link, cashier, "4831")
        self.assertEqual((ok, text), (True, ""))
        self.assertEqual(call.calls[0][2], {"status": "HANDED_OVER", "pickup_code": "4831"})
        link.refresh_from_db()
        self.assertEqual(link.status, S.PICKED_UP)
        order = link.order
        order.refresh_from_db()
        self.assertEqual((order.status, order.payment_method, order.cashier), (Order.Status.PAID, Order.PaymentMethod.CARD, cashier))
        payment = Payment.objects.get()
        self.assertEqual((payment.amount, payment.card_amount, payment.cash_amount, payment.shift), (54000, 54000, 0, shift))
        self.assertEqual(payment.idempotency_key, f"juma-{link.juma_id}")

    def test_when_juma_refuses_nothing_is_paid_on_the_register(self):
        cashier, link = self.ready_link()
        services.open_shift(cashier, 0)
        body = {"error": {"details": {"reason": "PICKUP_CODE_MISMATCH"}}}
        (ok, text), _call = run(actions.hand_over, link, cashier, "4831", default=(409, body))
        self.assertEqual((ok, text), (False, "Неверный код выдачи."))
        link.refresh_from_db()
        self.assertEqual((link.status, Payment.objects.count()), (S.READY, 0))

    def test_when_no_driver_is_called_yet_nothing_is_paid_and_cashier_is_told_to_wait(self):
        cashier, link = self.ready_link()
        services.open_shift(cashier, 0)
        body = {"error": {"details": {"reason": "NO_DELIVERY"}}}
        (ok, text), _call = run(actions.hand_over, link, cashier, "4831", default=(409, body))
        self.assertEqual((ok, text), (False, "Водитель ещё не вызван, подождите."))
        link.refresh_from_db()
        self.assertEqual((link.status, Payment.objects.count()), (S.READY, 0))


@override_settings(**JUMA)
class CancelAndRefundTests(TestCase):
    def test_cancel_before_payment_cancels_the_order_on_the_register(self):
        cashier, link = make_link(S.SENT)
        (ok, _text), call = run(actions.cancel, link, cashier, "OUT_OF_STOCK")
        self.assertTrue(ok)
        self.assertEqual(call.calls[0][2], {"status": "CANCELLED", "reason": "OUT_OF_STOCK"})
        link.refresh_from_db()
        link.order.refresh_from_db()
        self.assertEqual((link.status, link.payment_status, link.cancel_reason), (S.CANCELLED, P.WAITING_TRANSFER, "OUT_OF_STOCK"))
        self.assertEqual(link.order.status, Order.Status.CANCELLED)
        self.assertEqual(run(actions.cancel, link, cashier)[0], (True, ""))  # повтор не ошибка

    def test_cancel_after_payment_goes_to_refund_and_refund_can_be_confirmed(self):
        cashier, link = make_link(S.ACCEPTED, P.PAID)
        run(actions.cancel, link, cashier, "UNKNOWN_REASON")
        link.refresh_from_db()
        self.assertEqual((link.status, link.payment_status), (S.CANCELLED, P.REFUND_PENDING))
        (ok, _text), call = run(actions.confirm_refund, link)
        self.assertTrue(ok)
        self.assertEqual(call.calls[0][2], {"status": "REFUNDED"})
        link.refresh_from_db()
        self.assertEqual(link.payment_status, P.REFUNDED)

    def test_cancel_is_refused_after_handover_and_when_juma_refuses(self):
        cashier, link = make_link(S.PICKED_UP, P.PAID)
        self.assertFalse(run(actions.cancel, link, cashier)[0][0])
        cashier, link = make_link(S.SENT)
        self.assertFalse(run(actions.cancel, link, cashier, default=(409, {}))[0][0])
        link.refresh_from_db()
        self.assertEqual((link.status, link.order.status), (S.SENT, Order.Status.OPEN))
