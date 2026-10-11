from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from menu.models import Category, Dish

from .models import Hall, Order, OrderItem, Table

User = get_user_model()


class CashPaymentTests(TestCase):
    def setUp(self):
        self.cashier = User.objects.create_user(
            username="cashier1", password="pass12345", role="cashier"
        )
        hall = Hall.objects.create(name="Основной зал")
        table = Table.objects.create(hall=hall, number="1")
        category = Category.objects.create(name="Горячее")
        dish = Dish.objects.create(category=category, name="Плов", price=25000)

        self.order = Order.objects.create(table=table, waiter=self.cashier)
        # Позиция уже подана, значит заказ готов к оплате. Итого: 2 x 25000 = 50000
        OrderItem.objects.create(
            order=self.order, dish=dish, quantity=2, status=OrderItem.Status.SERVED
        )
        self.client.force_login(self.cashier)

    def test_total_is_calculated(self):
        self.assertEqual(self.order.total, Decimal("50000"))

    def test_cash_payment_with_change(self):
        response = self.client.post(
            reverse("pay", args=[self.order.id]),
            {"method": "cash", "received": "60000"},
        )
        self.assertRedirects(response, reverse("receipt", args=[self.order.id]))

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)
        self.assertEqual(self.order.payment_method, "cash")
        self.assertEqual(self.order.cash_received, Decimal("60000"))

        receipt = self.client.get(reverse("receipt", args=[self.order.id]))
        self.assertEqual(receipt.context["change"], Decimal("10000"))

    def test_cash_payment_rejected_if_not_enough(self):
        response = self.client.post(
            reverse("pay", args=[self.order.id]),
            {"method": "cash", "received": "40000"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["error"], "Полученная сумма меньше итога.")

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.OPEN)

class SplitPaymentTests(TestCase):
            def setUp(self):
                self.cashier = User.objects.create_user(
                    username="cashier2", password="pass12345", role="cashier"
                )
                hall = Hall.objects.create(name="Основной зал")
                table = Table.objects.create(hall=hall, number="1")
                category = Category.objects.create(name="Горячее")
                self.plov = Dish.objects.create(category=category, name="Плов", price=25000)
                self.tea = Dish.objects.create(category=category, name="Чай", price=5000)

                self.order = Order.objects.create(table=table, waiter=self.cashier)
                # Плов 2 шт (50000) и чай 1 шт (5000). Итого: 55000
                self.plov_item = OrderItem.objects.create(
                    order=self.order, dish=self.plov, quantity=2, status=OrderItem.Status.SERVED
                )
                self.tea_item = OrderItem.objects.create(
                    order=self.order, dish=self.tea, quantity=1, status=OrderItem.Status.SERVED
                )
                self.client.force_login(self.cashier)

            def _post_split(self, plov_qty, tea_qty, received="100000"):
                return self.client.post(
                    reverse("pay", args=[self.order.id]),
                    {
                        "split": "1",
                        "method": "cash",
                        "received": received,
                        f"qty_{self.plov_item.id}": str(plov_qty),
                        f"qty_{self.tea_item.id}": str(tea_qty),
                    },
                )

            def test_split_part_of_item(self):
                # Оплачиваем 1 плов из 2 (25000), чай остаётся
                response = self._post_split(plov_qty=1, tea_qty=0, received="30000")

                paid = Order.objects.get(status=Order.Status.PAID)
                self.assertRedirects(response, reverse("receipt", args=[paid.id]))
                self.assertEqual(paid.total, Decimal("25000"))
                self.assertEqual(paid.cash_received, Decimal("30000"))

                self.order.refresh_from_db()
                self.assertEqual(self.order.status, Order.Status.OPEN)
                self.assertEqual(self.order.total, Decimal("30000"))

            def test_split_totals_add_up(self):
                self._post_split(plov_qty=1, tea_qty=1, received="30000")
                paid = Order.objects.get(status=Order.Status.PAID)
                self.order.refresh_from_db()
                self.assertEqual(paid.total + self.order.total, Decimal("55000"))

            def test_split_whole_item_moves_to_paid_order(self):
                # Оплачиваем весь чай целиком: позиция переезжает в новый заказ
                self._post_split(plov_qty=0, tea_qty=1, received="5000")
                paid = Order.objects.get(status=Order.Status.PAID)
                self.assertEqual(paid.items.count(), 1)
                self.assertEqual(paid.items.first().dish, self.tea)
                self.assertEqual(self.order.items.count(), 1)

            def test_split_rejects_too_many(self):
                response = self._post_split(plov_qty=3, tea_qty=0)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["error"], "Проверьте количество у позиций.")
                self.assertFalse(Order.objects.filter(status=Order.Status.PAID).exists())

            def test_split_rejects_empty_selection(self):
                response = self._post_split(plov_qty=0, tea_qty=0)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["error"], "Выберите хотя бы одну позицию.")

class PermissionTests(TestCase):
                def setUp(self):
                    self.waiter1 = User.objects.create_user(username="w1", password="pass12345", role="waiter")
                    self.waiter2 = User.objects.create_user(username="w2", password="pass12345", role="waiter")
                    self.cashier = User.objects.create_user(username="c1", password="pass12345", role="cashier")
                    self.admin = User.objects.create_user(username="a1", password="pass12345", role="admin")
                    self.cook = User.objects.create_user(username="k1", password="pass12345", role="cook")

                    hall = Hall.objects.create(name="Основной зал")
                    self.table = Table.objects.create(hall=hall, number="1")
                    category = Category.objects.create(name="Горячее")
                    self.dish = Dish.objects.create(category=category, name="Плов", price=25000)

                def _make_order(self, item_status):
                    order = Order.objects.create(table=self.table, waiter=self.waiter1)
                    OrderItem.objects.create(order=order, dish=self.dish, quantity=1, status=item_status)
                    return order

                def test_anonymous_goes_to_login(self):
                    response = self.client.get(reverse("floor"))
                    self.assertEqual(response.status_code, 302)
                    self.assertIn("login", response.url)

                def test_waiter_opens_own_order(self):
                    order = self._make_order(OrderItem.Status.NEW)
                    self.client.force_login(self.waiter1)
                    response = self.client.get(reverse("order_detail", args=[order.id]))
                    self.assertEqual(response.status_code, 200)

                def test_waiter_cannot_open_foreign_order(self):
                    order = self._make_order(OrderItem.Status.NEW)
                    self.client.force_login(self.waiter2)
                    response = self.client.get(reverse("order_detail", args=[order.id]))
                    self.assertEqual(response.status_code, 403)

                def test_cashier_opens_any_order(self):
                    order = self._make_order(OrderItem.Status.NEW)
                    self.client.force_login(self.cashier)
                    response = self.client.get(reverse("order_detail", args=[order.id]))
                    self.assertEqual(response.status_code, 200)

                def test_waiter_cannot_pay(self):
                    order = self._make_order(OrderItem.Status.SERVED)
                    self.client.force_login(self.waiter1)
                    response = self.client.post(
                        reverse("pay", args=[order.id]), {"method": "cash", "received": "25000"}
                    )
                    self.assertEqual(response.status_code, 403)
                    order.refresh_from_db()
                    self.assertEqual(order.status, Order.Status.OPEN)

                def test_cook_cannot_pay(self):
                    order = self._make_order(OrderItem.Status.SERVED)
                    self.client.force_login(self.cook)
                    response = self.client.post(
                        reverse("pay", args=[order.id]), {"method": "cash", "received": "25000"}
                    )
                    self.assertEqual(response.status_code, 403)

                def test_waiter_can_cancel_unsent_order(self):
                    order = self._make_order(OrderItem.Status.NEW)
                    self.client.force_login(self.waiter1)
                    self.client.post(reverse("cancel_order", args=[order.id]))
                    order.refresh_from_db()
                    self.assertEqual(order.status, Order.Status.CANCELLED)

                def test_waiter_cannot_cancel_sent_order(self):
                    order = self._make_order(OrderItem.Status.COOKING)
                    self.client.force_login(self.waiter1)
                    self.client.post(reverse("cancel_order", args=[order.id]))
                    order.refresh_from_db()
                    self.assertEqual(order.status, Order.Status.OPEN)

                def test_admin_can_cancel_sent_order(self):
                    order = self._make_order(OrderItem.Status.COOKING)
                    self.client.force_login(self.admin)
                    self.client.post(reverse("cancel_order", args=[order.id]))
                    order.refresh_from_db()
                    self.assertEqual(order.status, Order.Status.CANCELLED)

                def test_cook_is_redirected_from_floor_to_kitchen(self):
                    self.client.force_login(self.cook)
                    response = self.client.get(reverse("floor"))
                    self.assertRedirects(response, reverse("kitchen_board"), fetch_redirect_response=False)