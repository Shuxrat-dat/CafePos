from django.contrib.auth import get_user_model
from django.test import TestCase

from menu.models import Category, Dish

from .models import GuestOrder, Hall, Table

User = get_user_model()


class GuestOrderTests(TestCase):
    def setUp(self):
        hall = Hall.objects.create(name="Основной зал")
        self.table = Table.objects.create(hall=hall, number="3")
        category = Category.objects.create(name="Горячее")
        self.plov = Dish.objects.create(category=category, name="Плов", price=40000)
        self.tea = Dish.objects.create(category=category, name="Чай", price=5000)
        self.gone = Dish.objects.create(category=category, name="Снятое", price=1000, is_available=False)
        self.url = f"/menu/t/{self.table.id}/"

    def _send(self, quantities):
        data = {f"qty_{dish.id}": str(qty) for dish, qty in quantities.items()}
        return self.client.post(self.url, data)

    def test_table_page_opens_without_login(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Плов")
        self.assertContains(response, "стол 3")
        self.assertNotContains(response, "Снятое")

    def test_unknown_table_is_404(self):
        self.assertEqual(self.client.get("/menu/t/9999/").status_code, 404)

    def test_post_creates_pending_request(self):
        response = self._send({self.plov: 2, self.tea: 1})
        self.assertRedirects(response, self.url + "?sent=1")
        guest = GuestOrder.objects.get()
        self.assertEqual(guest.status, GuestOrder.Status.PENDING)
        self.assertEqual(guest.table, self.table)
        self.assertEqual(guest.items.count(), 2)
        self.assertEqual(guest.items.get(dish=self.plov).quantity, 2)

    def test_empty_request_is_rejected(self):
        response = self._send({self.plov: 0})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["error"], "Выберите хотя бы одно блюдо.")
        self.assertFalse(GuestOrder.objects.exists())

    def test_unavailable_dish_is_ignored(self):
        self._send({self.gone: 1, self.plov: 1})
        guest = GuestOrder.objects.get()
        self.assertEqual(guest.items.count(), 1)
        self.assertEqual(guest.items.get().dish, self.plov)

    def test_quantity_is_capped(self):
        self._send({self.plov: 999})
        self.assertEqual(GuestOrder.objects.get().items.get().quantity, 20)

    def test_cooldown_blocks_second_request(self):
        self._send({self.plov: 1})
        response = self._send({self.tea: 1})
        self.assertEqual(response.context["error"], "Подождите минуту перед следующим заказом.")
        self.assertEqual(GuestOrder.objects.count(), 1)

    def test_pending_limit_per_table(self):
        for _ in range(3):
            GuestOrder.objects.create(table=self.table)
        response = self._send({self.plov: 1})
        self.assertEqual(
            response.context["error"], "Заказ этого стола уже ожидает официанта. Подождите, пожалуйста."
        )
        self.assertEqual(GuestOrder.objects.count(), 3)


class TablesQrTests(TestCase):
    def setUp(self):
        hall = Hall.objects.create(name="Основной зал")
        Table.objects.create(hall=hall, number="3")
        self.admin = User.objects.create_user(username="adm_tq", password="pass12345", role="admin")
        self.waiter = User.objects.create_user(username="wai_tq", password="pass12345", role="waiter")

    def test_admin_sees_codes(self):
        self.client.force_login(self.admin)
        response = self.client.get("/menu/qr/tables/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<svg")
        self.assertContains(response, "стол 3")

    def test_waiter_cannot_open(self):
        self.client.force_login(self.waiter)
        self.assertEqual(self.client.get("/menu/qr/tables/").status_code, 403)

    def test_guest_goes_to_login(self):
        response = self.client.get("/menu/qr/tables/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)