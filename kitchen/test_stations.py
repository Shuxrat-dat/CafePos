from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from menu.models import Category, Dish
from pos.models import Order, OrderItem

User = get_user_model()


class StationTests(TestCase):
    """Цеха: бар видит только напитки и отмечает готовыми только их."""

    def setUp(self):
        self.cook = User.objects.create_user(username="cook_st", password="pass12345", role="cook")
        waiter = User.objects.create_user(username="waiter_st", password="pass12345", role="waiter")
        hot = Category.objects.create(name="Горячее", station="hot")
        bar = Category.objects.create(name="Напитки", station="bar")
        plov = Dish.objects.create(category=hot, name="Плов", price=25000)
        tea = Dish.objects.create(category=bar, name="Чай", price=5000)
        self.order = Order.objects.create(waiter=waiter, order_type=Order.OrderType.TAKEAWAY)
        self.plov = OrderItem.objects.create(order=self.order, dish=plov, quantity=1, price=25000, status=OrderItem.Status.QUEUED)
        self.tea = OrderItem.objects.create(order=self.order, dish=tea, quantity=1, price=5000, status=OrderItem.Status.QUEUED)
        self.client.force_login(self.cook)

    def test_board_shows_station_tabs_when_several_stations(self):
        page = self.client.get(reverse("kitchen_board"))
        self.assertContains(page, 'id="station-tabs"')
        self.assertContains(page, "Бар")

    def test_bar_sees_only_drinks(self):
        html = self.client.get(reverse("kitchen_tickets") + "?station=bar").content.decode()
        self.assertIn("Чай", html)
        self.assertNotIn("Плов", html)

    def test_all_stations_see_everything(self):
        html = self.client.get(reverse("kitchen_tickets")).content.decode()
        self.assertIn("Чай", html)
        self.assertIn("Плов", html)

    def test_bar_ready_touches_only_drinks(self):
        self.client.post(reverse("ticket_ready", args=[self.order.id]), {"station": "bar"})
        self.tea.refresh_from_db()
        self.plov.refresh_from_db()
        self.assertEqual(self.tea.status, OrderItem.Status.READY)
        self.assertEqual(self.plov.status, OrderItem.Status.QUEUED)

    def test_bar_start_touches_only_drinks(self):
        self.client.post(reverse("ticket_start", args=[self.order.id]), {"station": "bar"})
        self.tea.refresh_from_db()
        self.plov.refresh_from_db()
        self.assertEqual(self.tea.status, OrderItem.Status.COOKING)
        self.assertEqual(self.plov.status, OrderItem.Status.QUEUED)

    def test_unknown_station_means_all(self):
        html = self.client.get(reverse("kitchen_tickets") + "?station=nope").content.decode()
        self.assertIn("Плов", html)
