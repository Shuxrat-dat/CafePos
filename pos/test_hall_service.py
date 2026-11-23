from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import CafeSettings
from pos import services
from pos.models import Hall, Order, Table

User = get_user_model()


class HallServiceRateTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="owner_hs", password="pass12345", role="owner")
        self.waiter = User.objects.create_user(username="waiter_hs", password="pass12345", role="waiter")
        cafe = CafeSettings.get()
        cafe.service_percent = Decimal("12")
        cafe.save()
        self.garden = Hall.objects.create(name="TASHQARI", service_percent=Decimal("5"))
        self.roof = Hall.objects.create(name="TEPA", service_percent=Decimal("10"))
        self.plain = Hall.objects.create(name="ZAL")  # ставка не задана -> общая
        self.free = Hall.objects.create(name="VIP", service_percent=Decimal("0"))
        self.t_garden = Table.objects.create(hall=self.garden, number="1")
        self.t_roof = Table.objects.create(hall=self.roof, number="1")
        self.t_plain = Table.objects.create(hall=self.plain, number="1")
        self.t_free = Table.objects.create(hall=self.free, number="1")

    def dine_in(self, table):
        return services.create_order(self.waiter, Order.OrderType.DINE_IN, table)

    def test_order_takes_the_rate_of_the_tables_hall(self):
        self.assertEqual(self.dine_in(self.t_garden).service_percent, Decimal("5"))
        self.assertEqual(self.dine_in(self.t_roof).service_percent, Decimal("10"))

    def test_hall_without_rate_uses_the_general_one(self):
        self.assertEqual(self.dine_in(self.t_plain).service_percent, Decimal("12"))

    def test_zero_in_hall_means_no_service_charge(self):
        self.assertEqual(self.dine_in(self.t_free).service_percent, Decimal("0"))

    def test_takeaway_and_delivery_have_no_service(self):
        for kind in (Order.OrderType.TAKEAWAY, Order.OrderType.DELIVERY):
            self.assertEqual(services.create_order(self.waiter, kind).service_percent, Decimal("0"))

    def test_moving_order_to_another_hall_changes_the_rate(self):
        order = self.dine_in(self.t_garden)
        services.set_order_type(order, Order.OrderType.DINE_IN, self.t_roof)
        order.refresh_from_db()
        self.assertEqual(order.service_percent, Decimal("10"))

    def test_changing_hall_rate_keeps_existing_orders(self):
        order = self.dine_in(self.t_garden)
        self.garden.service_percent = Decimal("20")
        self.garden.save()
        order.refresh_from_db()
        self.assertEqual(order.service_percent, Decimal("5"))
        self.assertEqual(self.dine_in(Table.objects.create(hall=self.garden, number="2")).service_percent, Decimal("20"))

    def test_owner_edits_hall_rate_and_can_clear_it(self):
        self.client.force_login(self.owner)
        url = reverse("hall_edit", args=[self.plain.id])
        self.assertEqual(self.client.get(url).status_code, 200)
        self.client.post(url, {"name": "ZAL", "name_uz": "", "sort_order": 0, "service_percent": "7.5"})
        self.plain.refresh_from_db()
        self.assertEqual(self.plain.service_percent, Decimal("7.50"))
        self.client.post(url, {"name": "ZAL", "name_uz": "", "sort_order": 0, "service_percent": ""})
        self.plain.refresh_from_db()
        self.assertIsNone(self.plain.service_percent)

    def test_rate_out_of_range_is_rejected(self):
        self.client.force_login(self.owner)
        response = self.client.post(reverse("hall_edit", args=[self.plain.id]),
                                    {"name": "ZAL", "name_uz": "", "sort_order": 0, "service_percent": "50"})
        self.assertEqual(response.status_code, 200)
        self.plain.refresh_from_db()
        self.assertIsNone(self.plain.service_percent)

    def test_waiter_cannot_edit_hall(self):
        self.client.force_login(self.waiter)
        self.assertEqual(self.client.get(reverse("hall_edit", args=[self.plain.id])).status_code, 403)

    def test_tables_page_shows_each_halls_rate(self):
        self.client.force_login(self.owner)
        html = self.client.get(reverse("manage_tables")).content.decode()
        self.assertIn("5%", html)
        self.assertIn("10%", html)
