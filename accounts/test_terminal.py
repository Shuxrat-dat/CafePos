from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import CafeSettings
from menu.models import Category, Dish
from pos import services
from pos.models import Hall, Order, OrderItem, Table

from . import terminal
from .models import Terminal, User


class TerminalBase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="owner_t", password="pass12345", role="owner")
        self.shoh = User.objects.create_user(username="shoh", password="pass12345", role="waiter", first_name="Shoh",
                                             sales_percent=Decimal("3"))
        self.other = User.objects.create_user(username="ikrom", password="pass12345", role="waiter", first_name="Ikrom")
        terminal.set_pin(self.shoh, "4826")
        terminal.set_pin(self.other, "7391")

    def make_terminal(self):
        """Владелец делает это устройство терминалом и выходит — как на моноблоке."""
        self.client.force_login(self.owner)
        self.client.post(reverse("manage_terminals"), {"action": "register", "name": "Bar"})
        key = self.client.cookies[terminal.COOKIE].value
        self.client.logout()  # тестовый клиент при выходе стирает все cookie; браузер ключ терминала сохраняет
        self.client.cookies[terminal.COOKIE] = key

    def pin_login(self, user, pin):
        return self.client.post(reverse("terminal_login"), {"user_id": user.id, "pin": pin})


class TerminalLoginTests(TerminalBase):
    def test_owner_registers_device_and_tiles_show_waiters(self):
        self.make_terminal()
        self.assertEqual(Terminal.objects.get().name, "Bar")
        html = self.client.get(reverse("terminal_home")).content.decode()
        self.assertIn("Shoh", html)
        self.assertIn("Ikrom", html)
        self.assertNotIn("owner_t", html)

    def test_login_page_on_terminal_redirects_to_tiles(self):
        self.make_terminal()
        self.assertRedirects(self.client.get(reverse("login")), reverse("terminal_home"))
        self.assertEqual(self.client.get(reverse("login") + "?password=1").status_code, 200)

    def test_correct_pin_opens_only_his_account(self):
        self.make_terminal()
        response = self.pin_login(self.shoh, "4826")
        self.assertRedirects(response, reverse("table_map"), fetch_redirect_response=False)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.shoh.id)
        self.assertTrue(self.client.session[terminal.SESSION_KEY])

    def test_wrong_pin_is_rejected(self):
        self.make_terminal()
        response = self.pin_login(self.shoh, "7391")  # PIN другого официанта
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_pin_locks_after_five_failures(self):
        self.make_terminal()
        for _ in range(5):
            self.pin_login(self.shoh, "0000")
        self.shoh.refresh_from_db()
        self.assertTrue(self.shoh.login_pin_is_locked)
        self.assertEqual(self.pin_login(self.shoh, "4826").status_code, 400)  # даже верный PIN пока не пускает

    def test_pin_does_not_work_from_unregistered_device(self):
        self.assertEqual(self.pin_login(self.shoh, "4826").status_code, 403)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_disabled_terminal_stops_pin_login(self):
        self.make_terminal()
        Terminal.objects.update(is_active=False)
        self.assertEqual(self.pin_login(self.shoh, "4826").status_code, 403)

    def test_owner_cannot_log_in_by_pin(self):
        self.make_terminal()
        self.owner.login_pin_hash = self.shoh.login_pin_hash
        self.owner.save()
        self.assertEqual(self.pin_login(self.owner, "4826").status_code, 400)

    def test_lock_returns_to_tiles(self):
        self.make_terminal()
        self.pin_login(self.shoh, "4826")
        page = self.client.get(reverse("table_map")).content.decode()
        self.assertIn('id="terminal-lock"', page)
        self.assertIn("180 * 1000", page)  # автоблокировка через 3 минуты
        response = self.client.post(reverse("terminal_lock"))
        self.assertRedirects(response, reverse("terminal_home"))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_password_login_on_phone_has_no_lock_timer(self):
        self.client.force_login(self.shoh)
        self.assertNotIn('id="terminal-lock"', self.client.get(reverse("table_map")).content.decode())

    def test_owner_sets_and_clears_pin_in_staff_form(self):
        self.client.force_login(self.owner)
        url = reverse("staff_edit", args=[self.other.id])
        data = {"username": "ikrom", "first_name": "Ikrom", "last_name": "", "role": "waiter", "is_active": "on",
                "hourly_rate": "", "sales_percent": "3", "password": "", "login_pin": "5917"}
        self.client.post(url, data)
        self.other.refresh_from_db()
        self.assertTrue(self.other.check_password("pass12345"))
        self.assertTrue(self.other.has_login_pin)
        self.client.post(url, {**data, "login_pin": "", "clear_login_pin": "on"})
        self.other.refresh_from_db()
        self.assertFalse(self.other.has_login_pin)

    def test_weak_pin_is_rejected(self):
        for weak in ("1234", "1111", "12", "abcd", "1234567"):
            self.assertTrue(terminal.weak_reason(weak), weak)
        self.assertEqual(terminal.weak_reason("4826"), "")


class WaiterEarningTests(TerminalBase):
    def setUp(self):
        super().setUp()
        cafe = CafeSettings.get()
        cafe.service_percent = Decimal("0")
        cafe.save()
        hall = Hall.objects.create(name="TEPA", service_percent=Decimal("10"))
        self.table = Table.objects.create(hall=hall, number="5")
        category = Category.objects.create(name="Горячее")
        self.dish = Dish.objects.create(category=category, name="Шашлык", price=Decimal("280000"))
        self.cashier = User.objects.create_user(username="kassa", password="pass12345", role="cashier")
        services.open_shift(self.cashier)

    def paid_order(self, qty=2):
        order = services.create_order(self.shoh, Order.OrderType.DINE_IN, self.table)
        OrderItem.objects.create(order=order, dish=self.dish, quantity=qty, price=self.dish.price)
        return services.pay_order(order, self.cashier, Order.PaymentMethod.CARD, None, None)

    def test_three_percent_of_dishes_not_of_service(self):
        order = self.paid_order()  # 560 000 + 10% обслуживания = 616 000
        self.assertEqual(order.total, Decimal("616000"))
        self.assertEqual(order.waiter_percent, Decimal("3"))
        self.assertEqual(order.waiter_earning, Decimal("16800"))

    def test_changing_percent_keeps_paid_orders(self):
        order = self.paid_order()
        self.shoh.sales_percent = Decimal("5")
        self.shoh.save()
        order = Order.objects.get(pk=order.pk)
        self.assertEqual(order.earned_by_waiter, Decimal("16800"))

    def test_waiter_sees_his_earnings(self):
        self.paid_order()
        self.client.force_login(self.shoh)
        page = self.client.get(reverse("my_earnings"))
        self.assertEqual(page.context["summary"]["today"]["earned"], Decimal("16800"))
        self.assertEqual(page.context["summary"]["month"]["count"], 1)
        self.assertEqual(self.client.get(reverse("table_map")).context["earned_today"], Decimal("16800"))

    def test_other_waiter_does_not_see_it(self):
        self.paid_order()
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse("my_earnings")).context["summary"]["today"]["earned"], Decimal("0"))

    def test_refund_removes_earning(self):
        order = self.paid_order()
        services.refund_order(order, self.owner, "ошибка")
        self.client.force_login(self.shoh)
        self.assertEqual(self.client.get(reverse("my_earnings")).context["summary"]["today"]["count"], 0)

    def test_payroll_uses_the_fixed_share(self):
        from reports.views import payroll_rows

        self.paid_order()
        today = timezone.localdate()
        row = next(r for r in payroll_rows(today, today) if r["user"] == self.shoh)
        self.assertEqual(row["sales"], Decimal("560000"))
        self.assertEqual(row["by_sales"], Decimal("16800"))
