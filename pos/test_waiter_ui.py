from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from menu.models import Category, Dish
from pos import services
from pos.models import Order, OrderItem

User = get_user_model()


class WaiterOrderScreenTests(TestCase):
    """Экран заказа официанта: корзина-шторка с живым счётчиком на телефоне. У остальных ролей вид прежний."""

    def setUp(self):
        self.waiter = User.objects.create_user(username="w_ui", password="pass12345", role="waiter")
        self.cashier = User.objects.create_user(username="c_ui", password="pass12345", role="cashier")
        category = Category.objects.create(name="Горячее")
        self.dish = Dish.objects.create(category=category, name="Плов", price=Decimal("50000"))
        self.order = services.create_order(self.waiter, Order.OrderType.TAKEAWAY)
        OrderItem.objects.create(order=self.order, dish=self.dish, quantity=2, price=self.dish.price)

    def page(self, user):
        self.client.force_login(user)
        return self.client.get(reverse("order_detail", args=[self.order.id])).content.decode()

    def test_waiter_gets_bottom_bar_and_sheet(self):
        html = self.page(self.waiter)
        for marker in ('id="cart-bar"', 'id="cart-backdrop"', "toggleCart", "wdgrid", "wdish"):
            self.assertIn(marker, html)

    def test_cart_panel_exposes_count_and_total(self):
        html = self.page(self.waiter)
        self.assertIn('id="cart-totals"', html)
        self.assertIn('data-count="1"', html)

    def test_cashier_has_the_same_terminal_layout(self):
        """Один экран заказа для всех: категории слева, плотные карточки блюд, заказ справа."""
        html = self.page(self.cashier)
        self.assertIn('id="cart-bar"', html)
        self.assertIn("wdgrid", html)
        self.assertIn('class="cat-btn chip-active"', html)
        self.assertNotIn('data-cat="" ', html)
