from datetime import datetime, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from menu.models import Category, Dish

from .forecast import MIN_DAYS_OF_DATA, build_forecast, shopping_list
from .models import Product, RecipeItem, StockMovement

User = get_user_model()


class ForecastTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.rice = Product.objects.create(name="Рис", unit="g", min_stock=1000, cost_per_unit=20)
        self.oil = Product.objects.create(name="Масло", unit="ml", min_stock=500, cost_per_unit=15)
        StockMovement.objects.create(product=self.rice, kind="income", quantity=10000)
        StockMovement.objects.create(product=self.oil, kind="income", quantity=20000)

    def _sell(self, product, amount, days_ago):
        movement = StockMovement.objects.create(product=product, kind="sale", quantity=amount)
        StockMovement.objects.filter(pk=movement.pk).update(created_at=self.now - timedelta(days=days_ago, hours=1))

    def test_not_enough_history_gives_no_forecast(self):
        self._sell(self.rice, 500, 1)
        info = build_forecast(self.now)[self.rice.id]
        self.assertTrue(info["nodata"])
        self.assertIsNone(info["days_left"])

    def test_days_left_from_daily_average(self):
        for day in range(1, MIN_DAYS_OF_DATA + 3):
            self._sell(self.rice, 1000, day)
        rice = build_forecast(self.now)[self.rice.id]
        self.assertAlmostEqual(rice["usage"], 1000, delta=1)
        # на складе 10000 - 5000 проданных = 5000, расход 1000 в день
        self.assertAlmostEqual(rice["days_left"], 5, delta=0.1)

    def test_levels_and_suggestion(self):
        for day in range(1, 8):
            self._sell(self.rice, 2000, day)
            self._sell(self.oil, 10, day)
        info = build_forecast(self.now)
        # Рис: 10000 - 14000 < 0, закончился
        self.assertEqual(info[self.rice.id]["level"], "out")
        self.assertGreater(info[self.rice.id]["suggest"], 0)
        # Масло расходуется медленно: запаса хватает
        self.assertEqual(info[self.oil.id]["level"], "ok")
        self.assertEqual(info[self.oil.id]["suggest"], 0)

    def test_shopping_list_contains_only_products_to_buy(self):
        for day in range(1, 8):
            self._sell(self.rice, 1200, day)
        rows = shopping_list(self.now)
        self.assertEqual([r["product"].name for r in rows], ["Рис"])

    def test_weekend_boost_detected(self):
        today = timezone.localdate(self.now)
        for offset in range(1, 15):
            day = today - timedelta(days=offset)
            amount = 3000 if day.weekday() in (4, 5, 6) else 1000
            movement = StockMovement.objects.create(product=self.rice, kind="sale", quantity=amount)
            stamp = timezone.make_aware(datetime(day.year, day.month, day.day, 14, 0))
            StockMovement.objects.filter(pk=movement.pk).update(created_at=stamp)
        boost = build_forecast(self.now)[self.rice.id]["weekend_boost"]
        self.assertIsNotNone(boost)
        self.assertGreaterEqual(boost, 100)


class ShoppingViewTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="own", password="x", role="owner")
        self.waiter = User.objects.create_user(username="wt", password="x", role="waiter")
        self.product = Product.objects.create(name="Мука", unit="g", min_stock=1000, cost_per_unit=10)
        category = Category.objects.create(name="Выпечка")
        self.dish = Dish.objects.create(category=category, name="Лепёшка", price=5000)
        RecipeItem.objects.create(dish=self.dish, product=self.product, quantity=200)

    def test_owner_sees_list_and_blocked_dishes(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("stock_shopping"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Мука")
        self.assertContains(response, "Лепёшка")
        self.assertContains(response, "t.me/share/url")

    def test_waiter_has_no_access(self):
        self.client.force_login(self.waiter)
        self.assertEqual(self.client.get(reverse("stock_shopping")).status_code, 403)

    def test_empty_state_when_everything_is_in_stock(self):
        StockMovement.objects.create(product=self.product, kind="income", quantity=50000)
        self.client.force_login(self.owner)
        response = self.client.get(reverse("stock_shopping"))
        self.assertContains(response, "Закупать пока нечего")

    def test_stock_screen_shows_days_column(self):
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse("stock")), "Хватит на")
