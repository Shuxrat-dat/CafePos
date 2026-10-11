from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from menu.models import Category, Dish

from .models import GuestOrder, GuestOrderItem, Hall, Order, OrderItem, Table

User = get_user_model()


class GuestAcceptTests(TestCase):
    def setUp(self):
        self.waiter1 = User.objects.create_user(username="w1_g", password="pass12345", role="waiter")
        self.waiter2 = User.objects.create_user(username="w2_g", password="pass12345", role="waiter")
        self.cook = User.objects.create_user(username="k_g", password="pass12345", role="cook")
        hall = Hall.objects.create(name="Основной зал")
        self.table = Table.objects.create(hall=hall, number="3")
        self.other_table = Table.objects.create(hall=hall, number="4")
        category = Category.objects.create(name="Горячее")
        self.plov = Dish.objects.create(category=category, name="Плов", price=40000)
        self.tea = Dish.objects.create(category=category, name="Чай", price=5000)
        self.gone = Dish.objects.create(category=category, name="Снятое", price=1000, is_available=False)
        self.order = Order.objects.create(table=self.table, waiter=self.waiter1)
        self.guest = GuestOrder.objects.create(table=self.table)
        GuestOrderItem.objects.create(guest_order=self.guest, dish=self.plov, quantity=2)
        GuestOrderItem.objects.create(guest_order=self.guest, dish=self.tea, quantity=1)

    def _accept(self, guest=None, order=None):
        guest = guest or self.guest
        order = order or self.order
        return self.client.post(reverse("accept_guest", args=[order.id, guest.id]))

    def test_accept_adds_new_items(self):
        self.client.force_login(self.waiter1)
        response = self._accept()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.order.items.count(), 2)
        item = self.order.items.get(dish=self.plov)
        self.assertEqual(item.quantity, 2)
        self.assertEqual(item.status, OrderItem.Status.NEW)
        self.guest.refresh_from_db()
        self.assertEqual(self.guest.status, GuestOrder.Status.ACCEPTED)

    def test_accept_merges_with_same_new_item(self):
        OrderItem.objects.create(order=self.order, dish=self.plov, quantity=1)
        self.client.force_login(self.waiter1)
        self._accept()
        self.assertEqual(self.order.items.filter(dish=self.plov).count(), 1)
        self.assertEqual(self.order.items.get(dish=self.plov).quantity, 3)

    def test_unavailable_dish_is_skipped(self):
        GuestOrderItem.objects.create(guest_order=self.guest, dish=self.gone, quantity=1)
        self.client.force_login(self.waiter1)
        self._accept()
        self.assertFalse(self.order.items.filter(dish=self.gone).exists())
        self.assertEqual(self.order.items.count(), 2)

    def test_double_accept_does_not_duplicate(self):
        self.client.force_login(self.waiter1)
        self._accept()
        response = self._accept()
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.order.items.get(dish=self.plov).quantity, 2)

    def test_reject_adds_nothing(self):
        self.client.force_login(self.waiter1)
        response = self.client.post(reverse("reject_guest", args=[self.order.id, self.guest.id]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.order.items.count(), 0)
        self.guest.refresh_from_db()
        self.assertEqual(self.guest.status, GuestOrder.Status.REJECTED)

    def test_other_waiter_cannot_accept(self):
        self.client.force_login(self.waiter2)
        self.assertEqual(self._accept().status_code, 403)
        self.guest.refresh_from_db()
        self.assertEqual(self.guest.status, GuestOrder.Status.PENDING)

    def test_guest_of_other_table_is_404(self):
        foreign = GuestOrder.objects.create(table=self.other_table)
        self.client.force_login(self.waiter1)
        self.assertEqual(self._accept(guest=foreign).status_code, 404)

    def test_cook_cannot_accept(self):
        self.client.force_login(self.cook)
        self.assertEqual(self._accept().status_code, 403)

    def test_panel_shows_guest_block(self):
        self.client.force_login(self.waiter1)
        response = self.client.get(reverse("order_panel", args=[self.order.id]))
        self.assertContains(response, "Заявка гостя")
        self.assertContains(response, "Плов")
        self._accept()
        response = self.client.get(reverse("order_panel", args=[self.order.id]))
        self.assertNotContains(response, "Заявка гостя")

    def test_floor_marks_table_with_request(self):
        self.client.force_login(self.waiter1)
        self.assertContains(self.client.get(reverse("floor")), "Заявка гостя")
        self._accept()
        self.assertNotContains(self.client.get(reverse("floor")), "Заявка гостя")