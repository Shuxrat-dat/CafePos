import uuid
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from menu.models import Category, Dish, Modifier
from pos.models import Order, OrderItem

from . import intake
from .models import JumaOrder
from .test_client import JUMA, fake_call

User = get_user_model()


def juma_order(dish, **patch):
    """Заказ Juma из двух позиций: 2 шт блюда с доплатой 2000 (цена 25000 + 2000) = 54000."""
    data = {
        "id": str(uuid.uuid4()), "number": "J-10452", "status": "PLACED", "pickup_code": "4831",
        "customer": {"name": "Aziz", "phone": "+998901234567"}, "delivery": {"by": "TAXI"},
        "payment": {"method": "CARD_TRANSFER", "status": "WAITING_TRANSFER"},
        "items": [{"external_id": f"d-{dish.id}", "qty": "2.000", "unit_price": "25000.00", "comment": "без лука",
                   "modifiers": [{"group_external_id": "g-extra", "external_id": f"m-{dish.modifiers.first().id}", "price_delta": "2000.00"}],
                   "line_total": "54000.00"}],
        "items_total": "54000.00", "comment": "Позвоните за 10 минут",
    }
    data.update(patch)
    return data


def make_world():
    cashier = User.objects.filter(username="kassa").first() or User.objects.create_user(username="kassa", password="x", role=User.Role.CASHIER)
    dish = Dish.objects.create(category=Category.objects.create(name="Супы"), name="Борщ", price=25000)
    dish.modifiers.add(Modifier.objects.create(name="Сметана", price_delta=2000))
    return cashier, dish


@override_settings(**JUMA)
class IntakeTests(TestCase):
    def run_intake(self, orders, responses=None):
        answers = {("GET", "/orders/pending"): (200, {"orders": orders})}
        answers.update(responses or {})
        call = fake_call(answers)
        with mock.patch("juma.client._call", call):
            return intake.intake_once(), call

    def test_order_becomes_delivery_order_with_juma_prices_and_is_acknowledged(self):
        _cashier, dish = make_world()
        data = juma_order(dish)
        ok, call = self.run_intake([data])
        self.assertTrue(ok)
        link = JumaOrder.objects.get()
        order = link.order
        self.assertEqual((order.order_type, order.status, order.customer_name, order.customer_phone), (Order.OrderType.DELIVERY, Order.Status.OPEN, "Aziz", "+998901234567"))
        self.assertEqual(order.delivery_address, "Juma J-10452, код 4831")
        self.assertEqual(order.note, "Позвоните за 10 минут")
        item = order.items.get()
        self.assertEqual((item.dish, item.quantity, item.price, item.comment), (dish, 2, 25000, "без лука"))
        self.assertEqual(item.modifiers.get().price_delta, 2000)
        self.assertEqual(item.status, OrderItem.Status.NEW)  # на кухню пока не ушло
        self.assertEqual(order.totals["total"], 54000)
        self.assertEqual((link.number, link.pickup_code, link.status, link.items_total), ("J-10452", "4831", "SENT", 54000))
        self.assertEqual([c[1].split("?")[0] for c in call.calls], ["/orders/pending", f"/orders/{data['id']}/ack"])

    def test_juma_price_is_kept_even_if_menu_price_changed_meanwhile(self):
        _cashier, dish = make_world()
        data = juma_order(dish)
        Dish.objects.filter(pk=dish.pk).update(price=30000)
        self.run_intake([data])
        self.assertEqual(JumaOrder.objects.get().order.items.get().price, 25000)

    def test_same_order_is_not_created_twice_and_failed_ack_is_retried(self):
        _cashier, dish = make_world()
        data = juma_order(dish)
        ack = f"/orders/{data['id']}/ack"
        self.run_intake([data], {("POST", ack): (0, {})})
        self.assertEqual(JumaOrder.objects.get().status, "PLACED")
        _ok, call = self.run_intake([data])
        self.assertEqual((Order.objects.count(), JumaOrder.objects.count()), (1, 1))
        self.assertEqual(JumaOrder.objects.get().status, "SENT")
        self.assertIn(ack, [c[1].split("?")[0] for c in call.calls])
        _ok, call = self.run_intake([data])
        self.assertNotIn(ack, [c[1].split("?")[0] for c in call.calls])  # уже подтверждён

    def test_owner_is_notified_once_when_the_order_is_accepted_from_the_cloud(self):
        _cashier, dish = make_world()
        data = juma_order(dish)
        ack = f"/orders/{data['id']}/ack"
        with mock.patch("juma.intake.telegram.notify_juma_order") as notify:
            self.run_intake([data], {("POST", ack): (0, {})})
            notify.assert_not_called()  # подтверждения нет: заказ ещё не принят из облака
            self.run_intake([data])
            self.assertEqual(notify.call_count, 1)
            self.run_intake([data])
            self.assertEqual(notify.call_count, 1)  # повторный опрос не дублирует сообщение

    def test_unavailable_unknown_or_weighed_dish_is_rejected_and_nothing_is_created(self):
        _cashier, dish = make_world()
        for patch, setup in (
            ({}, lambda: Dish.objects.filter(pk=dish.pk).update(is_available=False)),
            ({}, lambda: Dish.objects.filter(pk=dish.pk).update(is_hidden=True, is_available=True)),
            ({}, lambda: Dish.objects.filter(pk=dish.pk).update(is_hidden=False, sold_by_weight=True)),
        ):
            setup()
            data = juma_order(dish, **patch)
            _ok, call = self.run_intake([data])
            reject = [c for c in call.calls if c[1].endswith("/reject")]
            self.assertEqual(reject[0][2], {"code": "ITEM_UNAVAILABLE", "items": [f"d-{dish.id}"]})
            self.assertEqual((Order.objects.count(), JumaOrder.objects.count()), (0, 0))
        data = juma_order(dish)
        data["items"][0]["external_id"] = "d-9999"
        Dish.objects.filter(pk=dish.pk).update(sold_by_weight=False)
        _ok, call = self.run_intake([data])
        self.assertEqual([c[2]["items"] for c in call.calls if c[1].endswith("/reject")], [["d-9999"]])

    def test_unknown_modifier_and_wrong_total_are_rejected(self):
        _cashier, dish = make_world()
        bad_modifier = juma_order(dish)
        bad_modifier["items"][0]["modifiers"][0]["external_id"] = "m-9999"
        _ok, call = self.run_intake([bad_modifier])
        self.assertEqual([c[2]["code"] for c in call.calls if c[1].endswith("/reject")], ["ITEM_UNAVAILABLE"])
        _ok, call = self.run_intake([juma_order(dish, items_total="50000.00")])
        self.assertEqual([c[2]["code"] for c in call.calls if c[1].endswith("/reject")], ["PRICE_CHANGED"])
        self.assertEqual(Order.objects.count(), 0)

    def test_without_staff_the_order_waits_in_juma(self):
        dish = Dish.objects.create(category=Category.objects.create(name="Супы"), name="Борщ", price=25000)
        dish.modifiers.add(Modifier.objects.create(name="Сметана", price_delta=2000))
        _ok, call = self.run_intake([juma_order(dish)])
        self.assertEqual([c[1].split("?")[0] for c in call.calls], ["/orders/pending"])
        self.assertEqual(Order.objects.count(), 0)

    def test_no_connection_is_reported_and_creates_nothing(self):
        ok, _call = self.run_intake([], {("GET", "/orders/pending"): (0, {})})
        self.assertFalse(ok)
