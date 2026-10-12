"""Тесты новых возможностей кассы: типы заказов, скидки, обслуживание, смешанная оплата, кухня, заказы, роли."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.urls import reverse

from core.models import CafeSettings
from inventory.models import Product, RecipeItem, StockMovement
from menu.models import Category, Dish, Modifier

from . import services
from .models import Hall, Order, OrderItem, Table

User = get_user_model()


class Base(TestCase):
    def setUp(self):
        self.cashier = User.objects.create_user(username="kas", password="pass12345", role="cashier")
        self.waiter = User.objects.create_user(username="ofi", password="pass12345", role="waiter")
        self.admin = User.objects.create_user(username="adm", password="pass12345", role="admin")
        self.cook = User.objects.create_user(username="osh", password="pass12345", role="cook")
        self.hall = Hall.objects.create(name="Зал")
        self.table = Table.objects.create(hall=self.hall, number="1")
        self.table2 = Table.objects.create(hall=self.hall, number="2")
        self.category = Category.objects.create(name="Горячее")
        self.plov = Dish.objects.create(category=self.category, name="Плов", price=45000)
        self.tea = Dish.objects.create(category=self.category, name="Чай", price=10000)

    def order(self, *lines, order_type="dine_in", table=None, status=OrderItem.Status.SERVED, waiter=None):
        order = services.create_order(waiter or self.cashier, order_type, table if order_type == "dine_in" else None)
        for dish, qty in lines:
            OrderItem.objects.create(order=order, dish=dish, quantity=qty, status=status)
        return order


class TotalsTests(Base):
    def test_percent_discount_and_service(self):
        cafe = CafeSettings.get()
        cafe.service_percent = 10
        cafe.save()
        order = self.order((self.plov, 2), (self.tea, 1), table=self.table)  # 100 000
        self.assertEqual(services.apply_discount(order, "percent", "10", "Постоянный гость"), "")
        totals = order.totals
        self.assertEqual(totals["subtotal"], Decimal(100000))
        self.assertEqual(totals["discount"], Decimal(10000))
        self.assertEqual(totals["service"], Decimal(9000))  # 10% от суммы после скидки
        self.assertEqual(totals["total"], Decimal(99000))

    def test_service_only_for_dine_in(self):
        cafe = CafeSettings.get()
        cafe.service_percent = 10
        cafe.save()
        order = self.order((self.plov, 1), order_type="takeaway")
        self.assertEqual(order.total, Decimal(45000))

    def test_amount_discount_validation(self):
        order = self.order((self.plov, 1), table=self.table)
        self.assertEqual(services.apply_discount(order, "amount", "50 000"), "Скидка больше суммы заказа.")
        self.assertEqual(services.apply_discount(order, "percent", "150"), "Скидка не может быть больше 100%.")
        self.assertEqual(services.apply_discount(order, "amount", "5 000"), "")
        self.assertEqual(order.total, Decimal(40000))
        services.apply_discount(order, "", "")
        self.assertEqual(order.total, Decimal(45000))

    def test_discount_form_forbidden_for_waiter(self):
        order = self.order((self.plov, 1), table=self.table, waiter=self.waiter)
        self.client.force_login(self.waiter)
        response = self.client.post(reverse("discount_form", args=[order.id]), {"kind": "percent", "value": "50"})
        self.assertEqual(response.status_code, 403)


class PaymentTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.cashier)

    def test_mixed_payment_and_change(self):
        order = self.order((self.plov, 2), (self.tea, 1), table=self.table)  # 100 000
        response = self.client.post(
            reverse("pay", args=[order.id]), {"method": "mixed", "card": "60 000", "received": "50 000"}
        )
        self.assertRedirects(response, reverse("receipt", args=[order.id]))
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PAID)
        self.assertEqual(order.card_amount, Decimal(60000))
        self.assertEqual(order.cash_amount, Decimal(40000))
        self.assertEqual(order.change, Decimal(10000))
        self.assertEqual(order.cashier, self.cashier)
        receipt = self.client.get(reverse("receipt", args=[order.id]))
        self.assertContains(receipt, "Смешанная")

    def test_mixed_payment_rejects_full_card(self):
        order = self.order((self.plov, 1), table=self.table)
        response = self.client.post(reverse("pay", args=[order.id]), {"method": "mixed", "card": "45000"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("меньше итога", response.context["error"])
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.OPEN)

    def test_cash_with_spaces(self):
        order = self.order((self.plov, 1), table=self.table)
        self.client.post(reverse("pay", args=[order.id]), {"method": "cash", "received": "50 000"})
        order.refresh_from_db()
        self.assertEqual(order.cash_received, Decimal(50000))
        self.assertEqual(order.change, Decimal(5000))

    def test_payment_sends_unsent_items_and_writes_off(self):
        beef = Product.objects.create(name="Баранина", unit="g", cost_per_unit=100)
        StockMovement.objects.create(product=beef, kind="income", quantity=1000)
        RecipeItem.objects.create(dish=self.plov, product=beef, quantity=120)
        order = self.order((self.plov, 2), order_type="takeaway", status=OrderItem.Status.NEW)
        self.client.post(reverse("pay", args=[order.id]), {"method": "card"})
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PAID)
        self.assertEqual(order.items.get().status, OrderItem.Status.QUEUED)
        beef.refresh_from_db()
        self.assertEqual(beef.stock, Decimal(760))

    def test_split_keeps_percent_discount(self):
        order = self.order((self.plov, 2), table=self.table)
        services.apply_discount(order, "percent", "10")
        item = order.items.get()
        self.client.post(
            reverse("pay", args=[order.id]),
            {"split": "1", "method": "card", f"qty_{item.id}": "1"},
        )
        paid = Order.objects.get(status=Order.Status.PAID)
        order.refresh_from_db()
        self.assertEqual(paid.total, Decimal(40500))
        self.assertEqual(order.total, Decimal(40500))


class OrderFlowTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.cashier)

    def test_new_takeaway_reuses_empty_draft(self):
        first = self.client.post(reverse("new_order"), {"type": "takeaway"})
        second = self.client.post(reverse("new_order"), {"type": "takeaway"})
        self.assertEqual(first.url, second.url)
        order = Order.objects.get()
        self.assertEqual(order.order_type, "takeaway")
        self.assertIsNone(order.table)

    def test_add_change_remove_items(self):
        order = self.order(order_type="takeaway")
        self.client.post(reverse("add_item", args=[order.id, self.plov.id]))
        self.client.post(reverse("add_item", args=[order.id, self.plov.id]))
        item = order.items.get()
        self.assertEqual(item.quantity, 2)
        self.client.post(reverse("change_qty", args=[order.id, item.id]), {"delta": "-1"})
        item.refresh_from_db()
        self.assertEqual(item.quantity, 1)
        self.client.post(reverse("remove_item", args=[order.id, item.id]))
        self.assertFalse(order.items.exists())

    def test_stop_list_dish_cannot_be_added(self):
        self.plov.is_available = False
        self.plov.save()
        order = self.order(order_type="takeaway")
        response = self.client.post(reverse("add_item", args=[order.id, self.plov.id]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(order.items.exists())

    def test_size_group_allows_one_choice(self):
        small = Modifier.objects.create(name="S", group="Размер")
        large = Modifier.objects.create(name="L", group="Размер", price_delta=9000)
        self.tea.modifiers.set([small, large])
        order = self.order(order_type="takeaway")
        self.client.post(reverse("add_item", args=[order.id, self.tea.id]), {"modifiers": [small.id, large.id]})
        self.assertEqual(order.items.get().modifiers.count(), 1)

    def test_empty_order_does_not_occupy_table(self):
        self.client.post(reverse("open_table", args=[self.table.id]))
        response = self.client.get(reverse("floor"))
        table = next(t for h in response.context["halls"] for t in h.tables.all() if t.id == self.table.id)
        self.assertEqual(table.state, "free")

    def test_bill_request_marks_table(self):
        order = self.order((self.plov, 1), table=self.table)
        self.client.post(reverse("request_bill", args=[order.id]))
        response = self.client.get(reverse("floor"))
        table = next(t for h in response.context["halls"] for t in h.tables.all() if t.id == self.table.id)
        self.assertEqual(table.state, "bill")

    def test_reservation_and_seating(self):
        self.client.post(reverse("reserve_table", args=[self.table2.id]), {"name": "Азиз", "time": "19:30"})
        self.table2.refresh_from_db()
        self.assertTrue(self.table2.is_reserved)
        self.client.post(reverse("open_table", args=[self.table2.id]))
        self.table2.refresh_from_db()
        self.assertFalse(self.table2.is_reserved)

    def test_move_takeaway_to_table_adds_service(self):
        cafe = CafeSettings.get()
        cafe.service_percent = 10
        cafe.save()
        order = self.order((self.plov, 1), order_type="takeaway")
        self.client.post(reverse("move_order", args=[order.id, self.table2.id]))
        order.refresh_from_db()
        self.assertEqual(order.order_type, "dine_in")
        self.assertEqual(order.table, self.table2)
        self.assertEqual(order.total, Decimal(49500))

    def test_cancel_keeps_history(self):
        order = self.order((self.plov, 1), table=self.table, status=OrderItem.Status.NEW)
        self.client.post(reverse("cancel_order", args=[order.id]), {"reason": "Гость ушёл"})
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.CANCELLED)
        self.assertEqual(order.cancel_reason, "Гость ушёл")
        self.assertEqual(order.cancelled_by, self.cashier)
        self.assertEqual(order.items.count(), 1)


class KitchenFlowTests(Base):
    def test_ticket_moves_through_columns(self):
        order = self.order((self.plov, 1), table=self.table, status=OrderItem.Status.NEW)
        services.send_to_kitchen(order)
        self.client.force_login(self.cook)
        board = self.client.get(reverse("kitchen_board"))
        self.assertEqual([o.id for o in board.context["columns"]["new"]], [order.id])
        self.client.post(reverse("ticket_start", args=[order.id]))
        self.client.post(reverse("ticket_ready", args=[order.id]))
        item = order.items.get()
        self.assertEqual(item.status, OrderItem.Status.READY)
        self.assertIsNotNone(item.ready_at)
        self.client.post(reverse("ticket_served", args=[order.id]))
        self.assertEqual(order.items.get().status, OrderItem.Status.SERVED)

    def test_cancelled_order_leaves_kitchen(self):
        order = self.order((self.plov, 1), table=self.table, status=OrderItem.Status.COOKING)
        services.cancel_order(order, self.admin, "")
        self.client.force_login(self.cook)
        board = self.client.get(reverse("kitchen_board"))
        self.assertFalse(any(board.context["columns"].values()))


class OrdersPageTests(Base):
    def test_active_and_done_tabs(self):
        active = self.order((self.plov, 1), table=self.table, status=OrderItem.Status.COOKING)
        done = self.order((self.tea, 1), table=self.table2)
        services.pay_order(done, self.cashier, "card", None, None)
        self.client.force_login(self.cashier)
        response = self.client.get(reverse("orders"))
        self.assertEqual([o.id for o in response.context["page"].object_list], [active.id])
        response = self.client.get(reverse("orders"), {"tab": "done"})
        self.assertEqual([o.id for o in response.context["page"].object_list], [done.id])

    def test_waiter_sees_only_own_orders(self):
        mine = self.order((self.plov, 1), table=self.table, waiter=self.waiter)
        self.order((self.tea, 1), table=self.table2)
        self.client.force_login(self.waiter)
        response = self.client.get(reverse("orders"))
        self.assertEqual([o.id for o in response.context["page"].object_list], [mine.id])

    def test_search_by_number(self):
        order = self.order((self.plov, 1), table=self.table)
        self.order((self.tea, 1), table=self.table2)
        self.client.force_login(self.cashier)
        response = self.client.get(reverse("orders"), {"q": f"№{order.id}"})
        self.assertEqual([o.id for o in response.context["page"].object_list], [order.id])


class ManageTests(Base):
    def test_manage_pages_only_for_managers(self):
        for user, code in ((self.cashier, 403), (self.waiter, 403), (self.admin, 200)):
            self.client.force_login(user)
            for name in ("manage_menu", "manage_staff", "manage_settings", "manage_tables"):
                self.assertEqual(self.client.get(reverse(name)).status_code, code, (user.role, name))

    def test_dish_used_in_orders_is_hidden_not_deleted(self):
        self.order((self.plov, 1), table=self.table)
        self.client.force_login(self.admin)
        self.client.post(reverse("dish_delete", args=[self.plov.id]))
        self.plov.refresh_from_db()
        self.assertTrue(self.plov.is_hidden)

    def test_dish_price_must_be_positive(self):
        self.client.force_login(self.admin)
        data = {"name": "Суп", "category": self.category.id, "price": "0", "sort_order": "0",
                "recipe-TOTAL_FORMS": "0", "recipe-INITIAL_FORMS": "0"}
        response = self.client.post(reverse("dish_create"), data)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Dish.objects.filter(name="Суп").exists())
        data["price"] = "30000"
        self.client.post(reverse("dish_create"), data)
        self.assertTrue(Dish.objects.filter(name="Суп", price=30000).exists())

    def test_admin_cannot_create_owner(self):
        self.client.force_login(self.admin)
        self.client.post(reverse("staff_create"), {
            "username": "boss", "first_name": "Б", "role": "owner", "password": "Strong-pass-2026", "is_active": "on",
        })
        self.assertFalse(User.objects.filter(username="boss").exists())
        self.client.post(reverse("staff_create"), {
            "username": "newcook", "first_name": "П", "role": "cook", "password": "Strong-pass-2026", "is_active": "on",
        })
        self.assertTrue(User.objects.get(username="newcook").check_password("Strong-pass-2026"))


class LanguageTests(Base):
    def test_uzbek_interface_by_cookie(self):
        self.client.force_login(self.cashier)
        self.client.post(reverse("set_language"), {"language": "uz", "next": "/"})
        response = self.client.get(reverse("floor"))
        self.assertContains(response, "Stol raqami")
        self.assertNotContains(response, "Номер стола")

    def test_russian_interface(self):
        self.client.force_login(self.cashier)
        self.client.post(reverse("set_language"), {"language": "ru", "next": "/"})
        self.assertContains(self.client.get(reverse("floor")), "Номер стола")

    def test_language_redirect_is_local_only(self):
        response = self.client.post(reverse("set_language"), {"language": "uz", "next": "https://evil.example/"})
        self.assertEqual(response.url, "/")


class DemoGuardTests(TestCase):
    def test_seed_refuses_outside_demo_mode(self):
        with self.assertRaises(CommandError):
            call_command("seed_demo")
        self.assertFalse(User.objects.exists())

    def test_demo_login_disabled_outside_demo_mode(self):
        User.objects.create_user(username="owner", password="x", role="owner")
        self.assertEqual(self.client.post(reverse("demo_login"), {"username": "owner"}).status_code, 404)
