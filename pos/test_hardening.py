"""Проверки защиты после аудита: вход, QR стола, возвраты, скидки кассира, смена, персонал, кнопки зала."""
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from accounts import cancel_pin
from core.models import CafeSettings
from menu.models import Category, Dish

from . import guest_views, services
from .models import GuestOrder, Hall, Order, OrderItem, Table
from .testing import open_shift_for_tests

User = get_user_model()


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user(username="own", password="pass12345", role="owner")
        self.admin = User.objects.create_user(username="adm", password="pass12345", role="admin")
        self.cashier = User.objects.create_user(username="kas", password="pass12345", role="cashier")
        self.waiter = User.objects.create_user(username="ofi", password="pass12345", role="waiter")
        cancel_pin.set_pin(self.cashier, "7391")
        hall = Hall.objects.create(name="Зал")
        self.table = Table.objects.create(hall=hall, number="1")
        self.plov = Dish.objects.create(category=Category.objects.create(name="Горячее"), name="Плов", price=50000)

    def order(self, waiter=None):
        order = Order.objects.create(table=self.table, waiter=waiter or self.waiter)
        OrderItem.objects.create(order=order, dish=self.plov, quantity=2, status=OrderItem.Status.SERVED)
        return order

    def paid_order(self):
        order = self.order()
        services.pay_order(order, self.cashier, Order.PaymentMethod.CASH, Decimal("100000"), None)
        order.refresh_from_db()
        return order


class LoginGuardTests(Base):
    def _login(self, password):
        return self.client.post(reverse("login"), {"username": "ofi", "password": password})

    def test_login_is_locked_after_five_failures(self):
        for _ in range(5):
            self._login("wrong-pass")
        response = self._login("pass12345")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Слишком много неверных попыток")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_success_resets_counter(self):
        for _ in range(4):
            self._login("wrong-pass")
        self.assertEqual(self._login("pass12345").status_code, 302)
        self.client.logout()
        for _ in range(4):
            self._login("wrong-pass")
        self.assertEqual(self._login("pass12345").status_code, 302)

    def test_admin_site_login_is_guarded_too(self):
        for _ in range(5):
            self.client.post("/admin/login/", {"username": "ofi", "password": "wrong-pass"})
        response = self.client.post("/admin/login/", {"username": "ofi", "password": "pass12345"})
        self.assertContains(response, "Слишком много неверных попыток")


class TableQrKeyTests(Base):
    def test_order_without_key_is_refused(self):
        url = reverse("table_menu", args=[self.table.id])
        self.assertNotContains(self.client.get(url), "Отправить официанту")
        self.client.post(url, {f"qty_{self.plov.id}": "1"})
        self.assertFalse(GuestOrder.objects.exists())

    def test_wrong_key_is_refused(self):
        url = reverse("table_menu", args=[self.table.id]) + "?k=0000000000000000"
        self.client.post(url, {f"qty_{self.plov.id}": "1"})
        self.assertFalse(GuestOrder.objects.exists())

    def test_qr_page_prints_keyed_links(self):
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse("tables_qr")), self.table.menu_key)

    def test_one_address_hits_hourly_limit(self):
        cache.set("guest-hour:127.0.0.1", guest_views.MAX_ORDERS_PER_IP_HOUR, 3600)
        response = self.client.post(self.table.menu_path(), {f"qty_{self.plov.id}": "1"})
        self.assertEqual(response.context["error"], "Слишком много заказов. Попросите официанта.")
        self.assertFalse(GuestOrder.objects.exists())


class ShiftRequiredTests(Base):
    def test_payment_without_open_shift_is_refused(self):
        order = self.order()
        self.client.force_login(self.cashier)
        self.assertContains(self.client.get(reverse("pay", args=[order.id])), "Кассовая смена не открыта")
        response = self.client.post(reverse("pay", args=[order.id]), {"method": "cash", "received": "100000"})
        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.OPEN)

    def test_shift_open_ignores_foreign_next(self):
        self.client.force_login(self.cashier)
        response = self.client.post(reverse("shift_open"), {"initial_cash": "0", "next": "https://evil.example/"})
        self.assertEqual(response.headers["Location"], reverse("floor"))


class RefundControlTests(Base):
    def _old_paid_order(self):
        old_shift = open_shift_for_tests(self.cashier)
        order = self.paid_order()
        services.close_shift(old_shift, "100000")
        return order

    def test_cashier_cannot_refund_order_from_previous_shift(self):
        order = self._old_paid_order()
        open_shift_for_tests(self.cashier)
        self.client.force_login(self.cashier)
        self.client.post(reverse("refund_order", args=[order.id]), {"reason": "x", "pin": "7391"})
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PAID)

    def test_admin_refunds_old_order_and_owner_is_notified(self):
        order = self._old_paid_order()
        open_shift_for_tests(self.admin)
        self.client.force_login(self.admin)
        with mock.patch("core.telegram.notify_refund") as notify:
            self.client.post(reverse("refund_order", args=[order.id]), {"reason": "Ошибка"})
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.CANCELLED)
        notify.assert_called_once()

    def test_cashier_refunds_current_shift_order(self):
        open_shift_for_tests(self.cashier)
        order = self.paid_order()
        self.client.force_login(self.cashier)
        self.client.post(reverse("refund_order", args=[order.id]), {"reason": "x", "pin": "7391"})
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.CANCELLED)


class CashierDiscountCapTests(Base):
    def _discount(self, user, kind, value):
        order = self.order(waiter=user)
        self.client.force_login(user)
        self.client.post(reverse("discount_form", args=[order.id]), {"kind": kind, "value": value})
        order.refresh_from_db()
        return order

    def test_cashier_is_limited_by_setting(self):
        cafe = CafeSettings.get()
        cafe.cashier_max_discount = 10
        cafe.save()
        self.assertEqual(self._discount(self.cashier, "percent", "50").discount_kind, "")
        # 20 000 из 100 000 — это 20%, тоже больше предела
        self.assertEqual(self._discount(self.cashier, "amount", "20000").discount_kind, "")
        self.assertEqual(self._discount(self.cashier, "percent", "10").discount_value, Decimal("10"))

    def test_admin_is_not_limited(self):
        self.assertEqual(self._discount(self.admin, "percent", "100").discount_value, Decimal("100"))


class StaffManagementTests(Base):
    def test_admin_cannot_edit_another_admin(self):
        other = User.objects.create_user(username="adm2", password="pass12345", role="admin")
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse("staff_edit", args=[other.id])).status_code, 403)

    def test_admin_cannot_create_admin(self):
        self.client.force_login(self.admin)
        self.client.post(reverse("staff_create"), {
            "username": "newadm", "first_name": "X", "role": "admin", "is_active": "on", "password": "Zx9-long-pass",
        })
        self.assertFalse(User.objects.filter(username="newadm").exists())

    def test_admin_edits_self_and_waiter(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse("staff_edit", args=[self.admin.id])).status_code, 200)
        self.assertEqual(self.client.get(reverse("staff_edit", args=[self.waiter.id])).status_code, 200)

    def test_owner_edits_admin(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("staff_edit", args=[self.admin.id])).status_code, 200)


class FloorButtonsTests(Base):
    def test_waiter_sees_no_pay_links(self):
        order = self.order()
        self.client.force_login(self.waiter)
        page = self.client.get(reverse("floor") + f"?order_id={order.id}")
        self.assertNotContains(page, reverse("pay", args=[order.id]))

    def test_cashier_sees_pay_link(self):
        order = self.order(waiter=self.cashier)
        self.client.force_login(self.cashier)
        page = self.client.get(reverse("floor") + f"?order_id={order.id}")
        self.assertContains(page, reverse("pay", args=[order.id]))
