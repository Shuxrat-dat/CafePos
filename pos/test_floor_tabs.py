from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from menu.models import Category, Dish

from . import services
from .models import Hall, Order, Table

User = get_user_model()


class FloorTabsTests(TestCase):
    """«Заказы»: три раздела — в зале, навынос, доставка. В каждом только свои заказы."""

    def setUp(self):
        self.cashier = User.objects.create_user(username="kass_tabs", password="x", role="cashier")
        self.client.force_login(self.cashier)
        dish = Dish.objects.create(category=Category.objects.create(name="Еда"), name="Плов", price=40000)
        table = Table.objects.create(hall=Hall.objects.create(name="Зал"), number="5", seats=4)
        self.dine = services.create_order(self.cashier, Order.OrderType.DINE_IN, table=table)
        self.take = services.create_order(self.cashier, Order.OrderType.TAKEAWAY)
        self.deliv = services.create_order(self.cashier, Order.OrderType.DELIVERY)
        for order in (self.dine, self.take, self.deliv):
            order.items.create(dish=dish, quantity=1, price=dish.price)

    def _ids(self, tab=None, **extra):
        params = {"tab": tab} if tab else {}
        params.update(extra)
        response = self.client.get(reverse("floor"), params)
        return [o.id for o in response.context["current_orders"]], response

    def test_each_section_shows_only_its_own_orders(self):
        self.assertEqual(self._ids("dine_in")[0], [self.dine.id])
        self.assertEqual(self._ids("takeaway")[0], [self.take.id])
        self.assertEqual(self._ids("delivery")[0], [self.deliv.id])

    def test_default_section_is_in_hall(self):
        self.assertEqual(self._ids()[0], [self.dine.id])
        self.assertEqual(self._ids("garbage")[0], [self.dine.id])

    def test_tabs_show_counts_of_every_section(self):
        _ids, response = self._ids("takeaway")
        self.assertEqual({t["key"]: t["count"] for t in response.context["floor_tabs"]},
                         {"dine_in": 1, "takeaway": 1, "delivery": 1})
        self.assertEqual(response.context["active_tab"], "takeaway")
        self.assertContains(response, "?tab=delivery")

    def test_opening_an_order_switches_to_its_section(self):
        ids, response = self._ids(order_id=self.deliv.id)
        self.assertEqual(ids, [self.deliv.id])
        self.assertEqual(response.context["selected_order"].id, self.deliv.id)

    def test_order_from_another_section_is_not_selected_in_this_tab(self):
        _ids, response = self._ids("takeaway", order_id=self.dine.id)
        self.assertEqual(response.context["selected_order"].id, self.take.id)

    def test_empty_section_says_so(self):
        Order.objects.filter(id=self.deliv.id).delete()
        _ids, response = self._ids("delivery")
        self.assertEqual(_ids, [])
        self.assertContains(response, "В этом разделе заказов пока нет")

    def test_back_from_order_returns_to_its_section(self):
        page = self.client.get(reverse("order_detail", args=[self.take.id]))
        self.assertContains(page, f'{reverse("floor")}?tab=takeaway')

    def test_stage_button_keeps_the_section(self):
        response = self.client.post(reverse("toggle_order_stage", args=[self.deliv.id]))
        self.assertEqual(response["Location"], f'{reverse("floor")}?tab=delivery')
