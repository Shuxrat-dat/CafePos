from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from inventory.models import Product, RecipeItem
from menu.models import Category, Dish
from pos.models import Order

from . import insights

User = get_user_model()


def paid_order(waiter, dish, qty, when):
    order = Order.objects.create(waiter=waiter, status=Order.Status.PAID, order_type="takeaway")
    order.items.create(dish=dish, quantity=qty, price=dish.price)
    Order.objects.filter(pk=order.pk).update(closed_at=when)
    return order


class InsightsTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="adm", password="x", role="admin")
        self.category = Category.objects.create(name="Еда")
        self.now = timezone.now()
        self.meat = Product.objects.create(name="Мясо", unit="g", cost_per_unit=Decimal("100"))

    def _dish(self, name, price, grams=None):
        dish = Dish.objects.create(category=self.category, name=name, price=price)
        if grams:
            RecipeItem.objects.create(dish=dish, product=self.meat, quantity=grams)
        return dish

    def test_margin_from_recipe_cost(self):
        dish = self._dish("Стейк", 100000, grams=300)  # себестоимость 30 000
        rows = insights.dish_margins([{"obj": dish, "qty": 2, "revenue": Decimal(200000)}])
        self.assertEqual(rows[0]["cost"], Decimal(60000))
        self.assertEqual(rows[0]["profit"], Decimal(140000))
        self.assertEqual(rows[0]["margin"], 70)

    def test_dish_without_recipe_has_no_margin(self):
        dish = self._dish("Чай", 5000)
        rows = insights.dish_margins([{"obj": dish, "qty": 1, "revenue": Decimal(5000)}])
        self.assertIsNone(rows[0]["margin"])

    def test_no_advice_without_a_week_of_history(self):
        dish = self._dish("Плов", 40000, grams=100)
        paid_order(self.admin, dish, 1, self.now - timedelta(days=1))
        self.assertFalse(insights.menu_advice(self.now)["enough"])

    def test_advice_finds_loss_rare_and_profitable_dishes(self):
        thin = self._dish("Дешёвый бургер", 20000, grams=190)      # себестоимость 19 000, наценка 5%
        rare = self._dish("Редкий салат", 30000, grams=50)           # наценка 83%, почти не заказывают
        weak = self._dish("Забытый суп", 25000, grams=150)           # себестоимость 15 000, наценка 40%, не заказывают
        popular = self._dish("Хит", 50000, grams=100)                # наценка 80%, заказывают часто
        for day in range(1, 10):
            when = self.now - timedelta(days=day)
            paid_order(self.admin, popular, 6, when)
            paid_order(self.admin, thin, 3, when)
        paid_order(self.admin, rare, 1, self.now - timedelta(days=2))
        advice = insights.menu_advice(self.now)
        self.assertTrue(advice["enough"])
        self.assertEqual([e["dish"].name for e in advice["loss"]], ["Дешёвый бургер"])
        self.assertIn("Забытый суп", [e["dish"].name for e in advice["remove"]])
        self.assertIn("Редкий салат", [e["dish"].name for e in advice["promote"]])
        self.assertNotIn(weak, [e["dish"] for e in advice["loss"] + advice["promote"]])

    def test_headline_empty_period(self):
        items = insights.headline(
            revenue=0, orders_count=0, avg_check=0, single_day=True, compare_label="", compare_change=None,
            best_label="", best_sum=0, top_dish=None, margin=0, margin_pct_value=0, has_cost=False,
            cancelled_count=0, cancelled_sum=0, low_stock_count=0,
        )
        self.assertEqual(len(items), 1)
        self.assertIn("нет", items[0]["text"])

    def test_headline_marks_worse_day_as_bad(self):
        items = insights.headline(
            revenue=900000, orders_count=10, avg_check=90000, single_day=True,
            compare_label="К тому же дню прошлой недели", compare_change=-12, best_label="20:00", best_sum=400000,
            top_dish={"name": "Плов", "n": 7}, margin=500000, margin_pct_value=55, has_cost=True,
            cancelled_count=0, cancelled_sum=0, low_stock_count=0,
        )
        compare = next(i for i in items if "−12%" in i["text"])
        self.assertEqual(compare["kind"], "bad")
        self.assertTrue(any("Плов" in i["text"] for i in items))

    def test_dashboard_shows_summary_and_week_comparison(self):
        dish = self._dish("Плов", 40000, grams=100)
        today = timezone.localdate()
        paid_order(self.admin, dish, 2, timezone.now())
        paid_order(self.admin, dish, 1, timezone.now() - timedelta(days=7))
        self.client.force_login(self.admin)
        response = self.client.get("/reports/", {"from": today.isoformat(), "to": today.isoformat()})
        self.assertContains(response, "Коротко")
        self.assertContains(response, "К тому же дню прошлой недели")
        self.assertContains(response, "+100%")
        self.assertContains(response, "Что сделать с меню")
