from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.templatetags.ui import item_qty, per_kg, weight
from inventory.models import Product, RecipeItem
from menu.models import Category, Dish
from pos import services
from pos.models import Order, OrderItem

User = get_user_model()


class UnitTests(TestCase):
    """Блюда на вес и в розлив: ввод суммой, литры, панель с цифрами, вес от повара, сигареты поштучно."""

    def setUp(self):
        self.waiter = User.objects.create_user(username="w_units", password="pass12345", role="waiter")
        self.cook = User.objects.create_user(username="k_units", password="pass12345", role="cook")
        self.admin = User.objects.create_user(username="a_units", password="pass12345", role="admin")
        category = Category.objects.create(name="Разное")
        self.fish = Dish.objects.create(category=category, name="Сазан", price=Decimal("95000"), sold_by_weight=True)
        self.beer = Dish.objects.create(category=category, name="Пиво разливное", price=Decimal("30000"),
                                        sold_by_weight=True, measure="l")
        self.order = services.create_order(self.waiter, Order.OrderType.TAKEAWAY)
        self.client.force_login(self.waiter)

    def add(self, dish, **data):
        return self.client.post(reverse("add_item", args=[self.order.id, dish.id]), data)

    # ---------- ввод на сумму ----------

    def test_sum_mode_turns_money_into_grams(self):
        self.add(self.fish, weight="50000", weight_mode="sum")
        item = OrderItem.objects.get(dish=self.fish)
        self.assertEqual(item.weight_grams, 526)  # 50 000 / 95 000 за кг
        self.assertEqual(item.line_total, Decimal("49970"))

    def test_sum_mode_rejects_bad_amount(self):
        response = self.add(self.fish, weight="abc", weight_mode="sum")
        self.assertFalse(OrderItem.objects.exists())
        self.assertContains(response, "50000")

    def test_weight_mode_still_works(self):
        self.add(self.fish, weight="1,03", weight_mode="weight")
        self.assertEqual(OrderItem.objects.get().weight_grams, 1030)

    # ---------- литры ----------

    def test_draft_beer_in_milliliters(self):
        self.add(self.beer, weight="500")
        item = OrderItem.objects.get(dish=self.beer)
        self.assertEqual((item.measure, item.weight_grams, item.line_total), ("l", 500, Decimal("15000")))
        self.assertEqual(item_qty(item), "500 мл")
        self.assertEqual(per_kg(item), " / л")
        self.assertEqual(weight(1500, "l"), "1,5 л")

    def test_beer_on_sum(self):
        self.add(self.beer, weight="45000", weight_mode="sum")
        self.assertEqual(OrderItem.objects.get().weight_grams, 1500)

    def test_liter_limits_message(self):
        response = self.add(self.beer, weight="5")
        self.assertContains(response, "10 мл")
        self.assertFalse(OrderItem.objects.exists())

    def test_old_weight_items_stay_in_kilograms(self):
        self.add(self.fish, weight="450")
        item = OrderItem.objects.get()
        self.assertEqual(item.measure, "kg")
        self.assertEqual(item_qty(item), "450 г")

    # ---------- панель с цифрами ----------

    def test_weight_form_has_numpad_and_no_system_keyboard(self):
        html = self.client.get(reverse("item_form", args=[self.order.id, self.beer.id])).content.decode()
        self.assertIn("data-numpad", html)
        self.assertIn('inputmode="none"', html)
        self.assertIn('value="sum"', html)
        self.assertIn("мл", html)

    # ---------- повар вводит вес ----------

    def kitchen_item(self):
        return OrderItem.objects.create(order=self.order, dish=self.fish, quantity=1, status=OrderItem.Status.QUEUED)

    def test_cook_sets_weight_with_numpad(self):
        item = self.kitchen_item()
        self.client.force_login(self.cook)
        form = self.client.get(reverse("kitchen_item_weight", args=[item.id])).content.decode()
        self.assertIn("data-numpad", form)
        response = self.client.post(reverse("kitchen_item_weight", args=[item.id]), {"weight": "1250"})
        self.assertEqual(response.status_code, 200)
        item.refresh_from_db()
        self.assertEqual(item.weight_grams, 1250)

    def test_cook_bad_weight_reopens_the_window(self):
        item = self.kitchen_item()
        self.client.force_login(self.cook)
        response = self.client.post(reverse("kitchen_item_weight", args=[item.id]), {"weight": "1"})
        self.assertEqual(response["HX-Retarget"], "#modal")
        item.refresh_from_db()
        self.assertIsNone(item.weight_grams)

    def test_kitchen_ticket_offers_weight_button(self):
        self.kitchen_item()
        self.client.force_login(self.cook)
        html = self.client.get(reverse("kitchen_tickets")).content.decode()
        self.assertIn("kitchen/item/", html)
        self.assertIn("Укажите вес", html)

    def test_waiter_cannot_use_kitchen_weight(self):
        item = self.kitchen_item()
        response = self.client.post(reverse("kitchen_item_weight", args=[item.id]), {"weight": "500"})
        self.assertEqual(response.status_code, 403)

    def test_paid_order_weight_is_locked(self):
        item = self.kitchen_item()
        Order.objects.filter(pk=self.order.pk).update(status=Order.Status.PAID)
        self.client.force_login(self.cook)
        self.assertEqual(self.client.get(reverse("kitchen_item_weight", args=[item.id])).status_code, 404)

    # ---------- сигареты поштучно ----------

    def test_income_in_packs_counts_pieces(self):
        pack = Product.objects.create(name="Marlboro", unit="pcs", pack_size=20)
        self.client.force_login(self.admin)
        self.client.post(reverse("stock"), {"product": pack.id, "quantity": "2", "cost": "30000", "in_packs": "1"})
        pack.refresh_from_db()
        self.assertEqual(pack.stock, Decimal("40"))
        self.assertEqual(pack.cost_per_unit, Decimal("1500.00"))

    def test_income_without_packs_box_is_pieces(self):
        pack = Product.objects.create(name="Marlboro", unit="pcs", pack_size=20)
        self.client.force_login(self.admin)
        self.client.post(reverse("stock"), {"product": pack.id, "quantity": "7"})
        pack.refresh_from_db()
        self.assertEqual(pack.stock, Decimal("7"))

    def test_single_cigarette_and_pack_write_off_pieces(self):
        stock = Product.objects.create(name="Marlboro", unit="pcs", pack_size=20, stock=Decimal("40"))
        category = Category.objects.create(name="Сигареты")
        one = Dish.objects.create(category=category, name="Marlboro, 1 шт", price=Decimal("2000"))
        whole = Dish.objects.create(category=category, name="Marlboro, пачка", price=Decimal("35000"))
        RecipeItem.objects.create(dish=one, product=stock, quantity=1)
        RecipeItem.objects.create(dish=whole, product=stock, quantity=20)
        order = services.create_order(self.waiter, Order.OrderType.TAKEAWAY)
        OrderItem.objects.create(order=order, dish=one, quantity=3, status=OrderItem.Status.SERVED)
        OrderItem.objects.create(order=order, dish=whole, quantity=1, status=OrderItem.Status.SERVED)
        services.open_shift(self.admin, initial_cash=Decimal("0"))
        services.pay_order(order, self.admin, "card", None, None)
        stock.refresh_from_db()
        self.assertEqual(stock.stock, Decimal("17"))  # 40 − 3 − 20
