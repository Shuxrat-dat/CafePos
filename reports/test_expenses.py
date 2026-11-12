from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from menu.models import Category, Dish
from pos.models import Order, OrderItem

from .models import Expense

User = get_user_model()


class ExpenseTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="owner_exp", password="pass12345", role="owner")
        self.cashier = User.objects.create_user(username="cashier_exp", password="pass12345", role="cashier")
        self.today = timezone.localdate()

    def test_owner_adds_expense(self):
        self.client.force_login(self.owner)
        response = self.client.post(reverse("expenses"), {
            "date": self.today.isoformat(), "category": "rent", "amount": "3000000", "comment": "Ноябрь",
        })
        self.assertEqual(response.status_code, 302)
        expense = Expense.objects.get()
        self.assertEqual(expense.amount, Decimal("3000000"))
        self.assertEqual(expense.created_by, self.owner)

    def test_amount_must_be_positive(self):
        self.client.force_login(self.owner)
        response = self.client.post(reverse("expenses"), {
            "date": self.today.isoformat(), "category": "rent", "amount": "0",
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Expense.objects.exists())

    def test_cashier_cannot_see_expenses(self):
        self.client.force_login(self.cashier)
        self.assertEqual(self.client.get(reverse("expenses")).status_code, 403)
        self.assertEqual(self.client.post(reverse("expenses"), {"amount": "1"}).status_code, 403)

    def test_page_shows_only_period_total(self):
        Expense.objects.create(date=self.today, category="rent", amount=1000)
        Expense.objects.create(date=self.today - timedelta(days=400), category="rent", amount=999999)
        self.client.force_login(self.owner)
        response = self.client.get(reverse("expenses"))
        self.assertEqual(response.context["total"], Decimal("1000"))

    def test_delete_expense(self):
        expense = Expense.objects.create(date=self.today, category="other", amount=500)
        self.client.force_login(self.owner)
        self.client.post(reverse("expense_delete", args=[expense.id]))
        self.assertFalse(Expense.objects.exists())

    def test_dashboard_net_profit_subtracts_expenses(self):
        category = Category.objects.create(name="Горячее")
        dish = Dish.objects.create(category=category, name="Плов", price=50000)
        order = Order.objects.create(waiter=self.cashier, order_type=Order.OrderType.TAKEAWAY,
                                     status=Order.Status.PAID, closed_at=timezone.now(), payment_method="cash")
        OrderItem.objects.create(order=order, dish=dish, quantity=2, price=50000, status=OrderItem.Status.SERVED)
        Expense.objects.create(date=self.today, category="utilities", amount=30000)
        self.client.force_login(self.owner)
        response = self.client.get(reverse("reports"))
        self.assertEqual(response.context["expenses_total"], Decimal("30000"))
        self.assertEqual(response.context["net_profit"], response.context["revenue"] - Decimal("30000"))
        self.assertContains(response, "Чистая прибыль")
