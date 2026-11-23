from django.contrib.auth import get_user_model
from django.test import TestCase

from menu.models import Category, Dish
from pos.models import Order

User = get_user_model()


class DishCardBadgeTests(TestCase):
    """Число порций на карточке блюда считается в браузере по строкам корзины: у строки должен быть id блюда."""

    def setUp(self):
        self.waiter = User.objects.create_user(username="ofi_badge", password="pass12345", role="waiter")
        self.client.force_login(self.waiter)
        category = Category.objects.create(name="Еда")
        self.dish = Dish.objects.create(category=category, name="Бульон", price=3000)
        self.order = Order.objects.create(waiter=self.waiter, order_type="takeaway")

    def test_card_has_badge_and_cart_line_has_dish_id_and_quantity(self):
        self.order.items.create(dish=self.dish, quantity=3, price=self.dish.price)
        response = self.client.get(f"/orders/{self.order.id}/")
        self.assertContains(response, f'data-dish-pk="{self.dish.id}"')
        self.assertContains(response, "data-dish-badge")
        self.assertContains(response, f'data-line-dish="{self.dish.id}" data-line-qty="3"')

    def test_panel_refresh_keeps_line_data(self):
        self.order.items.create(dish=self.dish, quantity=2, price=self.dish.price)
        response = self.client.get(f"/orders/{self.order.id}/panel/")
        self.assertContains(response, f'data-line-dish="{self.dish.id}" data-line-qty="2"')
