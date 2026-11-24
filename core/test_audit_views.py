from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from menu.models import Category, Dish
from pos import services
from pos.models import Hall, Order, Table

from .models import AuditLog

User = get_user_model()
A = AuditLog.Action


class OrderViewAuditTests(TestCase):
    """Журнал: какие столы открывал сотрудник и что добавлял."""

    def setUp(self):
        self.cashier = User.objects.create_user(username="kass_aud", password="x", role="cashier")
        self.owner = User.objects.create_user(username="own_aud", password="x", role="owner")
        self.dish = Dish.objects.create(category=Category.objects.create(name="Еда"), name="Плов", price=40000)
        table = Table.objects.create(hall=Hall.objects.create(name="Зал"), number="5", seats=4)
        self.order = services.create_order(self.cashier, Order.OrderType.DINE_IN, table=table)

    def test_opening_order_is_logged_once_per_ten_minutes(self):
        self.client.force_login(self.cashier)
        for _n in range(3):
            self.client.get(reverse("order_detail", args=[self.order.id]))
        entry = AuditLog.objects.get(action=A.ORDER_VIEW)
        self.assertEqual(entry.user, self.cashier)
        self.assertEqual(entry.order_id, self.order.id)
        self.assertIn("5", entry.detail)

    def test_owner_views_are_not_logged(self):
        self.client.force_login(self.owner)
        self.client.get(reverse("order_detail", args=[self.order.id]))
        self.assertFalse(AuditLog.objects.filter(action=A.ORDER_VIEW).exists())

    def test_adding_dish_is_logged(self):
        self.client.force_login(self.cashier)
        self.client.post(reverse("add_item", args=[self.order.id, self.dish.id]), {"quantity": "2"})
        entry = AuditLog.objects.get(action=A.ITEM_ADD)
        self.assertIn("Плов", entry.detail)
        self.assertEqual(int(entry.amount), 80000)

    def test_journal_hides_views_until_chosen(self):
        self.client.force_login(self.cashier)
        self.client.get(reverse("order_detail", args=[self.order.id]))
        self.client.force_login(self.owner)
        everything = self.client.get(reverse("manage_audit"), {"days": "all"})
        self.assertEqual(len(everything.context["page"]), 0)
        chosen = self.client.get(reverse("manage_audit"), {"action": "order_view", "days": "all"})
        self.assertEqual(len(chosen.context["page"]), 1)
