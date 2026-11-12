from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from menu.models import Category, Dish
from pos.models import Order, OrderItem
from reports.models import Expense
from reports.views import payroll_rows

from .models import User, WorkShift


class ClockTests(TestCase):
    def setUp(self):
        self.cook = User.objects.create_user(username="cook_clock", password="pass12345", role="cook")

    def test_clock_in_and_out(self):
        self.client.force_login(self.cook)
        self.client.post(reverse("clock"), {"next": "/kitchen/"})
        shift = WorkShift.objects.get(user=self.cook)
        self.assertIsNone(shift.ended_at)
        response = self.client.post(reverse("clock"), {"next": "/kitchen/"})
        self.assertRedirects(response, "/kitchen/", fetch_redirect_response=False)
        shift.refresh_from_db()
        self.assertIsNotNone(shift.ended_at)
        self.assertEqual(WorkShift.objects.count(), 1)

    def test_get_is_not_allowed(self):
        self.client.force_login(self.cook)
        self.assertEqual(self.client.get(reverse("clock")).status_code, 404)

    def test_foreign_redirect_is_ignored(self):
        self.client.force_login(self.cook)
        response = self.client.post(reverse("clock"), {"next": "https://evil.example/"})
        self.assertRedirects(response, "/", fetch_redirect_response=False)

    def test_hours_are_clipped_to_period(self):
        now = timezone.now()
        shift = WorkShift(user=self.cook, started_at=now - timedelta(hours=10), ended_at=now)
        self.assertAlmostEqual(shift.hours_within(now - timedelta(hours=4), now + timedelta(hours=1)), 4.0)


class PayrollTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="owner_pay", password="pass12345", role="owner")
        self.cashier = User.objects.create_user(username="cashier_pay", password="pass12345", role="cashier")
        self.waiter = User.objects.create_user(
            username="waiter_pay", password="pass12345", role="waiter", first_name="Jasur",
            hourly_rate=Decimal("10000"), sales_percent=Decimal("5"),
        )
        now = timezone.now()
        WorkShift.objects.create(user=self.waiter, started_at=now - timedelta(hours=8), ended_at=now)
        category = Category.objects.create(name="Горячее")
        dish = Dish.objects.create(category=category, name="Плов", price=Decimal("50000"))
        order = Order.objects.create(waiter=self.waiter, order_type=Order.OrderType.TAKEAWAY,
                                     status=Order.Status.PAID, closed_at=now, payment_method="cash")
        OrderItem.objects.create(order=order, dish=dish, quantity=2, price=dish.price, status=OrderItem.Status.SERVED)
        self.today = timezone.localdate()

    def row(self):
        return next(r for r in payroll_rows(self.today - timedelta(days=1), self.today) if r["user"] == self.waiter)

    def test_salary_is_hours_times_rate_plus_percent_of_sales(self):
        row = self.row()
        self.assertAlmostEqual(row["hours"], 8.0, places=1)
        self.assertEqual(row["by_hours"], Decimal("80000"))
        self.assertEqual(row["by_sales"], Decimal("5000"))
        self.assertEqual(row["total"], Decimal("85000"))

    def test_to_expense_once(self):
        self.client.force_login(self.owner)
        data = {"action": "to_expense", "user_id": self.waiter.id,
                "from": (self.today - timedelta(days=1)).isoformat(), "to": self.today.isoformat()}
        self.client.post(reverse("payroll"), data)
        self.client.post(reverse("payroll"), data)
        expense = Expense.objects.get()
        self.assertEqual(expense.category, Expense.Category.SALARY)
        self.assertEqual(expense.amount, self.row()["total"])

    def test_owner_closes_forgotten_shift(self):
        open_shift = WorkShift.objects.create(user=self.cashier)
        self.client.force_login(self.owner)
        self.client.post(reverse("payroll"), {"action": "close_shift", "shift_id": open_shift.id})
        open_shift.refresh_from_db()
        self.assertEqual(open_shift.closed_by, self.owner)

    def test_page_and_access(self):
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse("payroll")), "Jasur")
        self.client.force_login(self.cashier)
        self.assertEqual(self.client.get(reverse("payroll")).status_code, 403)

    def test_staff_form_rejects_bad_percent(self):
        self.client.force_login(self.owner)
        response = self.client.post(reverse("staff_edit", args=[self.waiter.id]), {
            "username": "waiter_pay", "first_name": "Jasur", "role": "waiter", "is_active": "on",
            "hourly_rate": "10000", "sales_percent": "150",
        })
        self.assertEqual(response.status_code, 200)
        self.waiter.refresh_from_db()
        self.assertEqual(self.waiter.sales_percent, Decimal("5"))
