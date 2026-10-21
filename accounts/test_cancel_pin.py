"""Пароль отмены кассира: кассир задаёт его сам и вводит при отмене заказа, удалении блюда и возврате."""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import AuditLog
from menu.models import Category, Dish
from pos import services
from pos.models import Hall, Order, OrderItem, Table

from . import cancel_pin

User = get_user_model()
HTMX = {"HTTP_HX_REQUEST": "true"}


class Base(TestCase):
    def setUp(self):
        self.cashier = User.objects.create_user(username="kassir", password="Login-pass-91", role="cashier")
        cancel_pin.set_pin(self.cashier, "7391")
        self.owner = User.objects.create_user(username="egasi", password="Login-pass-91", role="owner")
        self.waiter = User.objects.create_user(username="ofitsiant", password="Login-pass-91", role="waiter")
        self.table = Table.objects.create(hall=Hall.objects.create(name="Зал"), number="1")
        category = Category.objects.create(name="Горячее")
        self.plov = Dish.objects.create(category=category, name="Плов", price=45000)
        self.client.force_login(self.cashier)

    def order(self, quantity=1, status=OrderItem.Status.NEW, waiter=None):
        order = Order.objects.create(table=self.table, waiter=waiter or self.cashier)
        item = OrderItem.objects.create(order=order, dish=self.plov, quantity=quantity, status=status)
        return order, item

    def cancel(self, order, pin=None, **extra):
        data = {"reason": "Гость ушёл"}
        if pin is not None:
            data["pin"] = pin
        return self.client.post(reverse("cancel_order", args=[order.id]), data, **extra)


class CancelOrderTests(Base):
    def test_wrong_or_missing_pin_does_not_cancel(self):
        order, _item = self.order()
        for pin in (None, "", "0000", "73910"):
            self.cancel(order, pin)
            order.refresh_from_db()
            self.assertEqual(order.status, Order.Status.OPEN, pin)

    def test_right_pin_cancels_and_is_logged(self):
        order, _item = self.order()
        self.cancel(order, "7391")
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.CANCELLED)
        self.assertEqual(order.cancelled_by, self.cashier)
        self.assertEqual(order.cancel_reason, "Гость ушёл")
        log = AuditLog.objects.get(action=AuditLog.Action.ORDER_CANCEL)
        self.assertEqual((log.order_id, log.user), (order.id, self.cashier))

    def test_window_shows_error_and_keeps_the_reason(self):
        order, _item = self.order()
        response = self.cancel(order, "0000", **HTMX)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Неверный пароль отмены. Осталось попыток: 4.")
        self.assertContains(response, "Гость ушёл")
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.OPEN)

    def test_window_closes_by_redirect_on_success(self):
        order, _item = self.order()
        response = self.cancel(order, "7391", **HTMX)
        self.assertEqual(response["HX-Redirect"], reverse("floor"))

    def test_window_asks_for_pin(self):
        order, _item = self.order()
        page = self.client.get(reverse("cancel_form", args=[order.id]))
        self.assertContains(page, 'name="pin"')

    def test_cashier_with_pin_can_cancel_order_already_in_kitchen(self):
        order, _item = self.order(status=OrderItem.Status.QUEUED)
        self.cancel(order, "7391")
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.CANCELLED)

    def test_cashier_without_pin_cannot_cancel_and_is_told_to_set_one(self):
        newbie = User.objects.create_user(username="yangi", password="Login-pass-91", role="cashier")
        self.client.force_login(newbie)
        order, _item = self.order(waiter=newbie)
        response = self.cancel(order, "7391", **HTMX)
        self.assertContains(response, "Сначала задайте свой пароль отмены")
        self.assertContains(response, reverse("cancel_pin_settings"))
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.OPEN)
        # Заказ уже на кухне: без пароля кассир по-прежнему не может его отменить
        sent, _item = self.order(status=OrderItem.Status.QUEUED, waiter=newbie)
        self.cancel(sent, "7391")
        sent.refresh_from_db()
        self.assertEqual(sent.status, Order.Status.OPEN)

    def test_owner_admin_and_waiter_keep_working_without_pin(self):
        order, _item = self.order()
        self.client.force_login(self.owner)
        self.cancel(order)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.CANCELLED)
        # Официант отменяет свой заказ, который ещё не ушёл на кухню, как и раньше
        self.client.force_login(self.waiter)
        mine, _item = self.order(waiter=self.waiter)
        self.cancel(mine)
        mine.refresh_from_db()
        self.assertEqual(mine.status, Order.Status.CANCELLED)


class RemoveDishTests(Base):
    def remove(self, order, item, pin=None):
        data = {} if pin is None else {"pin": pin}
        return self.client.post(reverse("remove_item", args=[order.id, item.id]), data)

    def test_dish_is_removed_only_with_the_right_pin(self):
        order, item = self.order()
        for pin in (None, "0000"):
            response = self.remove(order, item, pin)
            self.assertEqual(response["HX-Retarget"], "#modal")
            self.assertTrue(order.items.exists())
        self.remove(order, item, "7391")
        self.assertFalse(order.items.exists())

    def test_dish_already_in_kitchen_cashier_with_pin_removes_and_it_is_logged(self):
        order, item = self.order(status=OrderItem.Status.COOKING)
        with mock.patch("pos.views.telegram.notify_item_removed") as notify:
            self.remove(order, item, "7391")
        self.assertFalse(order.items.exists())
        log = AuditLog.objects.get(action=AuditLog.Action.ITEM_REMOVE)
        self.assertEqual((log.order_id, log.user), (order.id, self.cashier))
        notify.assert_called_once()

    def test_dish_in_kitchen_stays_for_cashier_without_pin(self):
        newbie = User.objects.create_user(username="yangi", password="Login-pass-91", role="cashier")
        self.client.force_login(newbie)
        order, item = self.order(status=OrderItem.Status.COOKING, waiter=newbie)
        response = self.remove(order, item, "7391")
        self.assertContains(response, "Сначала задайте свой пароль отмены")
        self.assertTrue(order.items.exists())

    def test_minus_on_last_portion_asks_for_pin_instead_of_deleting(self):
        order, item = self.order(quantity=1)
        response = self.client.post(reverse("change_qty", args=[order.id, item.id]), {"delta": "-1"})
        self.assertContains(response, "Убрать блюдо из заказа?")
        self.assertTrue(order.items.exists())

    def test_minus_with_several_portions_needs_no_pin(self):
        order, item = self.order(quantity=3)
        self.client.post(reverse("change_qty", args=[order.id, item.id]), {"delta": "-1"})
        item.refresh_from_db()
        self.assertEqual(item.quantity, 2)

    def test_trash_button_opens_the_pin_window_for_cashier_only(self):
        order, item = self.order()
        panel = self.client.get(reverse("order_panel", args=[order.id])).content.decode()
        self.assertIn(reverse("remove_item_form", args=[order.id, item.id]), panel)
        self.client.force_login(self.owner)
        panel = self.client.get(reverse("order_panel", args=[order.id])).content.decode()
        self.assertNotIn("remove/form", panel)

    def test_waiter_removes_own_new_dish_without_pin(self):
        self.client.force_login(self.waiter)
        order, item = self.order(waiter=self.waiter)
        self.remove(order, item)
        self.assertFalse(order.items.exists())


class RefundTests(Base):
    def paid_order(self):
        order, _item = self.order(status=OrderItem.Status.SERVED)
        services.pay_order(order, self.cashier, Order.PaymentMethod.CASH, Decimal("50000"), None)
        return order

    def test_refund_needs_pin(self):
        order = self.paid_order()
        url = reverse("refund_order", args=[order.id])
        self.client.post(url, {"reason": "Отказ гостя"})
        self.client.post(url, {"reason": "Отказ гостя", "pin": "0000"})
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PAID)
        self.client.post(url, {"reason": "Отказ гостя", "pin": "7391"})
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.CANCELLED)

    def test_owner_refunds_without_pin(self):
        order = self.paid_order()
        self.client.force_login(self.owner)
        self.client.post(reverse("refund_order", args=[order.id]), {"reason": "Ошибка пробития"})
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.CANCELLED)


class LockoutTests(Base):
    def test_five_wrong_pins_lock_it_and_the_owner_is_told(self):
        order, _item = self.order()
        with mock.patch("core.telegram.notify_pin_locked") as notify:
            for _n in range(5):
                self.cancel(order, "0000")
        self.cashier.refresh_from_db()
        self.assertTrue(self.cashier.cancel_pin_is_locked)
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.CANCEL_PIN_LOCKED).exists())
        notify.assert_called_once()
        # Пока заблокирован, даже верный пароль не принимается
        response = self.cancel(order, "7391", **HTMX)
        self.assertContains(response, "Слишком много неверных попыток")
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.OPEN)

    def test_lock_expires(self):
        order, _item = self.order()
        User.objects.filter(pk=self.cashier.pk).update(cancel_pin_locked_until=timezone.now() - timedelta(minutes=1))
        self.cancel(order, "7391")
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.CANCELLED)

    def test_right_pin_resets_the_counter(self):
        order, _item = self.order()
        for _n in range(4):
            self.cancel(order, "0000")
        other, _item = self.order()
        self.cancel(other, "7391")
        self.cashier.refresh_from_db()
        self.assertEqual(self.cashier.cancel_pin_failures, 0)

    def test_pin_is_stored_hashed(self):
        self.cashier.refresh_from_db()
        self.assertNotIn("7391", self.cashier.cancel_pin_hash)
        self.assertTrue(self.cashier.cancel_pin_hash.startswith(("pbkdf2_", "argon2", "bcrypt", "md5")))


class SetPinTests(Base):
    def setUp(self):
        super().setUp()
        self.newbie = User.objects.create_user(username="yangi", password="Login-pass-91", role="cashier")
        self.client.force_login(self.newbie)
        self.url = reverse("cancel_pin_settings")

    def post(self, **fields):
        data = {"account_password": "Login-pass-91", "pin": "4826", "pin2": "4826"}
        data.update(fields)
        return self.client.post(self.url, data)

    def test_cashier_sets_own_pin_and_it_works(self):
        response = self.post()
        self.assertEqual(response.status_code, 302)
        self.newbie.refresh_from_db()
        self.assertTrue(self.newbie.has_cancel_pin)
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.CANCEL_PIN_SET, user=self.newbie).exists())
        order, _item = self.order(waiter=self.newbie)
        self.cancel(order, "4826")
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.CANCELLED)

    def test_login_password_is_required(self):
        self.post(account_password="wrong-password")
        self.newbie.refresh_from_db()
        self.assertFalse(self.newbie.has_cancel_pin)

    def test_weak_pins_are_rejected(self):
        for pin in ("12", "1234", "0000", "kkkk", "yangi", "Login-pass-91"):
            self.post(pin=pin, pin2=pin)
            self.newbie.refresh_from_db()
            self.assertFalse(self.newbie.has_cancel_pin, pin)

    def test_mismatch_is_rejected(self):
        self.post(pin2="4827")
        self.newbie.refresh_from_db()
        self.assertFalse(self.newbie.has_cancel_pin)

    def test_pin_can_be_changed_and_old_one_stops_working(self):
        self.post()
        self.post(pin="9153", pin2="9153")
        order, _item = self.order(waiter=self.newbie)
        self.cancel(order, "4826")
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.OPEN)
        self.cancel(order, "9153")
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.CANCELLED)

    def test_page_is_only_for_roles_that_need_the_pin(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_menu_link_is_shown_to_cashier(self):
        page = self.client.get(reverse("floor"))
        self.assertContains(page, self.url)
        self.assertContains(page, "Пароль отмены")


class OwnerResetTests(Base):
    def test_owner_can_reset_but_not_set(self):
        self.client.force_login(self.owner)
        url = reverse("staff_edit", args=[self.cashier.id])
        page = self.client.get(url)
        self.assertContains(page, "Сбросить пароль отмены")
        self.assertNotContains(page, 'name="pin"')
        response = self.client.post(url, {
            "username": "kassir", "first_name": "", "last_name": "", "role": "cashier", "is_active": "on",
            "reset_cancel_pin": "on",
        })
        self.assertEqual(response.status_code, 302)
        self.cashier.refresh_from_db()
        self.assertFalse(self.cashier.has_cancel_pin)
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.CANCEL_PIN_SET, user=self.owner).exists())

    def test_reset_removes_the_lock_and_cashier_cannot_cancel_until_new_pin(self):
        User.objects.filter(pk=self.cashier.pk).update(
            cancel_pin_failures=3, cancel_pin_locked_until=timezone.now() + timedelta(minutes=5)
        )
        self.cashier.refresh_from_db()
        cancel_pin.reset_pin(self.cashier)
        self.cashier.refresh_from_db()
        self.assertEqual((self.cashier.cancel_pin_failures, self.cashier.cancel_pin_is_locked), (0, False))
        self.client.force_login(self.cashier)
        order, _item = self.order()
        self.cancel(order, "7391")
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.OPEN)

    def test_ordinary_save_keeps_the_pin(self):
        self.client.force_login(self.owner)
        self.client.post(reverse("staff_edit", args=[self.cashier.id]), {
            "username": "kassir", "first_name": "Malika", "last_name": "", "role": "cashier", "is_active": "on",
        })
        self.cashier.refresh_from_db()
        self.assertTrue(self.cashier.has_cancel_pin)
