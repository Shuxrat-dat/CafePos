"""Схема столов «Stollar» для официанта и кассира: нажал на стол, открылся заказ."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from menu.models import Category, Dish

from .models import Hall, Order, OrderItem, Table

User = get_user_model()


class TableMapTests(TestCase):
    def setUp(self):
        self.waiter = User.objects.create_user(username="aziz", password="pass12345", role="waiter", first_name="Aziz")
        self.other = User.objects.create_user(username="jasur", password="pass12345", role="waiter", first_name="Jasur")
        self.cashier = User.objects.create_user(username="kassir", password="pass12345", role="cashier")
        self.owner = User.objects.create_user(username="egasi", password="pass12345", role="owner")
        self.cook = User.objects.create_user(username="oshpaz", password="pass12345", role="cook")
        hall = Hall.objects.create(name="Zal")
        self.free = Table.objects.create(hall=hall, number="1", seats=6)
        self.mine = Table.objects.create(hall=hall, number="2", seats=4)
        self.theirs = Table.objects.create(hall=hall, number="3", seats=4)
        self.booked = Table.objects.create(hall=hall, number="4", seats=4, reserved_for="Karimov")
        dish = Dish.objects.create(category=Category.objects.create(name="Taom"), name="Plov", price=45000)
        self.my_order = Order.objects.create(table=self.mine, waiter=self.waiter)
        OrderItem.objects.create(order=self.my_order, dish=dish, quantity=1)
        self.their_order = Order.objects.create(table=self.theirs, waiter=self.other)
        OrderItem.objects.create(order=self.their_order, dish=dish, quantity=2)

    def nav(self, user):
        self.client.force_login(user)
        return {item["name"]: item for item in self.client.get(reverse("table_map")).context["nav_items"]}

    def test_waiter_and_cashier_have_stollar_in_the_menu(self):
        for user in (self.waiter, self.cashier):
            self.assertIn("table_map", self.nav(user))
        self.assertEqual(self.nav(self.waiter)["table_map"]["label"], "Столы")

    def test_owner_has_no_duplicate_tables_item(self):
        names = [i["label"] for i in self.nav(self.owner).values()]
        self.assertEqual(names.count("Столы"), 1)
        self.assertNotIn("table_map", self.nav(self.owner))

    def test_cook_cannot_open_it(self):
        self.client.force_login(self.cook)
        self.assertEqual(self.client.get(reverse("table_map")).status_code, 403)

    def test_page_shows_halls_and_tables_with_states(self):
        self.client.force_login(self.waiter)
        page = self.client.get(reverse("table_map"))
        self.assertContains(page, "Нажмите на стол, чтобы открыть заказ.")
        html = page.content.decode()
        for number, state in (("1", "free"), ("2", "busy"), ("3", "locked"), ("4", "reserved")):
            self.assertIn(f'data-table="{number}" data-state="{state}"', html)
        self.assertEqual(page.context["counts"]["free"], 1)

    def test_click_on_free_table_opens_a_new_order(self):
        self.client.force_login(self.waiter)
        response = self.client.post(reverse("open_table", args=[self.free.id]))
        order = Order.objects.get(table=self.free)
        self.assertEqual(order.waiter, self.waiter)
        self.assertRedirects(response, reverse("order_detail", args=[order.id]))

    def test_click_on_own_busy_table_opens_the_existing_order(self):
        self.client.force_login(self.waiter)
        response = self.client.post(reverse("open_table", args=[self.mine.id]))
        self.assertRedirects(response, reverse("order_detail", args=[self.my_order.id]))
        self.assertEqual(Order.objects.filter(table=self.mine).count(), 1)

    def test_waiter_cannot_open_another_waiters_table_but_cashier_can(self):
        self.client.force_login(self.waiter)
        self.assertEqual(self.client.post(reverse("open_table", args=[self.theirs.id])).status_code, 403)
        page = self.client.get(reverse("table_map")).content.decode()
        self.assertNotIn(reverse("open_table", args=[self.theirs.id]), page)
        self.client.force_login(self.cashier)
        response = self.client.post(reverse("open_table", args=[self.theirs.id]))
        self.assertRedirects(response, reverse("order_detail", args=[self.their_order.id]))

    def test_refresh_returns_only_the_grid_without_takeaway_list(self):
        Order.objects.create(waiter=self.waiter, order_type=Order.OrderType.TAKEAWAY)
        self.client.force_login(self.waiter)
        response = self.client.get(reverse("table_map"), HTTP_HX_REQUEST="true")
        html = response.content.decode()
        self.assertIn("data-hall-section", html)
        self.assertNotIn("<html", html)
        self.assertNotIn("Навынос и доставка", html)
