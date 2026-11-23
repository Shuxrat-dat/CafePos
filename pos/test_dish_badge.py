from django.contrib.auth import get_user_model
from django.test import TestCase

from menu.models import Category, Dish
from pos.models import Order

User = get_user_model()


class DishCardBadgeTests(TestCase):
    """Число порций на карточке блюда считается в браузере по строкам корзины: у строки должен быть id блюда."""

    def setUp(self):
        self.waiter = User.objects.create_user(username="ofi_badge", password="pass12345", role="waiter")
        self.client.force_login(self.waiter)
        category = Category.objects.create(name="Еда")
        self.dish = Dish.objects.create(category=category, name="Бульон", price=3000)
        self.order = Order.objects.create(waiter=self.waiter, order_type="takeaway")

    def test_card_has_badge_and_cart_line_has_dish_id_and_quantity(self):
        self.order.items.create(dish=self.dish, quantity=3, price=self.dish.price)
        response = self.client.get(f"/orders/{self.order.id}/")
        self.assertContains(response, f'data-dish-pk="{self.dish.id}"')
        self.assertContains(response, "data-dish-badge")
        self.assertContains(response, f'data-line-dish="{self.dish.id}"')
        self.assertContains(response, 'data-line-qty="3"')
        self.assertContains(response, 'data-line-status="new"')
        self.assertContains(response, f"/orders/{self.order.id}/item/")  # адрес для «−» на карточке
        self.assertContains(response, 'data-step="+"')
        self.assertContains(response, 'data-step="-"')

    def test_panel_refresh_keeps_line_data(self):
        self.order.items.create(dish=self.dish, quantity=2, price=self.dish.price)
        response = self.client.get(f"/orders/{self.order.id}/panel/")
        self.assertContains(response, f'data-line-dish="{self.dish.id}"')
        self.assertContains(response, 'data-line-qty="2"')


class SetQuantityTests(TestCase):
    """Количество вводится цифрами: нажали на число в корзине и ввели, например, 20."""

    def setUp(self):
        self.waiter = User.objects.create_user(username="ofi_qty", password="pass12345", role="waiter")
        self.client.force_login(self.waiter)
        category = Category.objects.create(name="Еда")
        self.dish = Dish.objects.create(category=category, name="Самса", price=10000)
        self.order = Order.objects.create(waiter=self.waiter, order_type="takeaway")
        self.item = self.order.items.create(dish=self.dish, quantity=1, price=self.dish.price)
        self.url = f"/orders/{self.order.id}/item/{self.item.id}/set-qty/"

    def test_form_opens_with_current_quantity(self):
        response = self.client.get(self.url)
        self.assertContains(response, 'name="quantity"')
        self.assertContains(response, 'value="1"')
        # Цифровая панель без запятой и без «00»/«000»
        self.assertContains(response, 'data-numpad="qty-input"')
        self.assertNotContains(response, 'data-key=","')
        self.assertNotContains(response, 'data-key="00"')
        self.assertNotContains(response, 'data-key="000"')

    def test_sets_new_item_quantity(self):
        self.client.post(self.url, {"quantity": "20"})
        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity, 20)

    def test_quantity_is_clamped_and_garbage_is_ignored(self):
        self.client.post(self.url, {"quantity": "5000"})
        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity, 99)
        self.client.post(self.url, {"quantity": "abc"})
        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity, 99)
        self.client.post(self.url, {"quantity": "0"})
        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity, 1)

    def test_sent_item_increase_adds_new_line_for_kitchen(self):
        self.item.status = "sent"
        self.item.save()
        self.client.post(self.url, {"quantity": "20"})
        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity, 1)
        extra = self.order.items.exclude(pk=self.item.pk).get()
        self.assertEqual((extra.quantity, extra.status), (19, "new"))

    def test_sent_item_decrease_is_audited(self):
        from core.models import AuditLog

        self.item.status = "sent"
        self.item.quantity = 10
        self.item.save()
        self.client.post(self.url, {"quantity": "4"})
        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity, 4)
        self.assertTrue(AuditLog.objects.filter(order_id=self.order.id).exists())


class QuickPrecheckTests(TestCase):
    """«Пречек»: один принтер для чеков — печатаем сразу и остаёмся в заказе; иначе открываем страницу пречека."""

    def setUp(self):
        from menu.models import Category, Dish

        self.waiter = User.objects.create_user(username="ofi_bill", password="pass12345", role="waiter")
        self.client.force_login(self.waiter)
        dish = Dish.objects.create(category=Category.objects.create(name="Еда"), name="Плов", price=45000)
        self.order = Order.objects.create(waiter=self.waiter, order_type="takeaway")
        self.order.items.create(dish=dish, quantity=1, price=dish.price)
        self.url = f"/orders/{self.order.id}/bill/"

    def test_no_printer_opens_precheck_page(self):
        response = self.client.post(self.url)
        self.assertRedirects(response, f"/orders/{self.order.id}/precheck/", fetch_redirect_response=False)

    def test_one_printer_prints_at_once_and_stays_on_order(self):
        from printing.models import Printer, PrintJob

        Printer.objects.create(name="Касса", host="192.168.1.50")
        response = self.client.post(self.url, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("HX-Redirect", response.headers)
        self.assertEqual(PrintJob.objects.filter(order=self.order, kind="precheck").count(), 1)
        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.bill_requested_at)

    def test_one_printer_without_htmx_goes_back_to_order(self):
        from printing.models import Printer

        Printer.objects.create(name="Касса", host="192.168.1.50")
        response = self.client.post(self.url)
        self.assertRedirects(response, f"/orders/{self.order.id}/", fetch_redirect_response=False)

    def test_two_printers_let_the_waiter_choose_on_the_page(self):
        from printing.models import Printer, PrintJob

        Printer.objects.create(name="Касса", host="192.168.1.50")
        Printer.objects.create(name="Бар", host="192.168.1.52")
        response = self.client.post(self.url, HTTP_HX_REQUEST="true")
        self.assertEqual(response["HX-Redirect"], f"/orders/{self.order.id}/precheck/")
        self.assertFalse(PrintJob.objects.exists())

    def test_from_order_list_returns_only_a_toast(self):
        from printing.models import Printer, PrintJob

        Printer.objects.create(name="Касса", host="192.168.1.50")
        response = self.client.post(self.url, {"list": "1"}, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 204)
        self.assertIn("toast", response["HX-Trigger"])
        self.assertEqual(PrintJob.objects.filter(order=self.order, kind="precheck").count(), 1)
