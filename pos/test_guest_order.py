from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase

from core.models import CafeSettings
from menu.models import Category, Dish

from .models import GuestOrder, Hall, Order, OrderItem, Table

User = get_user_model()


class GuestOrderTests(TestCase):
    def setUp(self):
        hall = Hall.objects.create(name="Основной зал")
        self.table = Table.objects.create(hall=hall, number="3")
        category = Category.objects.create(name="Горячее")
        self.plov = Dish.objects.create(category=category, name="Плов", price=40000)
        self.tea = Dish.objects.create(category=category, name="Чай", price=5000)
        self.gone = Dish.objects.create(category=category, name="Снятое", price=1000, is_available=False)
        self.url = self.table.menu_path()
        cache.clear()
        # Здесь проверяется режим «официант подтверждает»; режим «сразу на кухню» — в AutoOrderTests
        CafeSettings.objects.update_or_create(pk=CafeSettings.get().pk, defaults={"guest_auto_send": False})

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
        self.assertRedirects(response, self.url + "&sent=1")
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


class AutoOrderTests(TestCase):
    """Гость отсканировал QR и заказал: заказ сразу в базе и в очереди кухни, без участия официанта."""

    def setUp(self):
        hall = Hall.objects.create(name="Основной зал")
        self.table = Table.objects.create(hall=hall, number="3")
        self.other = Table.objects.create(hall=hall, number="4")
        category = Category.objects.create(name="Горячее")
        self.plov = Dish.objects.create(category=category, name="Плов", price=40000)
        self.tea = Dish.objects.create(category=category, name="Чай", price=5000)
        self.fish = Dish.objects.create(category=category, name="Рыба", price=200000, sold_by_weight=True)
        self.waiter = User.objects.create_user(username="wai_auto", password="pass12345", role="waiter")
        self.url = self.table.menu_path()
        cache.clear()

    def _send(self, quantities, url=None):
        data = {f"qty_{dish.id}": str(qty) for dish, qty in quantities.items()}
        return self.client.post(url or self.url, data)

    def test_order_goes_straight_to_kitchen(self):
        response = self._send({self.plov: 2, self.tea: 1})
        self.assertRedirects(response, self.url + "&sent=2")
        order = Order.objects.get()
        self.assertEqual(order.table, self.table)
        self.assertEqual(order.waiter, self.waiter)
        self.assertEqual(order.items.count(), 2)
        self.assertEqual(set(order.items.values_list("status", flat=True)), {OrderItem.Status.QUEUED})
        self.assertEqual(order.items.get(dish=self.plov).quantity, 2)
        self.assertEqual(GuestOrder.objects.get().status, GuestOrder.Status.ACCEPTED)

    def test_second_order_joins_the_open_order_of_the_table(self):
        self._send({self.plov: 1})
        cache.clear()
        self.client.cookies.clear()
        self._send({self.tea: 1})
        order = Order.objects.get()
        self.assertEqual(order.items.count(), 2)

    def test_existing_waiter_keeps_the_table(self):
        boss = User.objects.create_user(username="wai_boss", password="pass12345", role="waiter")
        existing = Order.objects.create(table=self.table, waiter=boss)
        existing.items.create(dish=self.tea, quantity=1)
        self._send({self.plov: 1})
        self.assertEqual(Order.objects.count(), 1)
        existing.refresh_from_db()
        self.assertEqual(existing.waiter, boss)
        self.assertEqual(existing.items.count(), 2)

    def test_waiters_draft_items_are_not_sent_to_kitchen(self):
        existing = Order.objects.create(table=self.table, waiter=self.waiter)
        draft = existing.items.create(dish=self.tea, quantity=1)
        self._send({self.plov: 1})
        draft.refresh_from_db()
        self.assertEqual(draft.status, OrderItem.Status.NEW)
        self.assertEqual(existing.items.get(dish=self.plov).status, OrderItem.Status.QUEUED)

    def test_weight_dish_is_split_into_portions(self):
        self._send({self.fish: 2})
        items = Order.objects.get().items.all()
        self.assertEqual(items.count(), 2)
        self.assertTrue(all(i.by_weight and i.quantity == 1 for i in items))

    def test_least_busy_waiter_gets_the_new_table(self):
        busy = User.objects.create_user(username="wai_busy", password="pass12345", role="waiter")
        Order.objects.create(table=self.other, waiter=self.waiter)
        Order.objects.create(table=None, waiter=self.waiter)
        self._send({self.plov: 1})
        self.assertEqual(Order.objects.get(table=self.table).waiter, busy)

    def test_without_key_nothing_is_ordered(self):
        response = self._send({self.plov: 1}, url=f"/menu/t/{self.table.id}/")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Order.objects.exists())

    def test_waiter_confirmation_mode_still_works(self):
        CafeSettings.objects.filter(pk=CafeSettings.get().pk).update(guest_auto_send=False)
        response = self._send({self.plov: 1})
        self.assertRedirects(response, self.url + "&sent=1")
        self.assertFalse(Order.objects.exists())
        self.assertEqual(GuestOrder.objects.get().status, GuestOrder.Status.PENDING)

    def test_guest_sees_progress_of_own_dishes(self):
        self._send({self.plov: 1})
        item = OrderItem.objects.get()
        OrderItem.objects.filter(pk=item.pk).update(status=OrderItem.Status.COOKING)
        response = self.client.get(self.url + "&status=1")
        self.assertContains(response, "Плов")
        self.assertContains(response, "Готовится")
        self.assertEqual(response.context["my_items"][0].guest_state, "cooking")

    def test_other_guest_does_not_see_these_dishes(self):
        self._send({self.plov: 1})
        self.client.cookies.clear()
        response = self.client.get(self.url + "&status=1")
        self.assertEqual(response.context["my_items"], [])

    def test_status_needs_the_table_key(self):
        self.assertEqual(self.client.get(f"/menu/t/{self.table.id}/?status=1").status_code, 404)

    def test_no_staff_falls_back_to_waiter_confirmation(self):
        User.objects.all().delete()
        response = self._send({self.plov: 1})
        self.assertRedirects(response, self.url + "&sent=1")
        self.assertEqual(GuestOrder.objects.get().status, GuestOrder.Status.PENDING)


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