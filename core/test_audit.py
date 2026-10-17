from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from inventory.models import Product
from menu.models import Category, Dish
from pos import services
from pos.models import Order

from . import audit
from .models import AuditLog

User = get_user_model()
A = AuditLog.Action


class AuditHooksTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="own", password="x", role="owner")
        self.waiter = User.objects.create_user(username="wt", password="x", role="waiter", first_name="Азиз")
        category = Category.objects.create(name="Еда")
        self.dish = Dish.objects.create(category=category, name="Плов", price=40000)

    def _order(self, user=None):
        order = services.create_order(user or self.waiter, Order.OrderType.TAKEAWAY)
        order.items.create(dish=self.dish, quantity=1, price=self.dish.price)
        return order

    def test_log_entries_are_append_only(self):
        audit.record(self.owner, A.SHIFT_OPEN, amount=0)
        entry = AuditLog.objects.get()
        entry.detail = "изменено"
        with self.assertRaises(PermissionError):
            entry.save()
        with self.assertRaises(PermissionError):
            entry.delete()

    def test_record_never_raises(self):
        audit.record(self.owner, "x" * 80, amount="not a number")
        self.assertEqual(AuditLog.objects.count(), 0)

    def test_cancel_with_items_is_logged_but_empty_draft_is_not(self):
        order = self._order()
        services.cancel_order(order, self.waiter, "гость ушёл")
        entry = AuditLog.objects.get(action=A.ORDER_CANCEL)
        self.assertEqual((entry.order_id, int(entry.amount), entry.user_name), (order.id, 40000, "Азиз"))
        draft = services.create_order(self.waiter, Order.OrderType.TAKEAWAY)
        services.cancel_order(draft, self.waiter, "")
        self.assertEqual(AuditLog.objects.filter(action=A.ORDER_CANCEL).count(), 1)

    def test_shift_and_cash_events_are_logged(self):
        shift = services.open_shift(self.owner, initial_cash=1000)
        services.add_cash_movement(shift, "deposit", "500", "размен", self.owner)
        services.close_shift(shift, actual_cash="900", user=self.owner)
        actions = set(AuditLog.objects.values_list("action", flat=True))
        self.assertTrue({A.SHIFT_OPEN, A.CASH_MOVE, A.SHIFT_CLOSE} <= actions)
        closed = AuditLog.objects.get(action=A.SHIFT_CLOSE)
        self.assertIsNotNone(closed.amount)

    def test_price_change_is_logged_with_old_and_new_value(self):
        self.client.force_login(self.owner)
        response = self.client.post(reverse("dish_edit", args=[self.dish.id]), {
            "name": "Плов", "category": self.dish.category_id, "price": "45000", "sort_order": "0",
            "is_available": "on", "recipe-TOTAL_FORMS": "0", "recipe-INITIAL_FORMS": "0",
            "recipe-MIN_NUM_FORMS": "0", "recipe-MAX_NUM_FORMS": "1000",
        })
        self.assertEqual(response.status_code, 302)
        entry = AuditLog.objects.get(action=A.PRICE_CHANGE)
        self.assertIn("40000", entry.detail)
        self.assertIn("45000", entry.detail)

    def test_failed_login_is_recorded_without_password(self):
        self.client.post(reverse("login"), {"username": "own", "password": "WRONG-PASSWORD"})
        entry = AuditLog.objects.get(action=A.LOGIN_FAILED)
        self.assertIn("own", entry.detail)
        self.assertNotIn("WRONG-PASSWORD", entry.detail)

    def test_stock_writeoff_is_logged(self):
        product = Product.objects.create(name="Мука", unit="g", cost_per_unit=10)
        self.client.force_login(self.owner)
        self.client.post(reverse("stock_writeoff"), {"product": product.id, "quantity": "500", "comment": "порча"})
        entry = AuditLog.objects.get(action=A.STOCK_WRITEOFF)
        self.assertEqual(int(entry.amount), 5000)
        self.assertIn("порча", entry.detail)


class SuspiciousReportTests(TestCase):
    def setUp(self):
        self.users = [User.objects.create_user(username=f"w{i}", password="x", role="waiter") for i in range(4)]

    def _log(self, user, action, times):
        for _i in range(times):
            audit.record(user, action, amount=1000)

    def test_flags_only_the_outlier(self):
        self._log(self.users[0], A.ORDER_CANCEL, 12)
        for other in self.users[1:]:
            self._log(other, A.ORDER_CANCEL, 2)
        report = audit.suspicious_report()
        self.assertEqual([r["user_name"] for r in report], ["w0"])
        self.assertEqual(report[0]["flags"][0]["count"], 12)

    def test_quiet_team_has_no_flags(self):
        for user in self.users:
            self._log(user, A.ORDER_CANCEL, 3)
        self.assertEqual(audit.suspicious_report(), [])

    def test_old_events_are_ignored(self):
        self._log(self.users[0], A.ORDER_CANCEL, 12)
        AuditLog.objects.update(created_at=timezone.now() - timedelta(days=60))
        self.assertEqual(audit.suspicious_report(), [])

    def test_repeated_cash_shortage_is_flagged(self):
        for _i in range(2):
            audit.record(self.users[0], A.SHIFT_CLOSE, amount=-5000)
        report = audit.suspicious_report()
        self.assertEqual(report[0]["flags"][0]["count"], 2)


class AuditScreenTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="own", password="x", role="owner")
        self.waiter = User.objects.create_user(username="wt", password="x", role="waiter")
        audit.record(self.waiter, A.REFUND, order=5, amount=70000, detail="ошибка в заказе")
        audit.record(self.owner, A.SHIFT_OPEN, amount=0)

    def test_waiter_cannot_open_journal(self):
        self.client.force_login(self.waiter)
        self.assertEqual(self.client.get(reverse("manage_audit")).status_code, 403)

    def test_owner_sees_entries_and_filters_work(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("manage_audit"))
        self.assertContains(response, "ошибка в заказе")
        filtered = self.client.get(reverse("manage_audit"), {"action": A.SHIFT_OPEN})
        self.assertNotContains(filtered, "ошибка в заказе")

    def test_garbage_filters_do_not_break_the_page(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("manage_audit"), {"action": "x", "user": "abc", "days": "zzz", "page": "999"})
        self.assertEqual(response.status_code, 200)
