"""Блюда на вес: цена за 1 кг, вес вводится в граммах, сумма и списание считаются по весу."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.templatetags.ui import weight
from inventory.models import Product, RecipeItem
from menu.models import Category, Dish

from . import services
from .models import GuestOrder, GuestOrderItem, Hall, Order, OrderItem, Table

User = get_user_model()


class WeightDishTests(TestCase):
    def setUp(self):
        self.cashier = User.objects.create_user(username="kassir", password="pass12345", role="cashier")
        self.table = Table.objects.create(hall=Hall.objects.create(name="Зал"), number="1")
        category = Category.objects.create(name="Рыба")
        self.fish = Dish.objects.create(category=category, name="Сазан", price=95000, sold_by_weight=True)
        self.tea = Dish.objects.create(category=category, name="Чай", price=5000)
        self.order = Order.objects.create(table=self.table, waiter=self.cashier)
        self.client.force_login(self.cashier)

    def add_fish(self, raw):
        return self.client.post(reverse("add_item", args=[self.order.id, self.fish.id]), {"weight": raw})

    def test_price_is_per_kilogram(self):
        self.add_fish("1030")
        item = self.order.items.get()
        self.assertTrue(item.by_weight)
        self.assertEqual(item.weight_grams, 1030)
        self.assertEqual(item.line_total, Decimal("97850"))
        self.assertEqual(self.order.totals["subtotal"], Decimal("97850"))

    def test_less_than_a_kilogram(self):
        self.add_fish("450")
        self.assertEqual(self.order.items.get().line_total, Decimal("42750"))

    def test_kilograms_with_comma(self):
        self.add_fish("1,03")
        self.assertEqual(self.order.items.get().weight_grams, 1030)

    def test_wrong_weight_keeps_form_open_with_error(self):
        for raw in ("", "abc", "5", "60000"):
            response = self.add_fish(raw)
            self.assertEqual(response["HX-Retarget"], "#modal")
            self.assertContains(response, "Вес")
        self.assertFalse(self.order.items.exists())

    def test_each_weighing_is_a_separate_line(self):
        self.add_fish("1030")
        self.add_fish("800")
        self.assertEqual(sorted(self.order.items.values_list("weight_grams", flat=True)), [800, 1030])

    def test_plus_button_does_not_double_weight(self):
        self.add_fish("500")
        item = self.order.items.get()
        self.client.post(reverse("change_qty", args=[self.order.id, item.id]), {"delta": "1"})
        item.refresh_from_db()
        self.assertEqual((item.quantity, item.weight_grams), (1, 500))

    def test_edit_weight(self):
        self.add_fish("500")
        item = self.order.items.get()
        self.client.post(reverse("edit_item", args=[self.order.id, item.id]), {"weight": "1200", "comment": ""})
        item.refresh_from_db()
        self.assertEqual(item.weight_grams, 1200)
        self.assertEqual(item.line_total, Decimal("114000"))

    def test_price_change_does_not_touch_old_order(self):
        self.add_fish("1000")
        self.fish.price = 120000
        self.fish.sold_by_weight = False
        self.fish.save()
        item = self.order.items.get()
        self.assertEqual(item.line_total, Decimal("95000"))

    def test_payment_writes_off_stock_by_weight(self):
        sazan = Product.objects.create(name="Сазан свежий", unit="g", stock=5000, cost_per_unit=60)
        RecipeItem.objects.create(dish=self.fish, product=sazan, quantity=1000)  # расход на 1 кг
        self.add_fish("450")
        self.order.items.update(status=OrderItem.Status.SERVED)
        response = self.client.post(reverse("pay", args=[self.order.id]), {"method": "card"})
        self.assertRedirects(response, reverse("receipt", args=[self.order.id]))
        sazan.refresh_from_db()
        self.assertEqual(sazan.stock, Decimal("4550"))
        self.order.refresh_from_db()
        self.assertEqual(self.order.total, Decimal("42750"))

    def test_receipt_shows_weight_and_price_per_kg(self):
        self.add_fish("1030")
        self.order.items.update(status=OrderItem.Status.SERVED)
        self.client.post(reverse("pay", args=[self.order.id]), {"method": "card"})
        receipt = self.client.get(reverse("receipt", args=[self.order.id]))
        self.assertContains(receipt, "1,03 кг")
        self.assertContains(receipt, "97")

    def test_guest_order_needs_weight_before_payment(self):
        guest = GuestOrder.objects.create(table=self.table)
        GuestOrderItem.objects.create(guest_order=guest, dish=self.fish, quantity=2)
        GuestOrderItem.objects.create(guest_order=guest, dish=self.tea, quantity=1)
        self.client.post(reverse("accept_guest", args=[self.order.id, guest.id]))

        fish_items = list(self.order.items.filter(dish=self.fish))
        self.assertEqual(len(fish_items), 2)
        self.assertTrue(all(i.needs_weight for i in fish_items))
        self.assertContains(self.client.get(reverse("order_panel", args=[self.order.id])), "Укажите вес")

        self.order.items.update(status=OrderItem.Status.SERVED)
        response = self.client.post(reverse("pay", args=[self.order.id]), {"method": "card"})
        self.assertRedirects(response, reverse("order_detail", args=[self.order.id]))
        with self.assertRaises(services.PaymentError):
            services.pay_order(self.order, self.cashier, "card", None, None)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.OPEN)

        # Вес указывают, когда блюдо уже на кухне и даже подано
        for item, grams in zip(fish_items, ("700", "1100")):
            self.client.post(reverse("edit_item", args=[self.order.id, item.id]), {"weight": grams, "comment": ""})
        self.order.items.update(status=OrderItem.Status.SERVED)
        self.client.post(reverse("pay", args=[self.order.id]), {"method": "card"})
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)
        self.assertEqual(self.order.total, Decimal("66500") + Decimal("104500") + Decimal("5000"))

    def test_floor_quick_add_opens_weight_form(self):
        response = self.client.post(reverse("quick_add", args=[self.fish.id]), {"order_id": self.order.id})
        self.assertRedirects(response, f"{reverse('order_detail', args=[self.order.id])}?weigh={self.fish.id}",
                             fetch_redirect_response=False)
        self.assertFalse(self.order.items.exists())


class WeightFilterTests(TestCase):
    def test_format(self):
        self.assertEqual(weight(450), "450 г")
        self.assertEqual(weight(1030), "1,03 кг")
        self.assertEqual(weight(1000), "1 кг")
        self.assertEqual(weight(None), "")
