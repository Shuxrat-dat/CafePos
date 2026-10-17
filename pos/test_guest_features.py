from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import CafeSettings
from menu.models import Category, Dish

from . import loyalty, reviews, services
from .models import Customer, Hall, LoyaltyEntry, Order, OrderItem, Review, Table

User = get_user_model()


class GuestFeaturesBase(TestCase):
    def setUp(self):
        self.cashier = User.objects.create_user(username="cash_g", password="x", role="cashier")
        self.owner = User.objects.create_user(username="own_g", password="x", role="owner")
        hall = Hall.objects.create(name="Зал")
        self.table = Table.objects.create(hall=hall, number="1")
        category = Category.objects.create(name="Еда")
        self.dish = Dish.objects.create(
            category=category, name="Плов", price=Decimal("50000"), allergens="gluten,spicy", prep_minutes=15
        )

    def paid_order(self, phone="+998 90 123-45-67", name="Рустам"):
        order = services.create_order(self.cashier, Order.OrderType.DINE_IN, self.table)
        order.customer_phone, order.customer_name = phone, name
        order.save(update_fields=["customer_phone", "customer_name"])
        OrderItem.objects.create(order=order, dish=self.dish, quantity=2, status=OrderItem.Status.SERVED)
        return services.pay_order(order, self.cashier, Order.PaymentMethod.CASH, Decimal("100000"), None)


class AllergenTests(GuestFeaturesBase):
    def test_guest_menu_shows_marks_and_cooking_time(self):
        response = self.client.get(reverse("public_menu"))
        self.assertContains(response, "Глютен")
        self.assertContains(response, "Острое")
        self.assertContains(response, "15")

    def test_unknown_codes_are_ignored(self):
        self.dish.allergens = "gluten,hacker,<script>"
        self.assertEqual(self.dish.allergen_codes, ["gluten"])

    def test_manager_can_save_marks(self):
        self.client.force_login(self.owner)
        response = self.client.post(reverse("dish_edit", args=[self.dish.id]), {
            "name": "Плов", "category": self.dish.category_id, "price": "50000", "sort_order": "0",
            "is_available": "on", "allergens": ["milk", "nuts"],
            "recipe-TOTAL_FORMS": "0", "recipe-INITIAL_FORMS": "0", "recipe-MIN_NUM_FORMS": "0",
            "recipe-MAX_NUM_FORMS": "1000",
        })
        self.assertEqual(response.status_code, 302)
        self.dish.refresh_from_db()
        self.assertEqual(self.dish.allergen_codes, ["milk", "nuts"])


@override_settings(TELEGRAM_SYNC=True)
class ReviewTests(GuestFeaturesBase):
    def test_valid_link_shows_form_and_saves_review_once(self):
        order = self.paid_order()
        url = reverse("order_review", args=[reviews.make_token(order)])
        self.assertContains(self.client.get(url), "Как вам у нас?")
        self.client.post(url, {"rating": "5", "comment": "Вкусно"})
        self.assertEqual(Review.objects.get().rating, 5)
        self.client.post(url, {"rating": "1", "comment": "второй раз"})
        self.assertEqual(Review.objects.count(), 1)
        self.assertContains(self.client.get(url), "Спасибо за оценку")

    def test_tampered_or_unknown_link_is_rejected(self):
        order = self.paid_order()
        token = reviews.make_token(order)
        self.assertEqual(self.client.get(reverse("order_review", args=[token + "x"])).status_code, 404)
        self.assertEqual(self.client.get(reverse("order_review", args=["abc"])).status_code, 404)

    def test_open_order_cannot_be_reviewed(self):
        order = services.create_order(self.cashier, Order.OrderType.TAKEAWAY)
        url = reverse("order_review", args=[reviews.make_token(order)])
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_bad_rating_is_refused(self):
        order = self.paid_order()
        url = reverse("order_review", args=[reviews.make_token(order)])
        for bad in ("0", "6", "abc", ""):
            response = self.client.post(url, {"rating": bad})
            self.assertContains(response, "Выберите оценку")
        self.assertEqual(Review.objects.count(), 0)

    def test_low_rating_reaches_telegram_but_high_does_not(self):
        cafe = CafeSettings.get()
        cafe.telegram_bot_token, cafe.telegram_chat_id = "123456789:" + "A" * 35, "555111"
        cafe.save()
        low, high = self.paid_order(), self.paid_order(phone="")
        with mock.patch("core.telegram._post", return_value=(True, "")) as post:
            self.client.post(reverse("order_review", args=[reviews.make_token(low)]), {"rating": "2", "comment": "Долго"})
            self.client.post(reverse("order_review", args=[reviews.make_token(high)]), {"rating": "5"})
        self.assertEqual(post.call_count, 1)
        self.assertIn("Долго", post.call_args.args[2])

    def test_receipt_contains_review_qr(self):
        order = self.paid_order()
        self.client.force_login(self.cashier)
        response = self.client.get(reverse("receipt", args=[order.id]))
        self.assertContains(response, "<svg")
        self.assertContains(response, "Оцените визит")

    def test_owner_sees_reviews_in_reports(self):
        order = self.paid_order()
        Review.objects.create(order=order, rating=2, comment="Холодный суп")
        self.client.force_login(self.owner)
        response = self.client.get("/reports/")
        self.assertContains(response, "Холодный суп")
        self.assertContains(response, "Отзывы гостей")


class LoyaltyTests(GuestFeaturesBase):
    def setUp(self):
        super().setUp()
        cafe = CafeSettings.get()
        cafe.loyalty_percent = 5
        cafe.save()

    def test_phone_is_normalized_to_last_nine_digits(self):
        self.assertEqual(loyalty.normalize_phone("+998 (90) 123-45-67"), "901234567")
        self.assertEqual(loyalty.normalize_phone("90 123 45 67"), "901234567")
        self.assertEqual(loyalty.normalize_phone("123"), "")

    def test_points_are_earned_once_and_repeat_visits_accumulate(self):
        self.paid_order()                        # 100 000 сум → 5 000 баллов
        self.paid_order(phone="901234567")        # тот же гость, другой формат номера
        customer = Customer.objects.get()
        self.assertEqual((customer.points, customer.visits), (10000, 2))
        self.assertEqual(LoyaltyEntry.objects.count(), 2)
        loyalty.earn(Order.objects.filter(status=Order.Status.PAID).first())   # повтор не начисляет
        self.assertEqual(Customer.objects.get().points, 10000)

    def test_no_points_without_phone_or_when_disabled(self):
        self.paid_order(phone="")
        self.assertEqual(Customer.objects.count(), 0)
        cafe = CafeSettings.get()
        cafe.loyalty_percent = 0
        cafe.save()
        self.paid_order()
        self.assertEqual(Customer.objects.count(), 0)

    def test_refund_takes_points_back(self):
        order = self.paid_order()
        self.assertEqual(Customer.objects.get().points, 5000)
        services.refund_order(order, self.cashier, "ошибка")
        customer = Customer.objects.get()
        self.assertEqual((customer.points, customer.visits), (0, 0))

    def test_loyalty_failure_never_blocks_payment(self):
        with mock.patch("pos.loyalty.Customer.objects") as broken:
            broken.select_for_update.side_effect = RuntimeError("boom")
            order = self.paid_order()
        self.assertEqual(order.status, Order.Status.PAID)

    def test_receipt_shows_earned_points_and_panel_shows_balance(self):
        order = self.paid_order()
        self.client.force_login(self.cashier)
        self.assertContains(self.client.get(reverse("receipt", args=[order.id])), "5000")
        open_order = services.create_order(self.cashier, Order.OrderType.TAKEAWAY)
        open_order.customer_phone = "901234567"
        open_order.save(update_fields=["customer_phone"])
        response = self.client.get(reverse("order_panel", args=[open_order.id]))
        self.assertContains(response, "Баллы: 5000")
